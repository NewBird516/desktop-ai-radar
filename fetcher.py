"""数据抓取：AI 资讯 RSS + GitHub 热门仓库（日/周/月可选）。

设计原则：
1. 所有网络请求都带超时 + 异常兜底，任何单个源头失败都不影响整体。
2. GitHub 趋势优先抓官方 trending 页面（真·日/周/月最火），失败降级到 Search API。
3. 结果落盘缓存到 cache.json，冷启动时先读缓存保证秒开。
"""

from __future__ import annotations

import calendar
import concurrent.futures as futures
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "sources.json")
CACHE_PATH = os.path.join(BASE_DIR, "cache.json")
TRANSLATION_CACHE_PATH = os.path.join(BASE_DIR, "translation_cache.json")
ARTICLE_CACHE_PATH = os.path.join(BASE_DIR, "article_cache.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

REQUEST_TIMEOUT = 12
MAX_NEWS_PER_SOURCE = 12
MAX_NEWS_TOTAL = 60
MAX_REPOS = 10
# 每次刷新最多新翻译的条目数：免费机翻接口有日额度，超出的保持英文原文。
# 条目内容是缓慢滚动的，绝大多数标题/描述第二次命中缓存，实际每轮真正调用只有新增的那几条。
MAX_REPOS_TRANSLATE = 10
MAX_NEWS_TRANSLATE = 40


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def strip_html(raw: str) -> str:
    """把一段富文本转成干净的纯文本。"""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def entry_timestamp(entry) -> int:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return calendar.timegm(value)
            except Exception:
                continue
    return 0


def normalize_timestamp(ts: int, now: int) -> int:
    """把明显错误的未来时间戳拉回真实区间。

    不少中文站点把本地时间直接写进 RSS，却不带时区偏移，解析后会凭空多出几个小时，
    导致条目在按时间排序时永远浮在顶部。这里按常见的 8/7/5/4 小时偏移逐档回落。
    """
    if ts <= now + 300 or ts <= 0:
        return ts
    for hours in (8, 7, 5, 4):
        candidate = ts - hours * 3600
        if candidate <= now + 300:
            return max(candidate, 1)
    return ts


def match_ai(text: str, keywords: list[str]) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in keywords)


# ----------------------------------------------------------------------------
# AI 资讯
# ----------------------------------------------------------------------------

def fetch_single_source(source: dict, keywords: list[str]) -> list[dict]:
    """抓单个 RSS/Atom 源，返回标准化条目列表。失败返回空列表。"""
    out: list[dict] = []
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return out
        feed = feedparser.parse(resp.content)
    except Exception:
        return out

    if feed.bozo and not getattr(feed, "entries", None):
        return out

    for entry in feed.entries[:MAX_NEWS_PER_SOURCE]:
        title = strip_html(entry.get("title", "")) or "(无标题)"
        link = entry.get("link", "") or ""
        if not link:
            continue

        summary = ""
        if entry.get("summary"):
            summary = strip_html(entry["summary"])
        if not summary and entry.get("content"):
            try:
                summary = strip_html(entry["content"][0].value)
            except Exception:
                summary = ""

        haystack = f"{title} {summary}"
        if not source.get("ai_vertical") and not match_ai(haystack, keywords):
            continue

        out.append({
            "title": clip(title, 140),
            "url": link,
            "source": source["name"],
            "region": source.get("region", "en"),
            "ts": entry_timestamp(entry),
            "summary": clip(summary, 500),
            "author": strip_html(entry.get("author", ""))[:40],
        })
    return out


def fetch_news(config: dict) -> list[dict]:
    """并发抓取所有资讯源，去重后按发布时间倒序。"""
    keywords = config.get("ai_keywords", [])
    items: list[dict] = []

    with futures.ThreadPoolExecutor(max_workers=min(10, len(config["sources"]) or 1)) as pool:
        tasks = [pool.submit(fetch_single_source, s, keywords) for s in config["sources"]]
        for task in tasks:
            try:
                items.extend(task.result(timeout=REQUEST_TIMEOUT + 8))
            except Exception:
                continue

    seen_title, seen_url = set(), set()
    unique: list[dict] = []
    for item in items:
        key_t = re.sub(r"\s+", "", item["title"]).lower()
        key_u = item["url"].split("#")[0].rstrip("/")
        if key_t in seen_title or key_u in seen_url:
            continue
        seen_title.add(key_t)
        seen_url.add(key_u)
        unique.append(item)

    # 极少数源不返回日期，用抓取时间兜底，避免永远沉底
    now = int(time.time())
    for i in unique:
        i["ts"] = normalize_timestamp(i["ts"], now) or now

    unique.sort(key=lambda x: x["ts"], reverse=True)
    return unique[:MAX_NEWS_TOTAL]


# ----------------------------------------------------------------------------
# GitHub 热门仓库（日 / 周 / 月 可选）
# ----------------------------------------------------------------------------

PERIODS = ("daily", "weekly", "monthly")
PERIOD_LABEL = {"daily": "今日", "weekly": "本周", "monthly": "本月"}
PERIOD_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}
# 趋势页对三种周期分别展示「stars today / this week / this month」，分别配对正则
_DELTA_RE = {
    "daily": re.compile(r"([\d,]+)\s*stars?\s+today", re.I),
    "weekly": re.compile(r"([\d,]+)\s*stars?\s+this\s+week", re.I),
    "monthly": re.compile(r"([\d,]+)\s*stars?\s+this\s+month", re.I),
}


def _int(text: str) -> int:
    try:
        return int(re.sub(r"[^\d]", "", text or "") or 0)
    except ValueError:
        return 0


def repos_from_trending_page(period: str = "weekly") -> tuple[list[dict], str]:
    """抓 github.com/trending?since=<period>，官方口径的日/周/月最火。"""
    since = period if period in PERIODS else "weekly"
    url = f"https://github.com/trending?since={since}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"trending page status {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("article.Box-row")
    if not rows:
        raise RuntimeError("trending page has no rows (layout changed?)")

    repos: list[dict] = []
    for art in rows[:MAX_REPOS]:
        anchor = art.select_one("h2 a")
        if not anchor:
            continue
        full_name = (anchor.get("href") or "").strip("/")
        if not full_name or "/" not in full_name:
            continue

        desc_node = art.select_one("p.col-9")
        desc = desc_node.get_text(" ", strip=True) if desc_node else ""

        lang_node = art.select_one("[itemprop='programmingLanguage']")
        lang = lang_node.get_text(strip=True) if lang_node else ""

        stars_node = art.select_one("a[href$='/stargazers']")
        stars = _int(stars_node.get_text(strip=True)) if stars_node else 0

        delta = 0
        span = art.select_one("span.d-inline-block.float-sm-right")
        if span:
            match = _DELTA_RE[since].search(span.get_text(" ", strip=True))
            delta = _int(match.group(1)) if match else 0

        repos.append({
            "full_name": full_name,
            "url": f"https://github.com/{full_name}",
            "desc": clip(desc, 110),
            "lang": lang,
            "stars": stars,
            "delta": delta,
            "delta_period": since,
        })

    if not repos:
        raise RuntimeError("trending parsed 0 repos")
    return repos, f"GitHub Trending ({PERIOD_LABEL.get(since, since)})"


def repos_from_search_api(period: str = "weekly") -> tuple[list[dict], str]:
    """降级通道：近 N 天（日/周/月）新建且星数最高的仓库。

    趋势页不可用时退而求其次，口径不同但同样客观可用。
    """
    days = PERIOD_DAYS.get(period, 7)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    url = "https://api.github.com/search/repositories"
    params = {"q": f"created:>{since}", "sort": "stars", "order": "desc", "per_page": MAX_REPOS}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"search api status {resp.status_code}")

    repos: list[dict] = []
    for item in (resp.json() or {}).get("items", [])[:MAX_REPOS]:
        repos.append({
            "full_name": item.get("full_name", ""),
            "url": item.get("html_url", ""),
            "desc": clip(item.get("description") or "暂无描述", 110),
            "lang": item.get("language") or "",
            "stars": item.get("stargazers_count", 0),
            "delta": 0,
            "delta_period": period,
        })
    if not repos:
        raise RuntimeError("search api returned 0 repos")
    return repos, f"GitHub Search API (近{PERIOD_LABEL.get(period, period)}新星)"


def fetch_repos(period: str = "weekly") -> tuple[list[dict], str]:
    for getter in (repos_from_trending_page, repos_from_search_api):
        try:
            repos, label = getter(period)
            repos = [r for r in repos if r.get("full_name") and r.get("url")]
            if repos:
                return repos, label
        except Exception:
            continue
    return [], "暂时不可用"


def fetch_repos_full(config: dict, period: str = "weekly") -> tuple[list[dict], str]:
    """抓取 + 补中文描述，返回 (repos, source)。

    整包刷新与单独切换周期共用，避免两套逻辑 drift。
    """
    repos, repo_source = fetch_repos(period)
    if config.get("translate_desc", True):
        repos = attach_translations(repos)
    return repos, repo_source


# ----------------------------------------------------------------------------
# 英文描述的中文翻译
# ----------------------------------------------------------------------------

TRANSLATION_CACHE_MAX = 1200
_ZH_CHAR = re.compile(r"[\u4e00-\u9fff]")


def load_translation_cache() -> dict:
    try:
        with open(TRANSLATION_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_translation_cache(cache: dict) -> None:
    try:
        with open(TRANSLATION_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(dict(list(cache.items())[-TRANSLATION_CACHE_MAX:]), f,
                      ensure_ascii=False, indent=1)
    except Exception:
        pass


def looks_chinese(text: str) -> bool:
    """中文占比超过 25% 就认为本来就是中文，不必再翻译。"""
    if not text:
        return False
    return len(_ZH_CHAR.findall(text)) / len(text) > 0.25


def _translate_mymemory(text: str) -> str:
    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": "en|zh-CN"},
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json() or {}
        out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
        if not out or "mymemory warning" in out.lower():
            return ""
        if out.lower() == text.lower():
            return ""
        return html.unescape(out)
    except Exception:
        return ""


def _translate_google(text: str) -> str:
    """Google 的免费 gtx 接口，网络能通的时候质量最好，作为第二通道。"""
    try:
        resp = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text},
            headers=HEADERS,
            timeout=12,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        segments = data[0] if isinstance(data, list) and data else []
        return "".join(s[0] for s in segments if s and s[0]).strip()
    except Exception:
        return ""


_TRANSLATORS = (_translate_mymemory, _translate_google)


def translate_en2zh(text: str) -> str:
    """逐个尝试翻译通道，全部失败返回空串（调用方会退回英文原文）。"""
    for translator in _TRANSLATORS:
        result = translator(text)
        if result and result.strip() and result.strip().lower() != text.strip().lower():
            return result.strip()
    return ""


def translate_batch(items: list[dict], field: str, out_field: str, limit: int,
                    workers: int = 2) -> list[dict]:
    """把 items 里属于英文的 field 字段翻译成中文，写回 out_field。

    命中缓存不消耗任何额度；失败的条目不写死结果，下次刷新仍会重试。
    免费接口的短窗口速率限制很敏感，这里刻意压低并发并一轮重试以提高首屏覆盖率。
    """
    cache = load_translation_cache()
    pending: dict[str, tuple[dict, object]] = {}

    def run(pool, queue: list[tuple[str, str]]) -> dict[str, str]:
        """并发翻译一批文本，返回 {key: 译文}，失败的 key 落在 results 之外。"""
        tasks: dict[str, object] = {}
        for key, text in queue:
            tasks[key] = pool.submit(translate_en2zh, text)
            time.sleep(0.3)  # 轻踩刹车，避免触发免费接口的短窗口限流
        results: dict[str, str] = {}
        for key, task in tasks.items():
            try:
                translated = (task.result(timeout=30) or "").strip()
            except Exception:
                translated = ""
            if translated:
                results[key] = translated
        return results

    cache = load_translation_cache()
    queue: list[tuple[str, str]] = []

    for item in items:
        text = (item.get(field) or "").strip()
        if not text or looks_chinese(text):
            continue
        key = "t:" + hashlib.md5(text.encode("utf-8")).hexdigest()
        cached = cache.get(key)
        if isinstance(cached, str) and cached:
            item[out_field] = cached
            continue
        if len(queue) >= limit:
            continue
        queue.append((key, text))

    if not queue:
        return items

    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        translated = run(pool, queue)
        # 未成功的条目再试一轮：绝大多数失败源于瞬时限流，稍等片刻通常就能通过
        retry = [pair for pair in queue if pair[0] not in translated]
        if retry:
            time.sleep(1.2)
            translated.update(run(pool, retry))

    changed = False
    for key, _ in queue:
        value = translated.get(key)
        if value:
            cache[key] = value
            changed = True
        elif cache.get(key) is None:
            cache[key] = ""
            changed = True

    if changed:
        save_translation_cache(cache)

    # 回填：按文本内容把命中缓存的译文写回条目
    for item in items:
        text = (item.get(field) or "").strip()
        if not text:
            continue
        value = cache.get("t:" + hashlib.md5(text.encode("utf-8")).hexdigest())
        if isinstance(value, str) and value:
            item[out_field] = value

    return items


def attach_translations(repos: list[dict]) -> list[dict]:
    """给仓库描述补上 desc_zh 字段。"""
    return translate_batch(repos, "desc", "desc_zh", MAX_REPOS_TRANSLATE)


def attach_news_translations(news: list[dict]) -> list[dict]:
    """把国外资讯的英文标题译为中文，写回 title_zh，中文源条目跳过。

    只翻译 region 标记为 en 的条目，避免把国内源的标题再送去机翻绕一圈。
    """
    targets = [n for n in news if n.get("region") == "en"]
    return translate_batch(targets, "title", "title_zh", MAX_NEWS_TRANSLATE)


# ----------------------------------------------------------------------------
# 原文正文抓取（点击某条资讯时按需拉取）
# ----------------------------------------------------------------------------

ARTICLE_LIMIT = 1400
ARTICLE_CACHE_MAX = 200

_NOISE_TAGS = (
    "script", "style", "noscript", "header", "footer", "nav", "aside",
    "form", "iframe", "svg", "button", "figure", "template",
)


def load_article_cache() -> dict:
    try:
        with open(ARTICLE_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_article_cache(cache: dict) -> None:
    try:
        trimmed = dict(list(cache.items())[-ARTICLE_CACHE_MAX:])
        with open(ARTICLE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def fetch_article_text(url: str) -> str:
    """尽力提取文章正文纯文本，抓不到就返回空串（调用方会给出重试/降级提示）。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return ""
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype:
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(list(_NOISE_TAGS)):
            tag.decompose()

        node = soup.find("article") or soup.find("main") or soup.body or soup
        paragraphs: list[str] = []
        seen: set[str] = set()
        for p in node.find_all("p"):
            text = p.get_text(" ", strip=True)
            if len(text) < 16:
                continue
            key = text[:40]
            if key in seen:
                continue
            seen.add(key)
            paragraphs.append(text)
            if sum(len(x) for x in paragraphs) > ARTICLE_LIMIT * 1.6:
                break

        if not paragraphs:
            return ""
        return clip("\n\n".join(paragraphs), ARTICLE_LIMIT)
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# 缓存
# ----------------------------------------------------------------------------

def read_cache() -> dict:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_cache(payload: dict) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def collect(period: str = "weekly") -> dict:
    """一次性抓齐两部分数据，失败时自动回填缓存内容。

    period 决定 GitHub 板块按「日 / 周 / 月」哪个周期拉取趋势榜。
    """
    config = load_config()
    news = fetch_news(config)
    repos, repo_source = fetch_repos_full(config, period)

    if config.get("translate_title", True):
        attach_news_translations(news)

    cached = read_cache()
    if not news:
        news = cached.get("news", [])
    if not repos:
        repos = cached.get("repos", [])
        repo_source = cached.get("repo_source", repo_source)

    payload = {
        "news": news,
        "repos": repos,
        "repo_source": repo_source,
        "repo_period": period,
        "updated_at": int(time.time()),
        "from_cache": not news and not repos,
    }
    write_cache(payload)
    return payload
