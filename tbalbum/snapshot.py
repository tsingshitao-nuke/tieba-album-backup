# -*- coding: utf-8 -*-
"""离线 HTML 生成（原版贴吧视觉还原）。

布局与现网一致的三层页面：
- L1 吧级相册墙（索引页 `相册列表.html`）：所有相册封面网格。
- L2 单本相册墙（`<相册名>.html`，每本一份）：该相册图片缩略图墙，点图进帖页。
- L3 帖子页（`<相册名>/帖子/<序号_描述>.html`，每张图一份）：大图 + 楼主描述 + 评论楼。

视觉还原靠「贴吧真实 class 名 + 贴吧真实 CSS」：
- `static/tieba_core.css`：运行时从相册列表页抽取的贴吧核心框架 CSS（css_extract.extract_link_css）。
- 内联 fallback：本文件内置一段精简的贴吧风样式，即使核心 CSS 没下载到也不至于裸奔。
图片一律指向本地已下载原图；评论用接口全量注入（比原页更全）。
"""
import datetime
import os
import re
from html import escape as _esc
from urllib.parse import quote

INDEX_PAGE_NAME = "相册列表"
RAW_SNAPSHOT_NAME = "原始快照.html"
STATIC_DIR = "static"
CORE_CSS = "tieba_core.css"

# 精简的贴吧风 fallback（核心 CSS 没拉到时的兜底，保证不裸奔）
FALLBACK_CSS = """/* 贴吧相册备份 · fallback（核心 CSS 缺失时兜底） */
*{box-sizing:border-box}
body{margin:0;background:#f5f6f7;color:#222;
  font:14px/1.7 "Microsoft YaHei UI","Microsoft YaHei",-apple-system,"PingFang SC",sans-serif}
a{color:#2b6cd4;text-decoration:none}a:hover{text-decoration:underline}
img{max-width:100%}
.p_thread .pb_content{max-width:960px;margin:0 auto;padding:18px 16px 48px}
.poster_component .p_content img{display:block;max-width:100%;margin:0 auto 10px;border-radius:4px}
.l_post{border-top:1px solid #e6e8eb;padding:12px 2px;margin-top:6px}
.d_post_content .p_content .user{color:#2b6cd4;font-weight:600;margin-right:8px}
.d_post_content .p_content .floor{color:#a7adb5;margin-right:8px}
.d_post_content .p_content .time{color:#a7adb5;float:right;font-size:12px}
.d_post_content .p_content .content{margin-top:4px;color:#333;word-break:break-word;white-space:pre-wrap}
.poster_text{margin-top:6px;color:#333;word-break:break-word}
.pb_footer{margin-top:26px;color:#9aa0a8;font-size:12px;text-align:center;line-height:1.9}
.album_wall,.bar_wall{display:flex;flex-wrap:wrap;gap:14px;padding:8px 0}
.grbm_ele_wrapper{width:168px;background:#fff;border:1px solid #e6e8eb;border-radius:8px;
  overflow:hidden;transition:box-shadow .15s;display:block}
.grbm_ele_wrapper:hover{box-shadow:0 4px 14px rgba(0,0,0,.08)}
.grbm_ele_wrapper img{width:168px;height:168px;object-fit:cover;display:block;background:#f0f1f3}
.grbm_ele_wrapper .grbm_ele_title{font-size:13px;padding:6px 8px;color:#222;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.grbm_ele_wrapper .grbm_ele_no{color:#a7adb5;font-size:12px;padding:0 8px 6px}
.head{background:#fff;border:1px solid #e6e8eb;border-radius:10px;padding:18px 20px;margin-bottom:16px}
.head h1{margin:4px 0 6px;font-size:22px;font-weight:600}
.head .crumb{font-size:13px;color:#9aa0a8}
.head .meta{font-size:13px;color:#9aa0a8;margin-top:4px}
.missing{color:#c0392b;font-size:13px;padding:30px 10px;text-align:center}
"""

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
_ONATTR_RE = re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def esc(text):
    return _esc("" if text is None else str(text), quote=True)


def rel_url(*segments):
    """生成相对 kw_dir 的本地路径（逐段百分号编码，不含前导 ./）。"""
    return "/".join(quote(str(s), safe="") for s in segments if str(s))


def _asset(prefix, *segments):
    """拼出从当前 HTML 指向本地资源的相对链接。

    prefix 为「回到 kw_dir 所需的层级前缀」：墙页/索引页=''，帖页='../../'。
    当 prefix 为空时用 './' 开头（浏览器解析更稳）；否则直接拼接到路径前。
    """
    path = rel_url(*segments)
    if prefix:
        return prefix + path
    return "./" + path


def fmt_time(ts):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def clean_comment_html(content):
    """贴吧评论内容 → 安全可读 HTML（去掉脚本/事件属性/远端表情，保留文本与换行）。"""
    if not content:
        return ""
    s = str(content)
    s = _SCRIPT_RE.sub("", s)
    s = _ONATTR_RE.sub("", s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _CTRL_RE.sub("", s)
    s = s.replace("\u0018", "").strip()
    if not s:
        return ""
    return esc(s).replace("\n", "<br>")


# ---------------------------------------------------------------------------
# L3 帖子页（每张图一份）
# ---------------------------------------------------------------------------
def _post_comments(comments, root_prefix):
    if not comments:
        return '<div class="p_postlist_floor"><div class="none">暂无评论</div></div>'
    items = []
    for c in comments:
        items.append(
            '<div class="l_post j_l_post">'
            '<div class="d_post">'
            '<div class="p_post">'
            '<div class="d_post_content">'
            '<div class="p_content">'
            '<span class="user">%s</span>'
            '<span class="floor">#%s</span>'
            '<span class="time">%s</span>'
            '<div class="content">%s</div>'
            '</div></div></div></div></div>'
            % (esc(c.get("user_name") or "匿名"), esc(str(c.get("floor") or "-")),
               esc(fmt_time(c.get("time"))), clean_comment_html(c.get("content")))
        )
    return '<div class="p_postlist_floor">%s</div>' % "".join(items)


def build_post_html(kw, album_name, entry, comments, safe, post_file, total,
                    root_prefix="../../", source_url="", grabbed_at=""):
    """单张图的帖子页（L3）：大图 + 楼主描述 + 评论楼，贴吧原版视觉。"""
    order = entry.get("order")
    descr = entry.get("descr") or ""
    img_src = _asset(root_prefix, *entry["rel"]) if entry.get("ok") and entry.get("rel") else ""
    wall_href = root_prefix + esc(safe) + ".html"
    parts = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>%s · %s吧相册</title>" % (esc(descr or ("第 %s 张" % order)), esc(kw)),
        '<link rel="stylesheet" href="%s">' % _asset(root_prefix, STATIC_DIR, CORE_CSS),
        "<style>%s</style>" % FALLBACK_CSS,
        "</head><body>",
        '<div class="p_thread thread_theme_7">',
        '<div class="pb_content clearfix">',
        '<div class="head"><div class="crumb"><a href="%s">← 返回「%s」相册</a></div></div>'
        % (wall_href, esc(album_name)),
        '<div class="p_postlist">',
        # 楼主帖
        '<div class="l_post j_l_post l_post_bright">',
        '<div class="d_post">',
        '<div class="poster_component editor_content_wrapper ueditor_container">',
        '<div class="p_post"><div class="d_post_content"><div class="p_content">',
    ]
    if img_src:
        alt = esc(descr or ("第 %s 张" % order))
        parts.append('<img class="BDE_Image" src="%s" alt="%s">' % (img_src, alt))
    else:
        parts.append('<div class="missing">图片下载失败<br>%s</div>' % esc(entry.get("error") or "未知错误"))
    parts.append('<div class="poster_text">%s</div>' % esc(descr))
    parts.append("</div></div></div></div></div>")
    # 评论楼
    parts.append(_post_comments(comments, root_prefix))
    parts.append("</div></div></div>")
    parts.append('<div class="pb_footer">本页由「贴吧相册备份工具」离线生成 · 图片已保存到本地<br>'
                 '图片版权归原作者所有，仅供个人备份收藏</div>')
    parts.append("</body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# L2 单本相册墙（每本一份）
# ---------------------------------------------------------------------------
def build_album_html(kw, album_name, entries, safe, post_files, tid="",
                     source_url="", grabbed_at="", album_index=None,
                     album_total=None, root_prefix=""):
    """单本相册墙页（L2）：缩略图网格，点图进对应帖页。"""
    ok_count = sum(1 for e in entries if e.get("ok"))
    nav = ""
    if album_index and album_total:
        nav = '<span class="dot">·</span>第 %d / %d 本' % (album_index, album_total)
    parts = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>%s · %s吧相册备份</title>" % (esc(album_name), esc(kw)),
        '<link rel="stylesheet" href="%s">' % _asset(root_prefix, STATIC_DIR, CORE_CSS),
        "<style>%s</style>" % FALLBACK_CSS,
        "</head><body>",
        '<div class="p_thread thread_theme_7"><div class="pb_content clearfix">',
        '<div class="head">',
        '<div class="crumb"><a href="%s">← 返回「%s吧」相册列表</a></div>'
        % (_asset(root_prefix, INDEX_PAGE_NAME + ".html"), esc(kw)),
        "<h1>%s</h1>" % esc(album_name),
        '<div class="meta">共 %d 张<span class="dot">·</span>已保存 %d 张%s'
        % (len(entries), ok_count, nav),
    ]
    if grabbed_at:
        parts.append('<span class="dot">·</span>备份于 %s' % esc(grabbed_at))
    if source_url:
        parts.append('<span class="dot">·</span><a href="%s" target="_blank" rel="noreferrer">查看原帖</a>'
                     % esc(source_url))
    parts.append("</div></div>")
    parts.append('<div class="album_wall">')
    for e, pf in zip(entries, post_files):
        href = _asset(root_prefix, safe, "帖子", pf)
        img = _asset(root_prefix, *e["rel"]) if e.get("ok") and e.get("rel") else ""
        no = esc(e.get("order"))
        title = esc(e.get("descr") or ("第 %s 张" % e.get("order")))
        parts.append('<a class="grbm_ele_wrapper" href="%s">' % href)
        if img:
            parts.append('<img src="%s" alt="%s" loading="lazy">' % (img, title))
        else:
            parts.append('<div class="missing">下载失败</div>')
        parts.append('<div class="grbm_ele_no">%s</div>' % no)
        parts.append('<div class="grbm_ele_title">%s</div>' % title)
        parts.append("</a>")
    parts.append("</div>")
    parts.append('<div class="pb_footer">本页由「贴吧相册备份工具」离线生成 · 图片已保存到本地 '
                 '<code>%s/图片/</code><br>图片版权归原作者所有，仅供个人备份收藏</div>'
                 % esc(album_name))
    parts.append("</div></div></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# L1 吧级相册墙（索引页）
# ---------------------------------------------------------------------------
def build_bar_wall_html(kw, albums, source_url="", grabbed_at="", root_prefix=""):
    """吧级相册墙（索引页 L1）：所有相册封面网格，点进各本相册墙。"""
    total_img = sum((a.get("total") or 0) for a in albums)
    saved_img = sum((a.get("saved") or 0) for a in albums)
    parts = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>%s吧 · 相册备份</title>" % esc(kw),
        '<link rel="stylesheet" href="%s">' % _asset(root_prefix, STATIC_DIR, CORE_CSS),
        "<style>%s</style>" % FALLBACK_CSS,
        "</head><body>",
        '<div class="p_thread thread_theme_7"><div class="pb_content clearfix">',
        '<div class="head"><h1>%s吧 · 相册备份</h1>' % esc(kw),
        '<div class="meta">共 %d 本相册<span class="dot">·</span>收录 %d 张图片'
        '<span class="dot">·</span>已保存 %d 张' % (len(albums), total_img, saved_img),
    ]
    if grabbed_at:
        parts.append('<span class="dot">·</span>备份于 %s' % esc(grabbed_at))
    if source_url:
        parts.append('<span class="dot">·</span><a href="%s" target="_blank" rel="noreferrer">查看原贴吧相册</a>'
                     % esc(source_url))
    parts.append("</div></div>")
    parts.append('<div class="bar_wall">')
    for a in albums:
        href = _asset(root_prefix, a.get("html") or "")
        cover = _asset(root_prefix, *a["cover"]) if a.get("cover") else ""
        name = esc(a.get("name") or "")
        parts.append('<a class="grbm_ele_wrapper" href="%s">' % href)
        if cover:
            parts.append('<img src="%s" alt="%s" loading="lazy">' % (cover, name))
        else:
            parts.append('<div class="missing">无封面</div>')
        parts.append('<div class="grbm_ele_title">%s</div>' % name)
        parts.append("</a>")
    parts.append("</div>")
    parts.append('<div class="pb_footer">本页由「贴吧相册备份工具」离线生成 · 双击任意相册进入浏览<br>'
                 '图片版权归原作者所有，仅供个人备份收藏</div>')
    parts.append("</div></div></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 旧网格（--legacy 切换，保留兼容）
# ---------------------------------------------------------------------------
def build_album_html_legacy(kw, album_name, entries, tid="", source_url="",
                            grabbed_at="", album_index=None, album_total=None):
    """旧版「仿贴吧」网格（用户可切换回此模式）。"""
    ok_count = sum(1 for e in entries if e.get("ok"))
    nav = ""
    if album_index and album_total:
        nav = '<span class="dot">·</span>第 %d / %d 本' % (album_index, album_total)
    parts = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>%s · %s吧相册备份</title>" % (esc(album_name), esc(kw)),
        '<link rel="stylesheet" href="%s">' % rel_url(STATIC_DIR, "style.css"),
        '</head><body><div class="page">',
        '<div class="head">',
        '<div class="crumb"><a href="%s">← 返回「%s吧」相册列表</a></div>'
        % (rel_url(INDEX_PAGE_NAME + ".html"), esc(kw)),
        "<h1>%s</h1>" % esc(album_name),
        '<div class="meta">共 %d 张<span class="dot">·</span>已保存 %d 张%s'
        % (len(entries), ok_count, nav),
    ]
    if grabbed_at:
        parts.append('<span class="dot">·</span>备份于 %s' % esc(grabbed_at))
    if source_url:
        parts.append('<span class="dot">·</span><a href="%s" target="_blank" rel="noreferrer">查看原帖</a>'
                     % esc(source_url))
    parts.append("</div></div>")
    parts.append('<div class="grid">')
    for e in entries:
        parts.append('<div class="card" id="p%s">' % esc(e.get("order")))
        if e.get("ok") and e.get("rel"):
            src = rel_url(*e["rel"])
            alt = esc(e.get("descr") or ("第 %s 张" % e.get("order")))
            parts.append('<div class="ph"><a href="%s" target="_blank"><img src="%s" alt="%s" loading="lazy"></a></div>'
                         % (src, src, alt))
        else:
            parts.append('<div class="ph"><div class="missing">下载失败<br><span class="err">%s</span></div></div>'
                         % esc(e.get("error") or "未知错误"))
        parts.append('<div class="cap"><span class="no">%s</span><span class="desc">%s</span></div>'
                     % (esc(e.get("order")), esc(e.get("descr") or "(无描述)")))
        bits = []
        if e.get("width") and e.get("height"):
            bits.append("%sx%s" % (esc(e.get("width")), esc(e.get("height"))))
        if e.get("bytes"):
            bits.append(_fmt_size(e.get("bytes")))
        if e.get("pic_id"):
            bits.append("id:%s" % esc(str(e.get("pic_id"))[:12]))
        if bits:
            parts.append('<div class="pmeta">%s</div>' % '<span class="dot">·</span>'.join(bits))
        parts.append(_comments_block_legacy(e.get("comments") or [], e.get("comment_total") or 0))
        parts.append("</div>")
    parts.append("</div>")
    parts.append('<div class="foot">本页由「贴吧相册备份工具」离线生成 · 图片已保存到本地 '
                 '<code>%s/图片/</code><br>图片版权归原作者所有，仅供个人备份收藏</div>'
                 % esc(album_name))
    parts.append("</div></body></html>")
    return "".join(parts)


def _fmt_size(num):
    try:
        num = int(num)
    except (TypeError, ValueError):
        return ""
    if num <= 0:
        return ""
    if num < 1024:
        return "%d B" % num
    if num < 1024 * 1024:
        return "%.1f KB" % (num / 1024.0)
    return "%.2f MB" % (num / 1024.0 / 1024.0)


def _comments_block_legacy(comments, comment_total=0):
    if not comments:
        return '<div class="comments"><div class="none">暂无评论</div></div>'
    shown = ""
    if comment_total and comment_total > len(comments):
        shown = '<span class="cnt" style="color:#b3b9c2;font-size:12px"> (共 %d 条，已保存 %d 条)</span>' % (
            comment_total, len(comments))
    items = []
    for c in comments:
        items.append(
            '<div class="cm">'
            '<span class="t">%s</span>'
            '<span class="u">%s</span>'
            '<span class="f">#%s</span>'
            '<div class="c">%s</div>'
            '</div>' % (esc(fmt_time(c.get("time"))), esc(c.get("user_name") or "匿名"),
                        esc(c.get("floor") or "-"), clean_comment_html(c.get("content"))))
    return ('<div class="comments"><div class="ctitle">评论%s</div>%s</div>'
            % (shown, "".join(items)))


# ---------------------------------------------------------------------------
# 输出与静态资源
# ---------------------------------------------------------------------------
LEGACY_CSS = """/* 贴吧相册备份 · 旧网格样式（--legacy 用） */
:root{--bg:#f5f6f7;--card:#fff;--line:#e6e8eb;--text:#1f2329;--sub:#8a919e;--brand:#2b6cd4;--chip:#eef3fc;--quote:#f7f8fa}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 "Microsoft YaHei UI","Microsoft YaHei",sans-serif}
a{color:var(--brand);text-decoration:none}a:hover{text-decoration:underline}
.page{max-width:1180px;margin:0 auto;padding:24px 20px 64px}
.head{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:18px}
.head h1{margin:6px 0 8px;font-size:22px;font-weight:600}
.head .crumb{font-size:13px;color:var(--sub)}
.head .meta{font-size:13px;color:var(--sub)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.ph{background:#f0f1f3;display:flex;align-items:center;justify-content:center;min-height:180px;max-height:460px;overflow:hidden}
.ph img{display:block;max-width:100%;max-height:460px;height:auto;cursor:zoom-in}
.ph .missing{padding:40px 12px;color:var(--sub);font-size:13px;text-align:center}
.cap{padding:10px 14px 0;font-size:13px}
.cap .no{display:inline-block;background:var(--chip);color:var(--brand);border-radius:4px;padding:1px 7px;font-size:12px;margin-right:6px}
.cap .desc{color:var(--text);word-break:break-all}
.pmeta{padding:4px 14px 10px;font-size:12px;color:var(--sub);word-break:break-all}
.comments{border-top:1px dashed var(--line);margin-top:auto;padding:8px 14px 12px;background:var(--quote)}
.comments .ctitle{font-size:12px;color:var(--sub);margin-bottom:6px}
.cm{padding:5px 0;border-top:1px solid #eef0f3;font-size:13px}
.cm:first-of-type{border-top:none}
.cm .u{color:var(--brand);font-weight:600;margin-right:6px}
.cm .f{color:#b3b9c2;font-size:12px;margin-right:6px}
.cm .t{color:#b3b9c2;font-size:12px;float:right}
.cm .c{color:#333;word-break:break-word;white-space:pre-wrap;margin-top:2px}
.comments .none{color:var(--sub);font-size:12px}
.err{color:#c0392b;font-size:12px}
.albums{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:16px}
.acard{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;transition:box-shadow .15s}
.acard:hover{box-shadow:0 4px 14px rgba(0,0,0,.08)}
.acard .cover{height:150px;background:#f0f1f3;display:block;overflow:hidden}
.acard .cover img{width:100%;height:150px;object-fit:cover;display:block}
.acard .cover .noimg{height:150px;display:flex;align-items:center;justify-content:center;color:var(--sub);font-size:12px}
.acard .body{padding:10px 12px 12px}
.acard .nm{font-size:14px;font-weight:600;color:var(--text);word-break:break-all}
.acard .cnt{margin-top:4px;font-size:12px;color:var(--sub)}
.acard .bar{margin-top:7px;height:4px;background:#eef0f3;border-radius:2px;overflow:hidden}
.acard .bar i{display:block;height:100%;background:#3fae6a}
.acard .bar i.warn{background:#e8a33d}
.foot{margin-top:26px;color:var(--sub);font-size:12px;text-align:center;line-height:1.9}
"""


def write_static(kw_dir, legacy=False):
    """写入 static/style.css（仅 --legacy 模式需要）。"""
    d = os.path.join(kw_dir, STATIC_DIR)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "style.css"), "w", encoding="utf-8") as fp:
        fp.write(LEGACY_CSS)
    return d


def write_html(path, html):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(html)
    return path


def write_raw_snapshot(path, raw_html):
    """保存现网渲染后的原始 DOM 存档（供对照，不参与离线浏览）。"""
    return write_html(path, raw_html or "")
