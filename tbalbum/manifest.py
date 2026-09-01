# -*- coding: utf-8 -*-
"""抓取清单（manifest.json）—— 结果记录 + 断点续传依据。"""
import datetime
import json
import os
import tempfile

MANIFEST_VERSION = 2
MANIFEST_NAME = "manifest.json"


def new_manifest(kw, source_url="", options=None):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    return {
        "version": MANIFEST_VERSION,
        "kw": kw,
        "source_url": source_url or "",
        "grabbed_at": now,
        "updated_at": now,
        "options": options or {},
        "albums": [],
    }


def load(path):
    """读取旧清单；不存在或损坏返回 None。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def save(path, data):
    """原子写入（先写临时文件再替换），避免中断产生半截 JSON。"""
    data = dict(data or {})
    data["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def completed_pic_ids(data, tid):
    """返回该相册已成功保存的 pic_id 集合，用于续传跳过。"""
    out = set()
    for album in (data or {}).get("albums") or []:
        if str(album.get("tid")) != str(tid):
            continue
        for img in album.get("images") or []:
            if img.get("ok") and img.get("pic_id"):
                out.add(img["pic_id"])
    return out


def summarize(data):
    """统计：相册数 / 图片总数 / 成功 / 失败 / 评论数。"""
    albums = (data or {}).get("albums") or []
    total = ok = failed = comments = 0
    for album in albums:
        imgs = album.get("images") or []
        total += len(imgs)
        for img in imgs:
            if img.get("ok"):
                ok += 1
            else:
                failed += 1
            comments += int(img.get("comment_count") or 0)
    return {"albums": len(albums), "images": total, "ok": ok,
            "failed": failed, "comments": comments}
