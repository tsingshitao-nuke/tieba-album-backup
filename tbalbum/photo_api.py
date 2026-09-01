# -*- coding: utf-8 -*-
"""单图信息 + 评论接口。

实测要点（务必保留）：
- 端点 `GET /photo/p?kw=&tid=&pic_id=&pn=<n>`，页面内嵌 `var albumData = {...};`
- albumData 是 **JS 对象字面量**（键用单引号、值多为单引号字符串），**不是合法 JSON**，
  直接 json.loads 会失败。这里用「定向提取 + 括号配对」解析，比盲目替换引号稳。
- 评论**按页返回，每页 10 条**：pn=1 → 楼层 2..12，pn=2 → 13..22，pn=3 → 23..32，
  pn=4 → 33..38（comment_amount=36）。必须翻页才能取全。
- `img.medium.url` 就是全尺寸原图（实测 330x518 / 117KB），与 pic/item/<pic_id>.jpg 一致。
- 响应为 **GBK**，无需登录。
"""
import json
import re
import time
from urllib.parse import quote

from . import PHOTO_PAGE_URL, COMMENTS_PER_PAGE, ORIGINAL_IMG_URL

# 每本相册里单张图最多翻多少页评论（安全阀，防止评论巨多的图拖垮整体）
DEFAULT_MAX_COMMENT_PAGES = 60


def _extract_balanced(text, open_idx):
    """从 open_idx 处的 '{' 或 '[' 起，返回配对的完整片段（识别字符串与转义）。"""
    if open_idx < 0 or open_idx >= len(text):
        return ""
    open_ch = text[open_idx]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return text[open_idx:i + 1]
        i += 1
    return ""


def _js_value(seg, key, open_ch):
    """取 `'key' : <{/[ 开头的值>`，返回原始文本片段。

    注意：re.escape 已经会把 `[` `{` 转义成 `\\[` `\\{`，此处不要再额外加反斜杠，
    否则正则会变成「字面反斜杠 + 未闭合的字符类」而抛 re.error。
    """
    m = re.search(r"['\"]%s['\"]\s*:\s*%s" % (re.escape(key), re.escape(open_ch)), seg)
    if not m:
        return ""
    return _extract_balanced(seg, m.end() - 1)


def _unescape_js(s):
    """还原 \\uXXXX（贴吧偶尔会写 \\u0018 之类的转义）。"""
    if "\\u" not in s:
        return s

    def _rep(m):
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)

    return re.sub(r"\\u([0-9a-fA-F]{4})", _rep, s)


def _js_str(seg, key):
    """取 `'key' : '值'`。"""
    m = re.search(r"['\"]%s['\"]\s*:\s*'((?:[^'\\]|\\.)*)'" % re.escape(key), seg)
    if not m:
        return ""
    return _unescape_js(m.group(1).replace("\\'", "'").replace('\\"', '"'))


def _js_int(seg, key, default=0):
    m = re.search(r"['\"]%s['\"]\s*:\s*'?(\d+)'?" % re.escape(key), seg)
    if not m:
        return default
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return default


def parse_album_data(html):
    """从 /photo/p 页面 HTML 解析 albumData，返回 dict，失败返回 None。"""
    m = re.search(r"albumData\s*=\s*\{", html)
    if not m:
        return None
    seg = html[m.start():m.start() + 600000]

    out = {
        "comment_amount": _js_int(seg, "comment_amount"),
        "comment_list": [],
        "comment_type": _js_str(seg, "comment_type"),
        "create_time": _js_int(seg, "create_time"),
        "desc": _js_str(seg, "desc"),
        "pic_id": _js_str(seg, "pic_id"),
        "post_id": _js_str(seg, "post_id"),
        "thread_id": _js_str(seg, "thread_id"),
        "pic_amount": _js_int(seg, "pic_amount"),
        "title": _js_str(seg, "title"),
        "total_page": _js_int(seg, "total_page"),
        "original_url": "",
    }

    arr = _js_value(seg, "comment_list", "[")
    if arr:
        try:
            out["comment_list"] = json.loads(arr)
        except Exception:
            out["comment_list"] = []

    obj = _js_value(seg, "img", "{")
    if obj:
        try:
            img = json.loads(obj)
            out["img"] = img
            medium = (img or {}).get("medium") or {}
            out["original_url"] = medium.get("url") or ""
        except Exception:
            pass
    if not out["original_url"] and out["pic_id"]:
        out["original_url"] = ORIGINAL_IMG_URL.format(pic_id=out["pic_id"])
    return out


def normalize_comment(raw):
    """统一评论字段（不同版本字段名不同）。"""
    if isinstance(raw, str):
        return {"floor": 0, "user_name": "", "user_id": "", "content": raw,
                "time": 0, "post_id": ""}
    return {
        "floor": raw.get("floor") or 0,
        "user_name": raw.get("user_name") or raw.get("username") or raw.get("nick") or "",
        "user_id": str(raw.get("user_id") or ""),
        "content": raw.get("content") or raw.get("text") or "",
        "time": raw.get("time") or 0,
        "post_id": str(raw.get("post_id") or ""),
    }


def fetch_comments(fetcher, kw, tid, pic_id, max_comments=None,
                   log=None, page_delay=0.3, max_pages=DEFAULT_MAX_COMMENT_PAGES):
    """翻页取全某张图的评论。返回 (comments, comment_amount)。

    终止条件：某页无新增 / 达到 comment_amount / 达到 max_comments / 达到 max_pages。
    """
    log = log or (lambda msg: None)
    kw_q, tid_s = quote(kw), str(tid)
    comments = []
    seen = set()
    total = 0
    pn = 1

    while pn <= max_pages:
        url = PHOTO_PAGE_URL.format(kw=kw_q, tid=tid_s, pic_id=pic_id, pn=pn)
        try:
            html = fetcher.get_text(url)
        except Exception as exc:                            # noqa: BLE001
            log("  评论 pn=%d 获取失败：%s" % (pn, exc))
            break

        ad = parse_album_data(html)
        if not ad:
            break
        if pn == 1:
            total = ad.get("comment_amount") or 0

        added = 0
        for raw in ad.get("comment_list") or []:
            item = normalize_comment(raw)
            key = item["post_id"] or (item["floor"], item["user_name"], item["content"][:40])
            if key in seen:
                continue
            seen.add(key)
            comments.append(item)
            added += 1

        if added == 0:
            break
        if max_comments and len(comments) >= max_comments:
            break
        if total and len(comments) >= total:
            break
        pn += 1
        if page_delay > 0:
            time.sleep(page_delay)

    return comments, total


def fetch_photo_info(fetcher, kw, tid, pic_id, log=None):
    """取单图信息（desc / 原图 url / 评论总数）以及第 1 页评论。"""
    log = log or (lambda msg: None)
    url = PHOTO_PAGE_URL.format(kw=quote(kw), tid=str(tid), pic_id=pic_id, pn=1)
    ad = parse_album_data(fetcher.get_text(url))
    if not ad:
        return None
    return {
        "desc": ad.get("desc") or "",
        "original_url": ad.get("original_url") or "",
        "comment_amount": ad.get("comment_amount") or 0,
        "comments": [normalize_comment(c) for c in (ad.get("comment_list") or [])],
        "pic_amount": ad.get("pic_amount") or 0,
        "post_id": ad.get("post_id") or "",
    }


def comment_page_estimate(comment_amount, max_comments=None):
    """估算需要翻多少页评论（用于进度显示）。"""
    if not comment_amount:
        return 1
    n = comment_amount if not max_comments else min(comment_amount, max_comments)
    return max(1, -(-int(n) // COMMENTS_PER_PAGE))
