# -*- coding: utf-8 -*-
"""相册图片清单接口。

实测要点（务必保留，改版时只改这里）：
- 端点 `GET /photo/g/bw/picture/list?kw=&alt=jview&rn=200&tid=&pn=1&ps=<a>&pe=<b>&info=1`
- **ps/pe 是 1 起、闭区间**，每页最多 200。
  实测：ps=1&pe=200 → index 0..199；ps=201&pe=400 → index 200..399；
  200+200+200+108 = 708 = total_num，index 全程连续无缺。
- **不可用 pic_id 游标分页**（`&prev=0&pic_id=xxx&next=40` 会从头返回，实测如此）。
- 响应为 **GBK**，由 Fetcher.smart_decode 处理。
- 无需登录。
"""
from urllib.parse import quote

from . import PICTURE_LIST_URL, PAGE_SIZE, ORIGINAL_IMG_URL


class AlbumInfo(object):
    """一本相册的元信息。"""

    def __init__(self, tid, name="", href="", total_num=0, title="", user_name=""):
        self.tid = str(tid)
        self.name = name or title or ("相册%s" % tid)
        self.href = href
        self.total_num = int(total_num or 0)
        self.title = title
        self.user_name = user_name

    def __repr__(self):
        return "AlbumInfo(tid=%s, name=%r, total=%d)" % (self.tid, self.name, self.total_num)


class Photo(object):
    """相册里的一张图。"""

    __slots__ = ("index", "pic_id", "descr", "purl", "murl", "width", "height")

    def __init__(self, index, pic_id, descr="", purl="", murl="", width=0, height=0):
        self.index = int(index or 0)
        self.pic_id = pic_id or ""
        self.descr = (descr or "").strip()
        self.purl = purl or ""
        self.murl = murl or ""
        self.width = width or 0
        self.height = height or 0

    @property
    def original_url(self):
        """原图直链（实测 forum/pic/item/<pic_id>.jpg 即为全尺寸原图）。"""
        return ORIGINAL_IMG_URL.format(pic_id=self.pic_id)

    def __repr__(self):
        return "Photo(#%d, %s, %r)" % (self.index, self.pic_id[:12], self.descr)


def _page_url(kw, tid, ps, pe):
    return PICTURE_LIST_URL.format(kw=quote(kw), tid=tid, ps=ps, pe=pe)


def fetch_album_photos(fetcher, kw, tid, log=None, on_page=None, max_pages=200):
    """按 ps/pe 分页拉全本相册的图片清单。

    返回 (photos, meta)：
      photos —— 按 index 升序、按 pic_id 去重后的 Photo 列表（即相册原始排列顺序）
      meta   —— {"title","total_num","total_page","user_name"}
    """
    log = log or (lambda msg: None)
    tid = str(tid)

    def _load(ps, pe):
        url = _page_url(kw, tid, ps, pe)
        data = fetcher.get_json(url)
        err_no = data.get("no", 0)
        try:
            err_no = int(err_no)
        except (TypeError, ValueError):
            err_no = 0
        if err_no != 0:
            raise RuntimeError("图片清单接口返回错误 no=%s error=%s"
                               % (err_no, data.get("error")))
        return data.get("data") or {}

    first = _load(1, PAGE_SIZE)
    meta = {
        "title": first.get("title") or "",
        "total_num": int(first.get("total_num") or 0),
        "total_page": int(first.get("total_page") or 0),
        "user_name": first.get("user_name") or "",
    }

    raw = list(first.get("pic_list") or [])
    total = meta["total_num"] or len(raw)
    meta["total_num"] = total
    log("  第 1 页：%d 张（相册共 %d 张）" % (len(raw), total))
    if on_page:
        on_page(1, len(raw), total)

    start = 1 + PAGE_SIZE
    page_no = 1
    while start <= total and page_no < max_pages:
        end = min(start + PAGE_SIZE - 1, total)
        chunk = _load(start, end).get("pic_list") or []
        if not chunk:
            break
        raw.extend(chunk)
        page_no += 1
        log("  第 %d 页：%d 张（累计 %d / %d）" % (page_no, len(chunk), len(raw), total))
        if on_page:
            on_page(page_no, len(raw), total)
        if end >= total:
            break
        start = end + 1

    # 去重（按 pic_id）+ 按 index 排序 —— index 即相册原始排列顺序
    by_id = {}
    for item in raw:
        pid = item.get("pic_id")
        if not pid:
            continue
        by_id.setdefault(pid, item)
    photos = [
        Photo(index=it.get("index") or 0,
              pic_id=it.get("pic_id") or "",
              descr=it.get("descr") or "",
              purl=it.get("purl") or "",
              murl=it.get("murl") or "",
              width=it.get("width") or 0,
              height=it.get("height") or 0)
        for it in by_id.values()
    ]
    photos.sort(key=lambda p: p.index)

    log("  清单取到 %d 张（去重后），相册应有 %d 张" % (len(photos), total))
    if photos and total and len(photos) < total:
        log("  警告：少了 %d 张，可能是分页异常或图片已被删除" % (total - len(photos)))
    return photos, meta
