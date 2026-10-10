"""AI 雷达 — Windows 桌面小组件。

上半区：国内外最新 AI 资讯（每 1 小时自动刷新，可手动刷新，点击展开详情）
下半区：GitHub 热门项目 Top10（日/周/月可切换，点击直达浏览器）

运行方式：pythonw app.py 或双击 run.bat
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import webview

import fetcher

BASE_DIR = Path(__file__).resolve().parent
UI_FILE = BASE_DIR / "ui" / "index.html"
LOG_FILE = BASE_DIR / "widget.log"

WINDOW_TITLE = "AI Radar · AI 雷达"
WINDOW_WIDTH = 428
WINDOW_HEIGHT = 872
REFRESH_INTERVAL_SEC = 3600  # 1 小时
ICON_FILE = BASE_DIR / "app.ico"

# 测试/调试用：设为 1 则启动时直接显示窗口（默认收起进托盘）
_START_VISIBLE = os.environ.get("AI_RADAR_START_VISIBLE") == "1"

_TRAY: dict = {}  # 托盘对象引用，防止被 GC 回收导致图标失效
_GLASS: "WindowShape | None" = None  # 圆角效果实例，需全局持有防止回调被回收
_TRAY_OBJ: "Tray | None" = None      # 托盘实例，同样防止被 GC

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)


def find_hwnd(title: str):
    """按窗口标题枚举顶层窗口，拿到本机窗口句柄。找不到返回 None。"""
    try:
        import ctypes
        from ctypes import wintypes

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32 = ctypes.windll.user32
        hits = []

        def _cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == title or title in buf.value:
                    hits.append(hwnd)
            return True

        user32.EnumWindows(EnumWindowsProc(_cb), 0)
        return hits[0] if hits else None
    except Exception:
        return None


class WindowHandle:
    """窗口句柄缓存与原生窗口操作。

    HTCAPTION 系统拖拽在透明分层窗口上实测无效，因此拖动改为：
    前端每次把光标位移传给 move_by()，这里直接用 SetWindowPos 移动窗口。
    """

    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010

    def __init__(self, title: str) -> None:
        self.title = title
        self._hwnd = None

    def hwnd(self):
        """拿当前句柄；窗口被重建过（旧句柄失效）会自动重新查找。"""
        import ctypes

        if self._hwnd and ctypes.windll.user32.IsWindow(self._hwnd):
            return self._hwnd
        self._hwnd = find_hwnd(self.title)
        return self._hwnd

    def move_by(self, dx: float, dy: float) -> bool:
        """把窗口移动 dx/dy 物理像素。由前端拖拽逻辑高频调用。"""
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = self.hwnd()
            if not hwnd:
                return False
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0,
                int(rect.left + dx), int(rect.top + dy), 0, 0,
                self.SWP_NOSIZE | self.SWP_NOZORDER | self.SWP_NOACTIVATE,
            )
            return True
        except Exception:
            return False


class WindowShape:
    """窗口圆角兜底。

    主体透明由 pywebview 的 transparent=True 提供：WebView2 背景透明 +
    WinForms SupportsTransparentBackColor，壁纸直接透过半透明的 CSS 面板显示。
    这里只负责在系统层面把窗口物理轮廓切圆，作为 CSS 圆角之外的双保险——
    即使 WebView2 透明在某台机器上失效，窗口也不会是一张生硬的直角。

      Win11 : DWMWA_WINDOW_CORNER_PREFERENCE 让 DWM 直接把窗口切圆
      Win10 : 用 SetWindowRgn 裁出圆角矩形顶替
    """

    DWMWA_WINDOW_CORNER_PREFERENCE = 33
    DWMWCP_ROUND = 2
    CORNER_RADIUS = 26  # 与 style.css 的 --r-card 保持一致

    def __init__(self, title: str) -> None:
        self.title = title
        self.applied = False

    # ---------- 对外入口 ----------

    def start(self) -> None:
        """窗体由 pywebview 异步创建，这里在后台轮询直到句柄出现再上效果。"""

        def _worker():
            for _ in range(40):
                hwnd = find_hwnd(self.title)
                if hwnd:
                    try:
                        self.apply(hwnd)
                        return
                    except Exception:
                        logging.exception("圆角效果应用失败")
                        return
                time.sleep(0.25)

        threading.Thread(target=_worker, daemon=True).start()

    def apply(self, hwnd) -> None:
        import sys

        build = sys.getwindowsversion().build
        if build >= 22000:
            self._set(hwnd, self.DWMWA_WINDOW_CORNER_PREFERENCE, self.DWMWCP_ROUND)
        else:
            self._set_window_rgn(hwnd)
        self.applied = True
        logging.info("圆角效果已应用（Windows build %s）", build)

    # ---------- 内部实现 ----------

    @staticmethod
    def _set(hwnd, attribute: int, value: int) -> bool:
        try:
            import ctypes

            ctypes.windll.dwmapi.DwmSetWindowAttribute.argtypes = (
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t
            )
            value = ctypes.c_int(value)
            return ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), ctypes.c_uint(attribute),
                ctypes.byref(value), ctypes.sizeof(value)
            ) == 0
        except Exception:
            return False

    def _set_window_rgn(self, hwnd) -> bool:
        """Win10 的系统圆角方案：用 GDI 圆角矩形直接裁切窗口。"""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w <= 0 or h <= 0:
                return False
            d = 4 * self.CORNER_RADIUS  # CreateRoundRectRgn 收的是椭圆直径
            region = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, d, d)
            return user32.SetWindowRgn(hwnd, region, True) != 0
        except Exception:
            return False


class Tray:
    """托盘图标：双击显示窗口，右键菜单（显示 / 窗口置顶 / 退出）。

    NotifyIcon 必须在 GUI 线程创建，所以先拿到 WinForms 窗体，再 BeginInvoke 过去执行。
    实例与图标对象都挂在全局 _TRAY 上，被 GC 回收图标会直接消失。
    """

    def __init__(self, window) -> None:
        self.window = window
        self.form = None
        self.ni = None
        self.state = {"topmost": False}
        threading.Thread(target=self._wait_form, daemon=True).start()

    def _wait_form(self) -> None:
        import time

        from webview.platforms import winforms

        for _ in range(80):
            try:
                form = winforms.BrowserView.instances.get(self.window.uid)
            except Exception:
                form = None
            if form is not None:
                self._install(form)
                return
            time.sleep(0.25)
        logging.error("等待窗体超时，托盘未创建")

    def _install(self, form) -> None:
        self.form = form
        try:
            from System import Action

            form.BeginInvoke(Action(self._on_gui))
        except Exception:
            logging.exception("托盘调度失败")

    def _on_gui(self) -> None:
        from System.Drawing import Icon, SystemIcons
        from System.Windows.Forms import ContextMenuStrip, NotifyIcon, ToolStripMenuItem

        form = self.form
        self._hide_from_taskbar(form)

        ni = NotifyIcon()
        try:
            ni.Icon = Icon(str(ICON_FILE))
        except Exception:
            ni.Icon = SystemIcons.Application
        ni.Text = "AI 雷达 · AI 资讯桌面组件"
        ni.Visible = True

        menu = ContextMenuStrip()
        mi_show = ToolStripMenuItem("显示窗口")
        mi_show.Click += self._on_show
        mi_top = ToolStripMenuItem("窗口置顶")
        mi_top.CheckOnClick = True
        mi_top.CheckedChanged += self._on_topmost
        mi_quit = ToolStripMenuItem("退出")
        mi_quit.Click += self._on_quit
        for it in (mi_show, mi_top, mi_quit):
            menu.Items.Add(it)
        ni.ContextMenuStrip = menu
        ni.DoubleClick += self._on_show

        self.ni = ni
        _TRAY["icon"] = ni
        _TRAY["notify"] = self
        logging.info("托盘图标已创建")

    # ---------- 事件（都在 GUI 线程执行） ----------

    @staticmethod
    def _hide_from_taskbar(form) -> None:
        """任务栏和 Alt-Tab 都不出现。

        注意不能用 form.ShowInTaskbar = False——它会重建窗体句柄，
        把正在初始化的 WebView2 控制器直接中止（E_ABORT）。改用窗口样式：
        去掉 WS_EX_APPWINDOW、加上 WS_EX_TOOLWINDOW，句柄不变、立即生效。
        """
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = int(form.Handle.ToInt64())  # Handle 是 .NET IntPtr，不能直接 int()
            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
            )
        except Exception:
            logging.exception("隐藏任务栏图标失败")

    def _on_show(self, sender=None, event=None) -> None:
        try:
            self.window.show()
        except Exception:
            logging.exception("显示窗口失败")

    def _on_topmost(self, sender, event) -> None:
        self.state["topmost"] = bool(sender.Checked)
        try:
            self.form.TopMost = self.state["topmost"]
        except Exception:
            logging.exception("置顶切换失败")

    def _on_quit(self, sender, event) -> None:
        try:
            self.ni.Visible = False
            self.ni.Dispose()
        except Exception:
            pass
        _TRAY.clear()
        try:
            self.window.destroy()
        except Exception:
            pass

    def show_balloon(self, title: str, text: str) -> None:
        if self.ni is None or self.form is None:
            return

        def _show():
            try:
                from System.Windows.Forms import ToolTipIcon

                self.ni.ShowBalloonTip(1200, title, text, ToolTipIcon.Info)
            except Exception:
                pass

        try:
            from System import Action

            self.form.BeginInvoke(Action(_show))
        except Exception:
            pass


class Backend:
    """暴露给前端的 Python 接口，同时负责后台定时刷新。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loading = False
        self._error: str | None = None
        self._window = None
        self._win = WindowHandle(WINDOW_TITLE)
        self._articles = fetcher.load_article_cache()
        self._pending_articles = set()
        self._balloon_shown = False
        cached = fetcher.read_cache()
        period = cached.get("repo_period", "weekly")
        self._repo_period = period if period in ("daily", "weekly", "monthly") else "weekly"
        with self._lock:
            self._data = cached
        logging.info("缓存载入，新闻 %s 条 / GitHub 周期 %s",
                     len(self._data.get("news", [])), self._repo_period)
        threading.Thread(target=self._auto_loop, daemon=True).start()

    # ---------- 给 JS 调用 ----------

    def get_data(self) -> dict:
        """前端轮询入口：O(1) 返回内存中的最新数据，绝不阻塞 UI。"""
        with self._lock:
            data = dict(self._data)
            loading = self._loading
            error = self._error
        data["loading"] = loading
        data["error"] = error
        return data

    def refresh(self) -> dict:
        """手动刷新：立即返回，抓取在后台线程完成。"""
        self._start_fetch()
        return {"started": True}

    def set_repo_period(self, period: str) -> dict:
        """切换 GitHub 板块的周期（日/周/月），只重抓仓库，不动资讯。"""
        if period not in ("daily", "weekly", "monthly"):
            return {"ok": False, "error": "invalid period"}
        if period == self._repo_period and self._data.get("repos"):
            return {"ok": True, "period": period, "unchanged": True}
        self._repo_period = period
        self._start_fetch_repos()
        return {"ok": True, "period": period}

    def _start_fetch_repos(self) -> None:
        """只重抓 GitHub 仓库（周期切换用），不触发资讯抓取与翻译。"""
        threading.Thread(target=self._fetch_repos_worker, args=(self._repo_period,), daemon=True).start()

    def _fetch_repos_worker(self, period: str) -> None:
        try:
            config = fetcher.load_config()
            repos, repo_source = fetcher.fetch_repos_full(config, period)
            with self._lock:
                d = dict(self._data)
                if repos:
                    d["repos"] = repos
                    d["repo_source"] = repo_source
                d["repo_period"] = period
                d["updated_at"] = int(time.time())
                self._data = d
                if repos:
                    fetcher.write_cache(d)
            logging.info("仓库刷新完成(period=%s): %s 个 / 来源 %s",
                         period, len(repos), repo_source)
        except Exception:  # noqa: BLE001
            logging.error("仓库刷新失败: %s", traceback.format_exc())

    def open_url(self, url: str) -> dict:
        webbrowser.open_new_tab(url or "https://github.com")
        return {"ok": True}

    def hide_window(self) -> dict:
        """收起到托盘（✕ 和最小化按钮都走这里，退出只在托盘菜单）。"""
        if self._window:
            try:
                self._window.hide()
            except Exception:
                pass
        self._balloon_once("已收起到托盘", "双击托盘图标重新打开窗口")
        return {"ok": True}

    def move_by(self, dx: float, dy: float) -> dict:
        """拖拽：前端把光标位移（已乘 devicePixelRatio）传过来，逐帧移动窗口。"""
        return {"ok": self._win.move_by(dx, dy)}

    def _balloon_once(self, title: str, text: str) -> None:
        """托盘气泡提示；同一会话只弹一次，避免打扰。"""
        if self._balloon_shown or not _TRAY.get("icon"):
            return
        self._balloon_shown = True
        _TRAY["notify"].show_balloon(title, text)

    def quit(self) -> None:
        if self._window:
            try:
                self._window.destroy()
            except Exception:
                pass

    def minimize(self) -> None:
        # 兼容旧调用：现在最小化即收起到托盘
        self.hide_window()

    def get_article(self, url: str) -> dict:
        """按需读取某条资讯的原文正文。

        首次调用返回 pending 并在后台抓取，之后轮询同一接口取结果，避免阻塞 UI 线程。
        """
        if not url:
            return {"state": "empty", "text": ""}
        if url in self._articles:
            return {"state": "done", "text": self._articles[url]}
        if url in self._pending_articles:
            return {"state": "pending", "text": ""}

        self._pending_articles.add(url)
        threading.Thread(target=self._article_worker, args=(url,), daemon=True).start()
        return {"state": "pending", "text": ""}

    def _article_worker(self, url: str) -> None:
        text = ""
        try:
            text = fetcher.fetch_article_text(url)
        finally:
            self._articles[url] = text
            self._pending_articles.discard(url)
            threading.Thread(target=fetcher.save_article_cache,
                             args=(dict(self._articles),), daemon=True).start()

    # ---------- 内部实现 ----------

    def bind_window(self, window) -> None:
        self._window = window

    def _set_loading(self, value: bool) -> None:
        with self._lock:
            self._loading = value

    def _start_fetch(self) -> None:
        with self._lock:
            if self._loading:
                return
            self._loading = True
            self._error = None
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self) -> None:
        try:
            payload = fetcher.collect(self._repo_period)
            with self._lock:
                self._data = payload
                self._error = None
            logging.info("刷新完成：新闻 %s 条 / 仓库 %s 个 / 来源 %s",
                         len(payload.get("news", [])),
                         len(payload.get("repos", [])),
                         payload.get("repo_source"))
        except Exception as exc:  # noqa: BLE001
            logging.error("刷新失败: %s", traceback.format_exc())
            with self._lock:
                self._error = str(exc) or repr(exc)
        finally:
            self._set_loading(False)

    def _auto_loop(self) -> None:
        """启动后立刻尝试刷新一次，此后每 1 小时自动刷新。"""
        self._start_fetch()
        while True:
            time.sleep(REFRESH_INTERVAL_SEC)
            self._start_fetch()


def window_position() -> tuple[int, int]:
    """默认停靠在屏幕右侧，留出 24px 边距。"""
    try:
        import ctypes
        width = ctypes.windll.user32.GetSystemMetrics(0)
        return max(0, width - WINDOW_WIDTH - 24), 60
    except Exception:
        return 100, 60


def main() -> None:
    global _GLASS, _TRAY_OBJ

    backend = Backend()
    if not UI_FILE.exists():
        raise FileNotFoundError(f"界面文件缺失: {UI_FILE}")

    # 部分环境（安全沙箱/杀软钩子）会让 WebView2 的渲染子进程起不来，
    # 表现为窗口白屏或 msedgewebview2 崩溃循环；关掉 Chromium 自身沙箱即可规避。
    # 本组件只加载本地 UI 和远程图文，不执行不可信代码，风险可接受。
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--no-sandbox")

    x, y = window_position()
    window = webview.create_window(
        title=WINDOW_TITLE,
        url=UI_FILE.as_uri(),
        js_api=backend,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        x=x,
        y=y,
        resizable=True,
        frameless=True,
        easy_drag=False,  # 拖拽由前端增量移动实现（HTCAPTION 在透明窗口上无效）
        on_top=False,     # 不常驻置顶；托盘菜单里可随时切换
        shadow=True,
        transparent=True,  # 逐像素透明：壁纸透过 CSS 半透明面板直接可见（DeskGo 同款机制）
        text_select=True,
        min_size=(340, 480),
        hidden=not _START_VISIBLE,  # 默认启动即收起到托盘
    )
    backend.bind_window(window)

    # 托盘：双击打开 / 右键菜单；实例挂全局引用防 GC
    _TRAY_OBJ = Tray(window)

    # 默认「启动即收起到托盘」。注意 pywebview 对透明窗口会在页面开始导航时
    # 强制 Show+Activate（透明窗口的兼容逻辑，无视 hidden 参数），
    # 所以只能在首页加载完成后再收起，而不是创建时就 hidden。
    if not _START_VISIBLE:
        def _auto_hide():
            try:
                if not window.events.loaded.wait(timeout=20):
                    logging.warning("首页加载超时，仍尝试收起到托盘")
                time.sleep(0.8)  # 留一点渲染时间，避免用户看到半成品页面
                window.hide()
                if _TRAY_OBJ and _TRAY_OBJ.ni is not None:
                    _TRAY_OBJ.show_balloon(
                        "AI 雷达正在后台运行",
                        "已收起到托盘：双击图标打开窗口，右键图标可退出",
                    )
                logging.info("已自动收起到托盘")
            except Exception:
                logging.exception("自动收起失败")

        threading.Thread(target=_auto_hide, daemon=True).start()

    # 圆角必须在窗体句柄出现之后才能设置，交给后台线程等窗口就绪
    _GLASS = WindowShape(WINDOW_TITLE)
    _GLASS.start()

    webview.start(debug=False)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        logging.critical("启动失败: %s", traceback.format_exc())
        sys.exit(1)
