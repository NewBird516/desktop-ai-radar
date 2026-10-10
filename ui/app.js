/* AI 雷达 — 前端逻辑
   数据通道：轮询 window.pywebview.api.get_data()（后端内存 O(1) 返回，不阻塞）
   updated_at 变化时才重绘，避免无谓的重排。 */

(function () {
  'use strict';

  var api = null;
  var lastStamp = 0;
  var lastRepoStamp = 0;
  var refreshing = false;
  var currentPeriod = 'weekly';
  var PERIOD_SUFFIX = { daily: '/日', weekly: '/周', monthly: '/月' };
  var REFRESH_INTERVAL = 3600; // 与后端保持一致，仅用于倒计时显示

  var el = {
    newsList: document.getElementById('news-list'),
    repoList: document.getElementById('repo-list'),
    newsMeta: document.getElementById('news-meta'),
    repoMeta: document.getElementById('repo-meta'),
    repoSeg: document.getElementById('repo-seg'),
    stamp: document.getElementById('stamp'),
    status: document.getElementById('status-text'),
    countdown: document.getElementById('countdown'),
    btnRefresh: document.getElementById('btn-refresh'),
    btnMin: document.getElementById('btn-min'),
    btnClose: document.getElementById('btn-close'),
    titlebar: document.getElementById('titlebar')
  };

  function setSegActive(p) {
    if (!el.repoSeg) return;
    var btns = el.repoSeg.querySelectorAll('.seg-btn');
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle('active', btns[i].getAttribute('data-period') === p);
    }
  }

  // ---------- 工具 ----------
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function relTime(ts) {
    if (!ts) return '';
    var diff = Math.floor(Date.now() / 1000) - ts;
    if (diff < 0) diff = 0;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 172800) return '昨天';
    var d = new Date(ts * 1000);
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function clockTime(ts) {
    if (!ts) return '--:--';
    var d = new Date(ts * 1000);
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  function fmtStar(n) {
    if (!n) return '0';
    if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k';
    return String(n);
  }

  // ---------- 贝塞尔超椭圆轮廓 ----------
  // 每个转角用一条三次贝塞尔曲线，控制点长度超过圆弧的 0.552 倍，
  // 曲率在「直线段 → 圆角」之间连续过渡，观感接近 Apple 的 squircle。
  var CURVE = 0.64;

  function squirclePath(w, h, r) {
    if (!(w > 0) || !(h > 0)) return '';
    r = Math.max(0, Math.min(r, Math.min(w, h) / 2));
    var c = r * CURVE;
    return 'M' + r + ',0' +
      'H' + (w - r) +
      'C' + (w - r + c) + ',0 ' + w + ',' + (r - c) + ' ' + w + ',' + r +
      'V' + (h - r) +
      'C' + w + ',' + (h - r + c) + ' ' + (w - r + c) + ',' + h + ' ' + (w - r) + ',' + h +
      'H' + r +
      'C' + (r - c) + ',' + h + ' 0,' + (h - r + c) + ' 0,' + (h - r) +
      'V' + r +
      'C0,' + (r - c) + ' ' + (r - c) + ',0 ' + r + ',0Z';
  }

  function paintPanel(panel, radius) {
    var svg = panel.querySelector('.sq');
    if (!svg) return;
    var w = panel.clientWidth, h = panel.clientHeight;
    if (!w || !h) return;
    svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
    svg.setAttribute('preserveAspectRatio', 'none');
    var fill = svg.querySelector('.sq-fill');
    var edge = svg.querySelector('.sq-edge');
    if (fill) fill.setAttribute('d', squirclePath(w, h, radius));
    // 描边内缩 0.5px，避免被容器边界裁掉一半
    if (edge) {
      edge.setAttribute('d', squirclePath(w - 1, h - 1, radius - 0.5));
      edge.setAttribute('transform', 'translate(.5,.5)');
    }
  }

  function paintAll() {
    var panels = document.querySelectorAll('.panel');
    for (var i = 0; i < panels.length; i++) paintPanel(panels[i], 18);
  }

  function observeLayout() {
    paintAll();
    window.addEventListener('resize', paintAll);
    if (window.ResizeObserver) {
      var ro = new ResizeObserver(paintAll);
      var panels = document.querySelectorAll('.panel');
      for (var i = 0; i < panels.length; i++) ro.observe(panels[i]);
    }
  }

  // ---------- 渲染 ----------
  function renderNews(news) {
    if (!news || !news.length) {
      el.newsList.innerHTML = '<div class="empty-tip">暂无资讯<br>请检查网络后点击右上角刷新</div>';
      el.newsMeta.textContent = '0 条';
      return;
    }
    el.newsMeta.textContent = news.length + ' 条 · 国内外';
    var html = news.map(function (n, i) {
      var cls = 'news-item ' + (n.region === 'cn' ? 'cn' : 'en');
      // RSS 自带的 description 很短时（很多中文源只给一句话），改为按需拉取原文正文
      var short = !n.summary || n.summary.length < 80;
      var body = short
        ? '<p class="loading-article">正在读取原文正文…</p>'
        : '<p>' + esc(n.summary) + '</p>';
      // 国外资讯优先显示机翻标题；英文原标题放在 tooltip 和展开区，机翻失真时可对照
      var zh = n.title_zh || '';
      var shown = zh || n.title;
      var tip = zh ? ('原标题：' + n.title) : n.source;
      var origin = zh
        ? '<p class="origin-title">原标题：' + esc(n.title) + '</p>'
        : '';
      return '' +
        '<div class="' + cls + '" data-url="' + esc(n.url) + '" data-idx="' + i + '"' +
          ' data-short="' + (short ? '1' : '0') + '">' +
          '<div class="news-top">' +
            '<span class="chip">' + esc(n.source) + '</span>' +
            '<span class="news-title" title="' + esc(tip) + '">' + esc(shown) + '</span>' +
            '<span class="news-time">' + relTime(n.ts) + '</span>' +
          '</div>' +
          '<div class="news-detail">' +
            origin +
            '<div class="detail-body">' + body + '</div>' +
            '<div class="detail-act">' +
              '<button class="link-btn" data-open="' + esc(n.url) + '">打开原文 ↗</button>' +
              '<button class="link-btn ghost" data-copy="' + esc(n.url) + '">复制链接</button>' +
            '</div>' +
          '</div>' +
        '</div>';
    }).join('');
    el.newsList.innerHTML = html;
  }

  // ---------- 按需拉取原文正文 ----------
  function requestArticle(item, retry) {
    var body = item.querySelector('.detail-body');
    if (!body || body.getAttribute('data-loaded') === '1') return;

    function fail(hint) {
      body.innerHTML = '<p class="empty">' + (hint || '无法自动提取正文，请点击「打开原文」阅读。') + '</p>';
    }

    api.get_article(item.getAttribute('data-url')).then(function (res) {
      if (res.state === 'pending') {
        if (retry > 0) {
          setTimeout(function () { requestArticle(item, retry - 1); }, 1200);
        } else {
          fail('正文仍在抓取中，请稍等几秒后重新展开，或点击「打开原文」。');
        }
        return;
      }
      if (res.state === 'done' && res.text) {
        body.setAttribute('data-loaded', '1');
        body.innerHTML = '<p>' + esc(res.text) + '</p>';
        return;
      }
      fail();
    }).catch(function () { fail(); });
  }

  function renderRepos(repos, source) {
    if (!repos || !repos.length) {
      el.repoList.innerHTML = '<div class="empty-tip">暂无 GitHub 数据</div>';
      el.repoMeta.textContent = '数据不可用';
      return;
    }
    el.repoMeta.textContent = 'Top ' + repos.length;
    var suffix = PERIOD_SUFFIX[currentPeriod] || '/周';
    el.repoList.innerHTML = repos.map(function (r, i) {
      var week = r.delta ? '<span class="week">+' + fmtStar(r.delta) + suffix + '</span>' : '';
      var zh = r.desc_zh || '';
      var shown = zh || r.desc || '暂无描述';
      // 鼠标悬停时可看到英文原版简介，中译文看不顺时也有得对照
      var tip = zh && r.desc ? (r.full_name + '\n\n原文简介：' + r.desc) : (r.full_name || '');
      return '' +
        '<div class="repo-item" data-url="' + esc(r.url) + '" title="' + esc(tip) + '">' +
          '<span class="repo-rank">' + (i + 1) + '</span>' +
          '<div class="repo-body">' +
            '<div class="repo-line">' +
              '<span class="repo-name">' + esc(r.full_name) + '</span>' +
              (r.lang ? '<span class="repo-lang">' + esc(r.lang) + '</span>' : '') +
              '<span class="repo-star">★ ' + fmtStar(r.stars) + week + '</span>' +
            '</div>' +
            '<div class="repo-desc">' + esc(shown || '暂无描述') + '</div>' +
          '</div>' +
        '</div>';
    }).join('');
  }

  function render(data) {
    var news = data.news || [];
    var repos = data.repos || [];

    if (data.repo_period && data.repo_period !== currentPeriod) {
      currentPeriod = data.repo_period;
    }
    setSegActive(currentPeriod);

    if (news.length || repos.length) {
      if (data.updated_at !== lastStamp) {
        renderNews(news);
        lastStamp = data.updated_at;
      }
      if (data.updated_at !== lastRepoStamp) {
        renderRepos(repos, data.repo_source);
        lastRepoStamp = data.updated_at;
      }
      el.stamp.textContent = '更新于 ' + clockTime(data.updated_at);
      el.status.textContent = '共 ' + news.length + ' 条资讯 · ' + repos.length + ' 个仓库 · 每小时自动刷新';
    }

    if (data.error && !news.length && !repos.length) {
      el.newsList.innerHTML = '<div class="error-tip">抓取失败：' + esc(data.error) + '<br>稍后会自动重试</div>';
    }

    var loading = !!data.loading;
    if (loading !== refreshing) {
      refreshing = loading;
      el.btnRefresh.classList.toggle('spin', loading);
      if (loading) el.status.textContent = '正在抓取最新数据…';
    }
  }

  // ---------- 轮询 ----------
  function pull() {
    if (!api) return;
    api.get_data().then(render).catch(function (e) {
      el.status.textContent = '数据通道异常：' + e;
    });
  }

  function tick() {
    pull();
    if (lastStamp) {
      var left = lastStamp + REFRESH_INTERVAL - Math.floor(Date.now() / 1000);
      el.countdown.textContent = left > 0
        ? '下次自动刷新 ' + Math.floor(left / 60) + ' 分钟后'
        : '稍后自动刷新';
    }
  }

  // ---------- 交互 ----------
  function bindCommon() {
    el.btnRefresh.addEventListener('click', function () {
      if (!api || refreshing) return;
      el.btnRefresh.classList.add('spin');
      el.status.textContent = '正在抓取最新数据…';
      api.refresh();
    });
    el.btnMin.addEventListener('click', function () { api && api.hide_window && api.hide_window(); });
    el.btnClose.addEventListener('click', function () { api && api.hide_window && api.hide_window(); });

    // 周期切换（日/周/月）：切换后只重抓 GitHub 仓库，不动资讯
    el.repoSeg.addEventListener('click', function (ev) {
      var btn = ev.target.closest('.seg-btn');
      if (!btn || !api) return;
      var p = btn.getAttribute('data-period');
      if (p === currentPeriod) return;
      setSegActive(p);
      currentPeriod = p;
      el.repoMeta.textContent = '加载中…';
      api.set_repo_period(p);
    });

    // 资讯列表：展开/收起 与 按钮动作
    el.newsList.addEventListener('click', function (ev) {
      var openBtn = ev.target.closest('[data-open]');
      if (openBtn) {
        ev.stopPropagation();
        api.open_url(openBtn.getAttribute('data-open'));
        return;
      }
      var copyBtn = ev.target.closest('[data-copy]');
      if (copyBtn) {
        ev.stopPropagation();
        var url = copyBtn.getAttribute('data-copy');
        copyBtn.textContent = '已复制';
        setTimeout(function () { copyBtn.textContent = '复制链接'; }, 1200);
        if (navigator.clipboard) navigator.clipboard.writeText(url).catch(function () {});
        return;
      }
      var item = ev.target.closest('.news-item');
      if (!item) return;
      var wasOpen = item.classList.contains('open');
      var opened = el.newsList.querySelector('.news-item.open');
      if (opened) opened.classList.remove('open');
      if (!wasOpen) {
        item.classList.add('open');
        if (item.getAttribute('data-short') === '1') requestArticle(item, 8);
      }
    });

    // 仓库列表：点击直达
    el.repoList.addEventListener('click', function (ev) {
      var item = ev.target.closest('.repo-item');
      if (item) api.open_url(item.getAttribute('data-url'));
    });

    document.addEventListener('contextmenu', function (ev) {
      if (!ev.target.closest('.news-detail')) ev.preventDefault();
    });
  }

  // ---------- 全窗口拖拽 ----------
  // 透明分层窗口吃不到系统的 HTCAPTION 拖拽消息（实测无效），所以拖动改为增量移动：
  // 位移超过阈值进入拖动态，之后每帧把光标位移通过 api.move_by() 传给后端 SetWindowPos。
  // 用 rAF 合帧，避免每个 mousemove 都跨一次 JS→Python 桥。
  function bindDrag() {
    var DRAG_THRESHOLD = 5;
    var down = false, dragging = false, sx = 0, sy = 0;
    var accX = 0, accY = 0, lastX = 0, lastY = 0, raf = 0;
    var suppress = false;

    function isInteractive(target) {
      return !!(target.closest && target.closest('.wbtn, .link-btn, .seg'));
    }

    // 落在原生滚动条上的按下要放行，否则滚不动列表
    function isOnScrollbar(ev) {
      var node = ev.target;
      if (!node || !node.classList || !node.classList.contains('list')) return false;
      return ev.offsetX > node.clientWidth;
    }

    function flush() {
      raf = 0;
      if (!dragging || (!accX && !accY)) return;
      var dx = accX, dy = accY;
      accX = 0; accY = 0;
      // screenX/screenY 是 CSS 像素，SetWindowPos 要物理像素，乘上 devicePixelRatio
      var dpr = window.devicePixelRatio || 1;
      api && api.move_by && api.move_by(dx * dpr, dy * dpr);
    }

    document.addEventListener('mousedown', function (ev) {
      if (ev.button !== 0) return;
      if (isInteractive(ev.target) || isOnScrollbar(ev)) return;
      down = true; dragging = false;
      sx = lastX = ev.screenX; sy = lastY = ev.screenY;
    });

    document.addEventListener('mousemove', function (ev) {
      if (!down) return;
      if (!dragging) {
        if (Math.abs(ev.screenX - sx) + Math.abs(ev.screenY - sy) < DRAG_THRESHOLD) return;
        dragging = true;
        document.body.classList.add('dragging');
        lastX = ev.screenX; lastY = ev.screenY;
      }
      accX += ev.screenX - lastX;
      accY += ev.screenY - lastY;
      lastX = ev.screenX; lastY = ev.screenY;
      if (!raf) raf = requestAnimationFrame(flush);
    });

    function endDrag() {
      down = false;
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove('dragging');
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      flush();
      suppress = true;  // 拖完的这次 click 是拖动收尾，吞掉防止误触条目
    }

    document.addEventListener('mouseup', endDrag);
    window.addEventListener('blur', endDrag);
    document.addEventListener('mouseleave', function () {
      if (down && !dragging) down = false;
    });

    document.addEventListener('click', function (ev) {
      if (!suppress) return;
      suppress = false;
      ev.stopPropagation();
      ev.preventDefault();
    }, true);
  }

  var started = false;

  function init() {
    if (started) return;
    started = true;
    api = window.pywebview.api;
    observeLayout();
    bindCommon();
    bindDrag();
    setSegActive(currentPeriod);
    pull();
    setInterval(tick, 2500);
  }

  function boot() {
    // pywebview 的桥接对象在 WebView 加载完成后才注入，脚本首次执行时大概率还没就绪，
    // 所以必须等 pywebviewready 事件，同时保留轮询兜底（事件可能早于脚本注册就已触发过）。
    if (window.pywebview && window.pywebview.api) {
      init();
      return;
    }
    paintAll();
    el.status.textContent = '正在连接桌面容器…';
    window.addEventListener('pywebviewready', init, { once: true });

    var tries = 0;
    (function wait() {
      if (started) return;
      if (window.pywebview && window.pywebview.api) {
        init();
        return;
      }
      if (++tries > 150) {  // 约 30 秒后放弃
        el.status.textContent = '连接失败：请关闭窗口后用 run.bat 重新启动';
        return;
      }
      setTimeout(wait, 200);
    })();
  }

  // 界面轮廓先画出来，别等数据桥
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { boot(); paintAll(); });
  } else {
    boot();
    paintAll();
  }
})();
