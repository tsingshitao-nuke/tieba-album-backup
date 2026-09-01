# -*- coding: utf-8 -*-
"""图片下载：并发 + 文件头校验 + 断点续传。

实测要点：
- 原图直链 `https://imgsa.baidu.com/forum/pic/item/<40位pic_id>.jpg` 无需登录，
  必须带 Referer，否则可能被防盗链拦截。
- pic_list 没有 format 字段，扩展名一律**按下载内容的文件头嗅探**，不靠 URL 后缀猜。
- 下载失败时依次回退到 purl（中等尺寸）→ murl（缩略图）。
- 已存在且大小正常的文件直接跳过 —— 中断后重跑不会重复下载。
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .http import StopRequested
from .naming import build_stem, sniff_ext, unique_path

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
# 预算时按最长扩展名预留，避免实际扩展名更长导致路径超限
_EXT_BUDGET = ".jpeg"
MIN_VALID_BYTES = 1024


class Downloader(object):
    def __init__(self, fetcher, workers=4, log=None, stop_event=None,
                 min_valid_bytes=MIN_VALID_BYTES):
        self.fetcher = fetcher
        self.workers = max(1, int(workers or 1))
        self.log = log or (lambda msg: None)
        self.stop_event = stop_event
        self.min_valid_bytes = int(min_valid_bytes or 0)
        self._lock = threading.Lock()
        self._done = 0

    # ------------------------------------------------------------------
    def _find_existing(self, dirpath, stem):
        """续传：已存在同名（任意图片扩展名）且大小正常的文件直接复用。"""
        for ext in IMAGE_EXTS:
            path = os.path.join(dirpath, stem + ext)
            if not os.path.exists(path):
                continue
            try:
                if os.path.getsize(path) >= self.min_valid_bytes:
                    return path
            except OSError:
                pass
        return ""

    def download_photo(self, photo, dirpath, total):
        """下载单张，返回结果 dict（不抛异常，失败记录 error）。"""
        stem = build_stem(photo.index, total, photo.descr, photo.pic_id,
                          dirpath=dirpath, ext=_EXT_BUDGET)
        base = {
            "index": photo.index,
            "pic_id": photo.pic_id,
            "descr": photo.descr,
            "stem": stem,
            "path": "",
            "ok": False,
            "skipped": False,
            "bytes": 0,
            "ext": "",
            "error": "",
            "source": "",
        }

        if self.stop_event is not None and self.stop_event.is_set():
            base["error"] = "已停止"
            return base

        existing = self._find_existing(dirpath, stem)
        if existing:
            base.update(ok=True, skipped=True, path=existing,
                        ext=os.path.splitext(existing)[1].lower())
            try:
                base["bytes"] = os.path.getsize(existing)
            except OSError:
                pass
            base["source"] = "本地已存在"
            return base

        candidates = []
        for url, tag in ((photo.original_url, "原图"), (photo.purl, "purl"), (photo.murl, "murl")):
            if url and url not in [c[0] for c in candidates]:
                candidates.append((url, tag))

        last_err = "无可用图片地址"
        for url, tag in candidates:
            if self.stop_event is not None and self.stop_event.is_set():
                last_err = "已停止"
                break
            try:
                data, _ = self.fetcher.get_bytes(url)
            except StopRequested:
                last_err = "已停止"
                break
            except Exception as exc:                        # noqa: BLE001
                last_err = "%s 获取失败：%s" % (tag, exc)
                continue

            ext = sniff_ext(data, "")
            if not ext:
                last_err = "%s 返回非图片内容（%d 字节）" % (tag, len(data))
                continue

            try:
                os.makedirs(dirpath, exist_ok=True)
                path = unique_path(os.path.join(dirpath, stem + ext))
                with open(path, "wb") as fp:
                    fp.write(data)
            except OSError as exc:
                last_err = "写入失败：%s" % exc
                continue

            base.update(ok=True, path=path, bytes=len(data), ext=ext,
                        source=tag, error="")
            return base

        base["error"] = last_err
        return base

    # ------------------------------------------------------------------
    def download_album(self, photos, dirpath, on_progress=None):
        """并发下载整本相册。返回按原顺序排列的结果列表。

        on_progress(done, total, result) 在**工作线程**中调用，GUI 侧需自行转主线程。
        """
        os.makedirs(dirpath, exist_ok=True)
        total = len(photos)
        results = [None] * total
        if total == 0:
            return results

        # Downloader 实例会被多本相册复用，计数必须按「本相册」归零，
        # 否则会出现「847/708」这种累计值配单本总数的错误进度。
        with self._lock:
            self._done = 0

        def _bump(res):
            with self._lock:
                self._done += 1
                done = self._done
            if on_progress:
                try:
                    on_progress(done, total, res)
                except Exception:
                    pass

        if self.workers == 1:
            for i, photo in enumerate(photos):
                res = self.download_photo(photo, dirpath, total)
                results[i] = res
                _bump(res)
            return results

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.download_photo, photo, dirpath, total): i
                       for i, photo in enumerate(photos)}
            try:
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:                 # noqa: BLE001
                        res = {"index": photos[i].index, "pic_id": photos[i].pic_id,
                               "descr": photos[i].descr, "ok": False, "skipped": False,
                               "path": "", "bytes": 0, "ext": "", "stem": "",
                               "error": "异常：%s" % exc, "source": ""}
                    results[i] = res
                    _bump(res)
            finally:
                for fut in futures:
                    fut.cancel()
        return results
