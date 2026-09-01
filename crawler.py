# -*- coding: utf-8 -*-
"""贴吧相册备份 —— 编排层。

流程：
    链接 → 解析贴吧名
      → 浏览器打开相册列表页（唯一需要浏览器的步骤，贴吧对 curl/无头一律安全验证）
      → 逐本相册：图片清单接口(ps/pe 分页拉全) → 并发下载原图 → 评论分页抓全
      → 生成离线 HTML + manifest.json

用法：
    python crawler.py --kw 红警3 --out-dir D:\\贴吧相册备份
    python crawler.py --login                  # 只打开登录窗口
    python crawler.py --selftest               # 离线自检，不联网
"""
import json
import os
import sys
import threading
import datetime
from urllib.parse import urlparse, parse_qs, unquote

from tbalbum import ALBUM_LIST_URL
from tbalbum.album_api import fetch_album_photos
from tbalbum.album_list import fetch_album_list
from tbalbum.browser import create_backend
from tbalbum.downloader import Downloader
from tbalbum.http import Fetcher, StopRequested
from tbalbum.manifest import (MANIFEST_NAME, completed_pic_ids, load as load_manifest,
                              new_manifest, save as save_manifest, summarize)
from tbalbum.naming import build_stem, sanitize
from tbalbum.photo_api import fetch_comments
from tbalbum import snapshot
from tbalbum import css_extract

import traceback as _tb


def _frozen_base():
    """日志/结果文件落点：打包后放 exe 旁边，开发时放当前目录。"""
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()


class _Tee:
    """把 stdout 同时写到文件（打包成窗口程序后控制台不可见）。"""
    def __init__(self, path, con):
        self._f = open(path, "w", encoding="utf-8", buffering=1)
        self._con = con

    def write(self, s):
        try:
            self._f.write(s)
        except Exception:
            pass
        try:
            if self._con:
                self._con.write(s)
        except Exception:
            pass

    def flush(self):
        try:
            self._f.flush()
        except Exception:
            pass


def extract_kw(url_or_kw):
    """从贴吧链接解析吧名，或直接返回输入的吧名。"""
    raw = (url_or_kw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        return raw
    try:
        qs = parse_qs(urlparse(raw).query)
        for key in ("kw", "word"):
            if qs.get(key) and qs[key][0]:
                return unquote(qs[key][0])
    except Exception:
        pass
    return ""


def default_profile():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"))
    return os.path.join(base, "贴吧相册下载器", "profile")


def default_browser():
    """自动选择浏览器后端：打包了 playwright 就用它，否则（瘦身版）退回 CDP。

    这样同一个 app.py / crawler.py 既能打出「Playwright 版」也能打出
    「CDP 瘦身版」，只是 spec 里排不排除 playwright 的区别。
    """
    try:
        import playwright                # noqa: F401
        return "playwright"
    except Exception:
        return "cdp"


def default_out_dir_suggestion():
    """给「选择输出目录」对话框的初始位置：优先 D 盘，避免默认写 C 盘。"""
    for cand in ("D:\\", "E:\\"):
        if os.path.isdir(cand):
            return os.path.join(cand, "贴吧相册备份")
    return os.path.expanduser("~")


class CrawlOptions(object):
    def __init__(self, out_dir, fetch_comments=True, max_comments=None,
                 workers=4, delay=0.15, comment_delay=0.25, resume=True,
                 save_raw=True, browser=None, profile_dir=None,
                 headless=False, original=True):
        self.out_dir = out_dir
        self.fetch_comments = bool(fetch_comments)
        self.max_comments = max_comments          # None = 不限
        self.workers = max(1, int(workers or 1))
        self.delay = max(0.0, float(delay or 0))
        self.comment_delay = max(0.0, float(comment_delay or 0))
        self.resume = bool(resume)
        self.save_raw = bool(save_raw)
        self.browser = browser            # None = 自动选择（见 default_browser）
        self.profile_dir = profile_dir or default_profile()
        self.headless = headless
        self.original = bool(original)    # True = 原版贴吧视觉还原；False = 旧网格(--legacy)

    def as_dict(self):
        return {"fetch_comments": self.fetch_comments, "max_comments": self.max_comments,
                "workers": self.workers, "delay": self.delay,
                "comment_delay": self.comment_delay, "resume": self.resume,
                "original": self.original}


class Crawler(object):
    def __init__(self, kw, options, log=None, progress=None, stop_event=None):
        self.kw = (kw or "").strip()
        self.options = options
        self.log = log or (lambda msg: None)
        self.progress = progress or (lambda stage, cur, total, msg="": None)
        self.stop_event = stop_event or threading.Event()
        self.source_url = ALBUM_LIST_URL.format(kw=self.kw)

    # ------------------------------------------------------------------
    def _stopped(self):
        return self.stop_event.is_set()

    def _unique_name(self, base, taken):
        name = base or "未命名"
        i = 2
        while name in taken:
            name = "%s(%d)" % (base, i)
            i += 1
        taken.add(name)
        return name

    @staticmethod
    def _album_entry(man, tid):
        for a in (man or {}).get("albums") or []:
            if str(a.get("tid")) == str(tid):
                return a
        return None

    # ------------------------------------------------------------------
    def run(self):
        opts = self.options
        kw_dir = os.path.join(opts.out_dir, self.kw)
        os.makedirs(kw_dir, exist_ok=True)
        man_path = os.path.join(kw_dir, MANIFEST_NAME)

        man = load_manifest(man_path) if opts.resume else None
        if not man or man.get("kw") != self.kw:
            man = new_manifest(self.kw, self.source_url, opts.as_dict())

        self.log("输出目录：%s" % kw_dir)
        self.progress("准备", 0, 1, "正在获取相册列表")

        # ---- 1) 相册列表（唯一需要真实浏览器的步骤）----
        backend = create_backend(opts.browser or default_browser(),
                                 profile_dir=opts.profile_dir,
                                 log=self.log, headless=opts.headless, timeout=60)
        albums, list_html, list_url = [], "", self.source_url
        logged_in = False
        try:
            backend.start()
            logged_in = backend.is_logged_in()
            if logged_in:
                self.log("检测到已登录的百度账号会话。")
            else:
                self.log("当前未登录。注意：贴吧的「相册列表页」只对登录用户展示内容，"
                         "未登录大概率解析不到相册（图片与评论接口本身不需要登录）。")
            albums, list_html, list_url = fetch_album_list(backend, self.kw, log=self.log)
        finally:
            backend.close()

        if opts.save_raw and list_html:
            snapshot.write_raw_snapshot(os.path.join(kw_dir, "相册列表_原始快照.html"), list_html)
            try:
                css_rel = css_extract.extract_link_css(list_html, kw_dir, log=self.log)
                if css_rel:
                    self.log("已抽取贴吧核心 CSS（本地还原用）：%s" % css_rel)
            except Exception as e:
                self.log("核心 CSS 抽取失败（页面将用内置 fallback 兜底）：%s" % e)

        if self._stopped():
            self.log("已停止。")
            save_manifest(man_path, man)
            return man
        if not albums:
            self.log("未解析到任何相册。")
            if not logged_in:
                self.log("很可能的原因是「未登录」：贴吧的相册列表页不对未登录访客展示。")
                self.log("请先点「登录百度账号」，登录成功后再点「开始保存」。")
            else:
                self.log("已登录但仍未解析到，可能是贴吧改版或页面被安全验证拦截；"
                         "可稍后重试，或看 相册列表_原始快照.html 核对页面结构。")
            self.progress("结束", 1, 1, "未解析到相册")
            save_manifest(man_path, man)
            return man

        self.log("共 %d 本相册。" % len(albums))

        # ---- 2) 逐本相册 ----
        fetcher = Fetcher(delay=opts.delay, timeout=30, retries=3,
                          stop_event=self.stop_event, log=self.log)
        downloader = Downloader(fetcher, workers=opts.workers, log=self.log,
                               stop_event=self.stop_event)
        snapshot.write_static(kw_dir)

        taken = set()
        try:
            for ai, (name, tid, href) in enumerate(albums, 1):
                if self._stopped():
                    break
                safe = self._unique_name(sanitize(name, maxlen=60) or ("相册%s" % tid), taken)
                self._process_album(fetcher, downloader, kw_dir, man,
                                    name, safe, tid, href, ai, len(albums))
                save_manifest(man_path, man)          # 每本结束就落盘，便于中断续传
            self._write_index(kw_dir, man)
        except StopRequested:
            self.log("已停止。")
        finally:
            fetcher.close()
            save_manifest(man_path, man)

        stat = summarize(man)
        self.log("全部完成：%d 本相册，图片 %d 张（成功 %d / 失败 %d），评论 %d 条。"
                 % (stat["albums"], stat["images"], stat["ok"], stat["failed"], stat["comments"]))
        self.progress("完成", 1, 1, "共 %d 本相册 / %d 张图片" % (stat["albums"], stat["images"]))
        return man

    # ------------------------------------------------------------------
    def _process_album(self, fetcher, downloader, kw_dir, man,
                       album_name, safe, tid, href, ai, n_albums):
        log, opts = self.log, self.options
        log("=== [%d/%d] 相册「%s」tid=%s ===" % (ai, n_albums, album_name, tid))
        self.progress("相册 %d/%d" % (ai, n_albums), 0, 1, album_name)

        photos, meta = fetch_album_photos(
            fetcher, self.kw, tid, log=log,
            on_page=lambda p, got, tot: self.progress("清单", got, tot, "%s 第 %d 页" % (album_name, p)))
        if not photos:
            log("  没有取到图片，跳过。")
            return

        # 上次的记录（用于续传时复用评论，评论是最耗时的一步）
        old = self._album_entry(man, tid) or {}
        old_by_pid = {}
        for img in old.get("images") or []:
            if img.get("pic_id"):
                old_by_pid[img["pic_id"]] = img
        if old_by_pid:
            skip_n = len(completed_pic_ids(man, tid))
            if skip_n:
                log("  发现上次记录 %d 条，已下载的图片将跳过、评论将复用。" % skip_n)

        img_dir = os.path.join(kw_dir, safe, "图片")

        def _on_download(done, total, res):
            self.progress("下载 %s" % album_name, done, total,
                          res.get("descr") or ("第 %s 张" % res.get("index")))

        results = downloader.download_album(photos, img_dir, on_progress=_on_download)
        ok_n = sum(1 for r in results if r and r.get("ok"))
        skip_n = sum(1 for r in results if r and r.get("skipped"))
        log("  图片：%d 张，成功 %d 张（其中复用本地已有 %d 张）"
            % (len(results), ok_n, skip_n))

        # ---- 评论 ----
        entries = []
        total_photos = len(photos)
        for i, (photo, res) in enumerate(zip(photos, results), 1):
            res = res or {}
            comments, c_total = [], 0
            if opts.fetch_comments:
                if self._stopped():
                    log("  已停止，后续评论不再抓取。")
                else:
                    prev = old_by_pid.get(photo.pic_id) or {}
                    if opts.resume and prev.get("comments"):
                        comments = prev["comments"]
                        c_total = int(prev.get("comment_total") or len(comments))
                    else:
                        comments, c_total = fetch_comments(
                            fetcher, self.kw, tid, photo.pic_id,
                            max_comments=opts.max_comments, log=log,
                            page_delay=opts.comment_delay)
                self.progress("评论 %s" % album_name, i, total_photos,
                              "%s（%d 条）" % (photo.descr or "第 %d 张" % i, len(comments)))

            rel = None
            if res.get("ok") and res.get("path"):
                rel = (safe, "图片", os.path.basename(res["path"]))
            entries.append({
                "order": i,
                "pic_id": photo.pic_id,
                "descr": photo.descr,
                "remote": res.get("source") == "原图" and photo.original_url or (photo.original_url),
                "rel": rel,
                "file": os.path.basename(res["path"]) if res.get("path") else "",
                "ok": bool(res.get("ok")),
                "bytes": int(res.get("bytes") or 0),
                "ext": res.get("ext") or "",
                "width": photo.width, "height": photo.height,
                "error": res.get("error") or "",
                "comments": comments,
                "comment_total": c_total,
                "comment_count": len(comments),
            })

        if opts.save_raw:
            raw_path = os.path.join(kw_dir, safe, "原始数据.json")
            try:
                with open(raw_path, "w", encoding="utf-8") as fp:
                    json.dump({"tid": tid, "name": album_name, "meta": meta,
                               "photos": [{"index": p.index, "pic_id": p.pic_id,
                                           "descr": p.descr, "purl": p.purl}
                                          for p in photos],
                               "comments": {e["pic_id"]: e["comments"] for e in entries}},
                              fp, ensure_ascii=False, indent=2)
            except OSError as exc:
                log("  原始数据保存失败：%s" % exc)

        # ---- 离线 HTML ----
        html_name = safe + ".html"
        grabbed_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        src_url = "https://tieba.baidu.com" + (href or ("/p/" + tid))
        if opts.original:
            # 原版贴吧视觉还原：每本相册 = 一张墙页 + 每张图一个帖页
            post_dir = os.path.join(kw_dir, safe, "帖子")
            os.makedirs(post_dir, exist_ok=True)
            post_files = []
            for e in entries:
                stem = build_stem(int(e["order"]) - 1, len(entries), e.get("descr"),
                                  e.get("pic_id"), dirpath=post_dir, ext=".html")
                pf = stem + ".html"
                post_files.append(pf)
                snapshot.write_html(
                    os.path.join(post_dir, pf),
                    snapshot.build_post_html(self.kw, album_name, e, e.get("comments") or [],
                                             safe, pf, len(entries), root_prefix="../../",
                                             source_url=src_url, grabbed_at=grabbed_at))
            snapshot.write_html(
                os.path.join(kw_dir, html_name),
                snapshot.build_album_html(self.kw, album_name, entries, safe, post_files,
                                          tid=tid, source_url=src_url, grabbed_at=grabbed_at,
                                          album_index=ai, album_total=n_albums))
        else:
            snapshot.write_html(
                os.path.join(kw_dir, html_name),
                snapshot.build_album_html_legacy(self.kw, album_name, entries, tid=tid,
                                                 source_url=src_url, grabbed_at=grabbed_at,
                                                 album_index=ai, album_total=n_albums))

        cover = None
        for e in entries:
            if e.get("rel"):
                cover = e["rel"]
                break

        entry = {
            "tid": str(tid), "name": album_name, "dir": safe, "html": html_name,
            "href": href, "total_num": int(meta.get("total_num") or len(photos)),
            "saved": sum(1 for e in entries if e["ok"]),
            "failed": sum(1 for e in entries if not e["ok"]),
            "cover": list(cover) if cover else None,
            "images": entries,
        }
        albums = man.setdefault("albums", [])
        for idx, a in enumerate(albums):
            if str(a.get("tid")) == str(tid):
                albums[idx] = entry
                break
        else:
            albums.append(entry)

        log("=== 相册「%s」完成：%d/%d 张，评论 %d 条 ==="
            % (album_name, entry["saved"], len(entries),
               sum(e["comment_count"] for e in entries)))

    # ------------------------------------------------------------------
    def _write_index(self, kw_dir, man):
        albums = []
        for a in man.get("albums") or []:
            albums.append({
                "name": a.get("name"), "tid": a.get("tid"),
                "html": a.get("html") or "",
                "total": a.get("total_num") or len(a.get("images") or []),
                "saved": a.get("saved") or 0,
                "cover": tuple(a["cover"]) if a.get("cover") else None,
            })
        path = os.path.join(kw_dir, snapshot.INDEX_PAGE_NAME + ".html")
        snapshot.write_html(path, snapshot.build_bar_wall_html(
            self.kw, albums, source_url=self.source_url,
            grabbed_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
        self.log("已生成索引页（吧级相册墙）：%s" % os.path.basename(path))


# ---------------------------------------------------------------------------
# 顶层流程
# ---------------------------------------------------------------------------
def login_flow(profile_dir=None, log=None, browser=None):
    """打开浏览器窗口让用户登录，检测到 BDUSS 后关闭。"""
    log = log or (lambda msg: None)
    backend = create_backend(browser or default_browser(),
                             profile_dir=profile_dir or default_profile(), log=log)
    return backend.open_login("https://tieba.baidu.com/", timeout=360)


def crawl_flow(kw, out_dir, options=None, log=None, progress=None, stop_event=None):
    """抓取入口。kw 为吧名，out_dir 为输出根目录。"""
    options = options or CrawlOptions(out_dir=out_dir)
    options.out_dir = out_dir
    return Crawler(kw, options, log=log, progress=progress, stop_event=stop_event).run()


# ---------------------------------------------------------------------------
# 离线自检（不联网）
# ---------------------------------------------------------------------------
def _selftest():
    import tempfile
    from tbalbum.http import smart_decode
    from tbalbum.naming import build_stem, sanitize, seq_width, sniff_ext, unique_path
    from tbalbum.photo_api import parse_album_data
    from tbalbum.album_list import parse_album_list, strip_html_comments
    from tbalbum import snapshot as snap

    print("[1] GBK 解码")
    gbk_bytes = "相册名".encode("gbk")
    assert smart_decode(gbk_bytes) == "相册名", smart_decode(gbk_bytes)
    assert smart_decode(b'{"a":1}') == '{"a":1}'
    print("    OK：GBK 与 ASCII 均正确解码")

    print("[2] 命名：序号_描述")
    assert sanitize("a/b:c*d?e") == "a_b_c_d_e", sanitize("a/b:c*d?e")
    assert seq_width(708) == 3 and seq_width(9) == 3 and seq_width(1500) == 4
    assert build_stem(0, 708, "0将军刽子手0", "abc123") == "001_0将军刽子手0"
    assert build_stem(707, 708, "", "91c7df806538abcd") == "708_91c7df806538", \
        build_stem(707, 708, "", "91c7df806538abcd")
    long_stem = build_stem(0, 10, "x" * 300, "pid")
    assert len(long_stem) <= 80 + 4 + 1, len(long_stem)
    short = build_stem(0, 10, "y" * 300, "pid", dirpath="z" * 230, ext=".jpeg")
    assert 230 + 1 + len(short) + 5 <= 259, len(short)
    print("    OK：清洗 / 位宽 / 无描述回落 pic_id / 超长截断")

    print("[3] 扩展名嗅探")
    assert sniff_ext(b"\xff\xd8\xff\xe0" + b"\x00" * 20) == ".jpg"
    assert sniff_ext(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20) == ".png"
    assert sniff_ext(b"GIF89a" + b"\x00" * 20) == ".gif"
    # 无法识别时返回 default；下载器传入 default="" 来判定「不是图片」
    assert sniff_ext(b"<html><body>x</body>") == ".jpg"
    assert sniff_ext(b"<html><body>x</body>", "") == ""
    print("    OK：jpeg/png/gif 识别，HTML 被判为非图片")

    print("[4] 同名不覆盖")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "001_a.jpg")
    open(p, "wb").write(b"x")
    assert unique_path(p) == os.path.join(d, "001_a(2).jpg")
    print("    OK：追加 (2)")

    print("[5] 相册列表解析（注释包裹 + # 当引号）")
    fixture = """<html><body>
    <!--<div id="good_right"><div class="gr_block_main">
      <div class="grbm_ele_wrapper">
        <a class="grbm_ele_a grbm_ele_big" href="/p/1111111111"></a>
        <div class="grbm_ele_title"><a>第一本相册</a></div>
      </div>
      <div class="grbm_ele_wrapper">
        <a class="grbm_ele_a grbm_ele_big" href="/p/2222222222"></a>
        <div class="grbm_ele_title"><a>第二本/相册</a></div>
      </div>
    </div></div>-->
    </body></html>"""
    got = parse_album_list(fixture)
    assert ("第一本相册", "1111111111") in [(n, t) for n, t, _ in got], got
    assert ("第二本/相册", "2222222222") in [(n, t) for n, t, _ in got], got
    assert strip_html_comments("<!--<b>x</b>-->") == "<b>x</b>"
    assert parse_album_list("") == []
    print("    OK：解析到 %d 本相册，注释被正确揭开" % len(got))

    print("[6] /photo/p 的 albumData 解析")
    page = """<html><script>var albumData = {'comment_amount' : 36,
      'comment_list' : [{"thread_id":1,"post_id":9,"content":"\\u54c8\\u54c8<img class=\\"BDE_Smiley\\" src=\\"http://x.gif\\">","user_name":"小明","floor":2,"time":1330506241}],
      'desc' : '0\\u5c06\\u519b','pic_id' : 'ac54','img' : {"original":{"id":"ac54"},"medium":{"url":"https://imgsa.baidu.com/forum/pic/item/ac54.jpg"}}};
      _.Module.use('x');</script></html>"""
    ad = parse_album_data(page)
    assert ad is not None, "未解析到 albumData"
    assert ad["comment_amount"] == 36, ad["comment_amount"]
    assert ad["desc"] == "0将军", ad["desc"]
    assert ad["original_url"].endswith("ac54.jpg"), ad["original_url"]
    assert len(ad["comment_list"]) == 1
    print("    OK：评论 %d 条 / desc=%s / 原图=%s" % (len(ad["comment_list"]), ad["desc"], ad["original_url"]))

    print("[7] 离线 HTML（原版贴吧视觉）：墙页 + 帖页 + 索引墙")
    entries = [{
        "order": 1, "pic_id": "a" * 40, "descr": "测试图", "ok": True,
        "rel": ("测试相册", "图片", "001_测试图.jpg"), "bytes": 119842,
        "width": 330, "height": 518,
        "comments": [{"floor": 2, "user_name": "小明", "content": "哈哈哈", "time": 1330506241}],
        "comment_total": 1, "comment_count": 1,
    }]
    post_files = ["001_测试图.html"]
    wall = snap.build_album_html("红警3", "测试相册", entries, "测试相册", post_files, tid="1")
    assert "grbm_ele_wrapper" in wall, "墙页应有贴吧相册卡片结构"
    assert "./static/tieba_core.css" in wall, "墙页未引用本地核心 CSS"
    assert ("./%E6%B5%8B%E8%AF%95%E7%9B%B8%E5%86%8C/%E5%9B%BE%E7%89%87/"
            "001_%E6%B5%8B%E8%AF%95%E5%9B%BE.jpg") in wall, "图片未指向本地"
    assert "imgsa.baidu.com/forum/pic/item" not in wall, "仍残留远程图片地址"
    assert ("./%E6%B5%8B%E8%AF%95%E7%9B%B8%E5%86%8C/%E5%B8%96%E5%AD%90/"
            "001_%E6%B5%8B%E8%AF%95%E5%9B%BE.html") in wall, "墙页未链接帖页"
    assert "测试图" in wall, "墙页未显示图片描述（缩略图标题）"

    post = snap.build_post_html("红警3", "测试相册", entries[0], entries[0]["comments"],
                                "测试相册", "001_测试图.html", 1, root_prefix="../../")
    assert "p_thread" in post and "p_postlist" in post, "帖页应有贴吧原版 class"
    assert "../../static/tieba_core.css" in post, "帖页未引用本地核心 CSS"
    assert ("../../%E6%B5%8B%E8%AF%95%E7%9B%B8%E5%86%8C/%E5%9B%BE%E7%89%87/"
            "001_%E6%B5%8B%E8%AF%95%E5%9B%BE.jpg") in post, "帖页图片未指本地"
    assert "imgsa.baidu.com/forum/pic/item" not in post, "帖页残留远程图"
    assert "哈哈哈" in post and "小明" in post, "帖页评论未嵌入"

    idx = snap.build_bar_wall_html("红警3", [{"name": "测试相册", "tid": "1", "html": "测试相册.html",
                                          "total": 1, "saved": 1,
                                          "cover": ("测试相册", "图片", "001_测试图.jpg")}])
    assert "grbm_ele_wrapper" in idx, "索引墙应有相册卡片"
    assert "./%E6%B5%8B%E8%AF%95%E7%9B%B8%E5%86%8C.html" in idx, "索引页链接错误"

    legacy = snap.build_album_html_legacy("红警3", "测试相册", entries, tid="1")
    assert 'class="card"' in legacy, "旧网格模式应可用"
    print("    OK：墙页/帖页/索引墙均原版结构、图片本地、评论嵌入、引用本地 CSS；旧网格可用")

    print()
    print("=== 自检全部通过 ===")


def main(argv=None):
    argv = argv if argv is not None else sys.argv
    args, i = {}, 1
    while i < len(argv):
        a = argv[i]
        if a in ("--kw", "--out-dir", "--max-comments", "--workers") and i + 1 < len(argv):
            args[a[2:]] = argv[i + 1]
            i += 1
        elif a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            args[k] = v
        elif a.startswith("--"):
            args[a[2:]] = True
        i += 1

    if args.get("selftest"):
        if getattr(sys, "frozen", False):
            sys.stdout = _Tee(os.path.join(_frozen_base(), "selftest_result.txt"), None)
        _selftest()
        return 0
    if args.get("login"):
        return 0 if login_flow(log=print, browser=str(args.get("browser") or "playwright")) else 1

    link = str(args.get("kw") or "")
    kw = extract_kw(link) or link
    if not kw:
        print("用法: python crawler.py --kw 红警3 [--out-dir 目录] [--no-comments] "
              "[--browser playwright|cdp]")
        return 1
    out_dir = str(args.get("out-dir") or "").strip() or os.path.join(os.getcwd(), "输出")
    opts = CrawlOptions(
        out_dir=out_dir,
        fetch_comments=not bool(args.get("no-comments")),
        max_comments=int(args["max-comments"]) if args.get("max-comments") else None,
        workers=int(args.get("workers") or 4),
        browser=str(args.get("browser") or "playwright"),
    )
    crawl_flow(kw, out_dir, opts, log=print,
               progress=lambda s, c, t, m="": print("  [%s] %s/%s %s" % (s, c, t, m)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        if getattr(sys, "frozen", False):
            try:
                with open(os.path.join(_frozen_base(), "启动错误.log"), "w", encoding="utf-8") as _f:
                    _tb.print_exc(file=_f)
            except Exception:
                pass
        raise
