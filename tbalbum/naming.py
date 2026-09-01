# -*- coding: utf-8 -*-
"""文件名生成。

命名规则（用户确认）：`<序号>_<描述>.<扩展名>`，例如 `001_0将军刽子手0.jpg`。

实测要点：
- 贴吧**不保存上传者的原始文件名**，接口里只有 pic_id（40 位哈希）和 descr。
  descr 是可读真名（吧友真人秀=用户名，本吧吧徽=「动画版 by 真空菊爆器」）。
  descr 为空时回落到 pic_id 前 12 位。
- pic_list 没有 format 字段，扩展名一律按**下载内容的文件头嗅探**，不靠 URL 后缀猜。
- Windows 路径上限 260，描述需按剩余预算截断。
"""
import os
import re

INVALID_CHARS = r'[\\/:*?"<>|]'
CONTROL_CHARS = r"[\x00-\x1f\x7f]"
MAX_DESCR = 80
MIN_SEQ_WIDTH = 3
MAX_PATH = 259


def sanitize(text, maxlen=MAX_DESCR):
    """清洗为合法的 Windows 文件名片段。"""
    s = "" if text is None else str(text)
    s = re.sub(CONTROL_CHARS, " ", s)
    s = re.sub(INVALID_CHARS, "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Windows 下文件名不能以点或空格结尾
    s = s.strip(". ")
    if maxlen and len(s) > maxlen:
        s = s[:maxlen].rstrip()
    return s


def seq_width(total):
    """序号位宽：按总数自适应，最少 3 位（9 张也是 001..009，便于排序）。"""
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = 0
    return max(MIN_SEQ_WIDTH, len(str(max(1, total))))


def sniff_ext(data, default=".jpg"):
    """按文件头判断真实图片类型。"""
    if not data or len(data) < 12:
        return default
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return default


def build_stem(index, total, descr, pic_id, dirpath=None, ext=""):
    """生成 `001_描述` 形式的文件名主干（不含扩展名）。

    index 为 0 起；dirpath 给出时按 Windows 路径上限截短。
    """
    num = "%0*d" % (seq_width(total), int(index) + 1)
    name = sanitize(descr, MAX_DESCR)
    if not name:
        name = (pic_id or "")[:12] or ("img%s" % num)
    stem = "%s_%s" % (num, name)

    if dirpath:
        budget = MAX_PATH - len(str(dirpath)) - len(ext) - 1
        if budget < 12:
            budget = 12
        if len(stem) > budget:
            stem = stem[:budget].rstrip(". ")
    return stem


def unique_path(path):
    """目标已存在则追加 (2) (3)…… 避免覆盖。"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while True:
        cand = "%s(%d)%s" % (root, i, ext)
        if not os.path.exists(cand):
            return cand
        i += 1
