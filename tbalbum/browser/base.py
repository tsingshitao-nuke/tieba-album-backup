# -*- coding: utf-8 -*-
"""浏览器后端抽象。

为什么还需要浏览器：
    相册列表页 `f?kw=X&tab=album` 对 curl、iPhone UA、以及 Edge `--headless --dump-dom`
    **一律返回「百度安全验证」**（已逐一实测）。只有真实有头浏览器能过。
    而图片清单 / 原图 / 评论三个接口都**不需要浏览器**，走纯 HTTP 即可。

因此浏览器只负责两件事：登录、取相册列表页 DOM。
"""


class BrowserError(Exception):
    """浏览器不可用或操作失败。"""


class BrowserBackend(object):
    """浏览器后端接口。阶段一用 Playwright，阶段二用 CDP，上层代码不变。"""

    name = "base"

    def __init__(self, profile_dir=None, log=None, headless=False, timeout=60):
        self.profile_dir = profile_dir
        self.log = log or (lambda msg: None)
        self.headless = headless
        self.timeout = timeout
        self._started = False

    # ------------------------------------------------------------------
    def start(self):
        raise NotImplementedError

    def get_html(self, url, settle_ms=2500, scroll=False, max_scroll_rounds=20):
        """返回渲染完成后的完整 HTML。"""
        raise NotImplementedError

    def open_login(self, url, timeout=360):
        """打开登录窗口，等待用户完成登录。返回是否检测到登录态。"""
        raise NotImplementedError

    def is_logged_in(self):
        return False

    def cookies(self):
        return {}

    def close(self):
        self._started = False

    # ------------------------------------------------------------------
    def __enter__(self):
        if not self._started:
            self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
