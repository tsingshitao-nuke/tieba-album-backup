# -*- coding: utf-8 -*-
"""贴吧真实 CSS 抽取 / 本地化（离线视觉还原用）。

贴吧现网页面通过一堆 <link rel=stylesheet> 引入 CDN 上的 CSS。
备份时把列表页里这些 link 的真实 CSS 抓到本地 `static/tieba_core.css`，
离线 HTML 引用它，就能最大程度还原现网视觉（比 snapshot.FALLBACK_CSS 更准）。

抓不到也不影响可用性：crawler 捕获异常后，snapshot 的内置 fallback 会兜底渲染。
"""
import os
import re
import ssl
import urllib.request
from urllib.parse import urlparse

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0")

CORE_CSS = "tieba_core.css"
STATIC_DIR = "static"

# 一个 <link ...> 标签
_LINK_TAG_RE = re.compile(r"<link\b[^>]*?>", re.I)
# 某个属性值
_ATTR_RE = re.compile(r'\b([a-z-]+)\s*=\s*["\']([^"\']*)["\']', re.I)


def _attr(tag, name):
    for m in _ATTR_RE.finditer(tag):
        if m.group(1).lower() == name.lower():
            return m.group(2)
    return None


def _link_stylesheet_hrefs(html):
    """从 HTML 中抽取所有 <link rel=stylesheet> 且非 alternate/preload 的 href。"""
    hrefs = []
    for tag in _LINK_TAG_RE.findall(html or ""):
        rel = _attr(tag, "rel")
        if not rel or "stylesheet" not in rel.lower():
            continue
        if "alternate" in rel.lower():
            continue
        href = _attr(tag, "href")
        if href:
            hrefs.append(href)
    return hrefs


def download_text(url, timeout=8):
    """下载文本资源（CSS），utf-8 / gb18030 / gbk 解码兜底。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": "https://tieba.baidu.com/",
        "Accept": "text/css,*/*;q=0.1",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _abs_url(href, base_url):
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return base_url.rstrip("/") + href
    # 相对路径（少见，但兜底）
    return base_url.rstrip("/") + "/" + href.lstrip("/")


def extract_link_css(list_html, out_dir, base_url="https://tieba.baidu.com/", log=None):
    """抽取列表页所有贴吧样式表并合并到 <out_dir>/static/tieba_core.css。

    返回相对 out_dir 的路径（"static/tieba_core.css"），失败时返回 None。
    """
    log = log or (lambda m: None)
    hrefs = _link_stylesheet_hrefs(list_html)
    if not hrefs:
        log("列表页未找到 <link rel=stylesheet>，将用内置 fallback 样式。")
        return None

    # 去重（同一 CSS 可能多次引用）
    seen, abs_urls = set(), []
    for h in hrefs:
        u = _abs_url(h, base_url)
        if u in seen:
            continue
        seen.add(u)
        abs_urls.append(u)

    target_dir = os.path.join(out_dir, STATIC_DIR)
    os.makedirs(target_dir, exist_ok=True)
    out_path = os.path.join(target_dir, CORE_CSS)

    parts, ok = [], 0
    for u in abs_urls:
        try:
            txt = download_text(u)
        except Exception as e:
            log("CSS 下载失败 %s：%s" % (u, e))
            continue
        if txt and txt.strip():
            parts.append("/* ===== %s ===== */\n%s" % (u, txt))
            ok += 1

    if not parts:
        log("所有贴吧 CSS 均下载失败，将用内置 fallback 样式。")
        return None

    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write("\n\n".join(parts))
    log("已本地化 %d 个贴吧 CSS 文件 → %s" % (ok, os.path.join(STATIC_DIR, CORE_CSS)))
    return os.path.join(STATIC_DIR, CORE_CSS)
