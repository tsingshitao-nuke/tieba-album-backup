# -*- coding: utf-8 -*-
"""相册列表页解析。

实测要点（务必保留）：
- 相册列表页把数据包在 `<!-- -->` **注释里**，必须「揭开注释」——即把注释标记去掉、
  让内部标签变成真实 HTML，而**不是**删除注释内容。
- 注释片段里常见用 `#` 充当属性引号（`href=#/p/123#`），需 `#`→`"` 再解析一次。
- 相册名与链接必须**按 grbm_ele_wrapper 成对提取**；旧版分别取 names[i] / links[i]
  按下标配对，一旦某条缺失就会整体错位。
"""
import re
from urllib.parse import quote

from . import ALBUM_LIST_URL

_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.S)


def strip_html_comments(text):
    """揭开贴吧包裹相册数据的注释（去掉注释标记，保留内容）。"""
    return _COMMENT_RE.sub(r"\1", text or "")


def _tid_from_href(href):
    m = re.search(r"/p/(\d+)", href or "")
    return m.group(1) if m else ""


def _clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def _parse_with_lxml(html_text):
    """按 grbm_ele_wrapper 成对提取 (相册名, tid, href)。"""
    try:
        from lxml import html as lxhtml
    except Exception:
        return []
    try:
        # 必须显式指定 parser 编码：page.content() 返回的是已解码的 str，
        # 这里再编码回 utf-8 交给 lxml 时，若没有 meta charset，lxml 会按
        # Latin-1 猜，导致中文相册名变成乱码。
        parser = lxhtml.HTMLParser(encoding="utf-8")
        tree = lxhtml.fromstring((html_text or "").encode("utf-8", "ignore"), parser=parser)
    except Exception:
        return []

    out = []
    for w in tree.xpath('//div[contains(@class,"grbm_ele_wrapper")]'):
        href = ""
        hrefs = w.xpath('.//a[contains(@class,"grbm_ele_big")]/@href')
        if hrefs:
            href = hrefs[0]
        else:
            for h in w.xpath(".//a/@href"):
                if "/p/" in h or "/photo/" in h:
                    href = h
                    break
        tid = _tid_from_href(href)
        if not tid:
            continue
        name = "".join(w.xpath('.//div[contains(@class,"grbm_ele_title")]//text()'))
        name = _clean(name)
        if not name:
            name = _clean("".join(w.xpath('.//a[contains(@class,"grbm_ele_big")]//@title')))
        if not name:
            name = _clean("".join(w.xpath('.//img/@alt')))
        out.append((name, tid, href))
    return out


def _parse_with_regex(html_text):
    """最后兜底：直接正则找 /p/<tid>。"""
    out = []
    for m in re.finditer(r'href=["\']?(/p/(\d+)[^"\'\s>]*)["\']?', html_text or ""):
        out.append(("", m.group(2), m.group(1)))
    return out


def parse_album_list(html):
    """解析相册列表页 HTML，返回 [(相册名, tid, href)]（已按 tid 去重、保持原顺序）。"""
    if not html:
        return []

    comment_free = strip_html_comments(html)

    albums = _parse_with_lxml(comment_free)
    if not albums:
        # 贴吧注释惯用 # 充当引号，替换后再试
        albums = _parse_with_lxml(re.sub("#", '"', comment_free))
    if not albums:
        albums = _parse_with_regex(comment_free)
    if not albums:
        albums = _parse_with_regex(re.sub("#", '"', comment_free))

    seen, out = set(), []
    for idx, (name, tid, href) in enumerate(albums, 1):
        if not tid or tid in seen:
            continue
        seen.add(tid)
        out.append((name or ("相册%d" % idx), tid, href))
    return out


def fetch_album_list(backend, kw, log=None, scroll=True, max_scroll_rounds=12, settle_ms=2500):
    """用浏览器打开相册列表页并返回 [(相册名, tid, href)]、原始 HTML、页面 URL。"""
    log = log or (lambda msg: None)
    url = ALBUM_LIST_URL.format(kw=quote(kw))
    html = backend.get_html(url, settle_ms=settle_ms, scroll=scroll,
                            max_scroll_rounds=max_scroll_rounds)
    albums = parse_album_list(html)
    if not albums:
        log("  未在列表页解析到相册（可能被安全验证拦截或贴吧改版）")
    else:
        log("  解析到 %d 本相册" % len(albums))
    return albums, html, url
