# -*- coding: utf-8 -*-
"""贴吧相册备份 —— 纯逻辑包（不直接依赖浏览器，便于离线自检）。

分层：
    http        统一 HTTP：GBK 解码 / UA+Referer / 重试退避 / 限速
    naming      文件名：序号_描述.ext、清洗、截断、去重、扩展名嗅探
    album_api   相册图片清单（picture/list 的 ps/pe 分页）
    photo_api   单图信息 + 评论分页（/photo/p）
    downloader  并发下载 + 文件头校验 + 续传
    snapshot    离线 HTML 生成
    manifest    结果清单 + 断点续传
    browser     浏览器后端（仅相册列表页需要真实浏览器过安全验证）
"""

__version__ = "2.0.0"

# ---------------------------------------------------------------------------
# 贴吧接口常量集中在此，改版时只改这里（全部经 curl 实测验证）
# ---------------------------------------------------------------------------
TIEBA_HOST = "https://tieba.baidu.com"

# 相册列表页（必须真实浏览器，curl/无头一律返回「百度安全验证」）
ALBUM_LIST_URL = TIEBA_HOST + "/f?kw={kw}&ie=utf-8&tab=album"

# 相册图片清单：ps/pe 区间分页，每页最多 200。实测 200+200+200+108=708 全部取到。
# 注意：pic_id 游标分页（&prev=0&pic_id=xxx&next=40）会从头返回，不可用。
PICTURE_LIST_URL = TIEBA_HOST + "/photo/g/bw/picture/list?kw={kw}&alt=jview&rn=200&tid={tid}&pn=1&ps={ps}&pe={pe}&info=1"
PAGE_SIZE = 200

# 单图详情页：内嵌 albumData（含 desc / 原图 url / comment_list），pn 为评论页码
PHOTO_PAGE_URL = TIEBA_HOST + "/photo/p?kw={kw}&ie=utf-8&flux=1&tid={tid}&pic_id={pic_id}&pn={pn}&fp=2&see_lz=1"
COMMENTS_PER_PAGE = 10      # 实测每页 10 条评论

# 原图直链（无需登录，需带 Referer）
ORIGINAL_IMG_URL = "https://imgsa.baidu.com/forum/pic/item/{pic_id}.jpg"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0")
REFERER = TIEBA_HOST + "/"
