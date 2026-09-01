# -*- coding: utf-8 -*-
"""统一 HTTP 层。

实测要点（务必保留）：
- 贴吧接口**全站 GBK 编码**，直接按 utf-8 解析会抛异常或产生乱码 → smart_decode 做
  「Content-Type charset → utf-8 → gbk → gb18030」的严格解码回退。
- 相册/图片接口均**无需登录**，但必须带 UA + Referer，否则容易被判爬虫。
"""
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter

from . import USER_AGENT, REFERER


class FetchError(Exception):
    """请求最终失败。"""


class StopRequested(Exception):
    """用户点了「停止」。"""


def smart_decode(data, content_type=""):
    """按 Content-Type charset → utf-8 → gbk → gb18030 严格解码回退。

    用 strict 而非 ignore：贴吧 JSON 是 GBK，utf-8 严格解码会失败从而正确落到 gbk；
    若用 ignore 则可能把 GBK 内容硬解成 utf-8 乱码。
    """
    order = []
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        order.append(m.group(1))
    order += ["utf-8", "gbk", "gb18030"]

    seen = set()
    for enc in order:
        key = enc.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return data.decode(enc, "strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("gb18030", "ignore")


class Fetcher(object):
    """带限速、重试、停止信号的 HTTP 客户端。"""

    def __init__(self, delay=0.0, timeout=30, retries=3, stop_event=None, log=None):
        self.delay = max(0.0, float(delay or 0))
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.stop_event = stop_event
        self.log = log or (lambda msg: None)

        # Session 不保证线程安全，并发下载时每个线程独立创建一份（见 session 属性）
        self._local = threading.local()
        self._sessions = []
        self._sessions_lock = threading.Lock()

    # ------------------------------------------------------------------
    def _make_session(self):
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "*/*",
        })
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        with self._sessions_lock:
            self._sessions.append(sess)
        return sess

    @property
    def session(self):
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = self._make_session()
            self._local.session = sess
        return sess

    # ------------------------------------------------------------------
    def _check_stop(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise StopRequested()

    def _throttle(self):
        if self.delay > 0:
            # 分段 sleep，保证「停止」能及时响应
            end = time.time() + self.delay
            while time.time() < end:
                self._check_stop()
                time.sleep(min(0.1, end - time.time()))

    def get_bytes(self, url, referer=REFERER, headers=None, timeout=None):
        """返回 (bytes, status_code)。失败抛 FetchError。"""
        hdrs = {"Referer": referer or REFERER}
        if headers:
            hdrs.update(headers)

        last_err = None
        for attempt in range(self.retries):
            self._check_stop()
            self._throttle()
            try:
                resp = self.session.get(url, headers=hdrs,
                                        timeout=timeout or self.timeout)
                if resp.status_code == 200:
                    return resp.content, resp.status_code
                last_err = "HTTP %s" % resp.status_code
            except StopRequested:
                raise
            except Exception as exc:                       # noqa: BLE001
                last_err = str(exc)

            if attempt < self.retries - 1:
                backoff = min(8.0, 1.0 * (2 ** attempt))
                self.log("  重试 %d/%d（%.0fs 后）：%s" % (attempt + 1, self.retries, backoff, last_err))
                end = time.time() + backoff
                while time.time() < end:
                    self._check_stop()
                    time.sleep(min(0.2, end - time.time()))

        raise FetchError("%s（%s）" % (last_err, url))

    def get_text(self, url, referer=REFERER, headers=None, timeout=None):
        """取文本，自动处理 GBK 编码。返回 str。"""
        data, _ = self.get_bytes(url, referer=referer, headers=headers, timeout=timeout)
        return smart_decode(data)

    def get_json(self, url, referer=REFERER, headers=None, timeout=None):
        """取 JSON。贴吧 JSON 为 GBK，先解码再 loads。"""
        text = self.get_text(url, referer=referer, headers=headers, timeout=timeout)
        import json
        try:
            return json.loads(text)
        except Exception:
            # 容错：去掉可能的 JSONP 包裹
            m = re.search(r"\(\s*(\{.*\})\s*\)\s*;?\s*$", text, re.S)
            if m:
                return json.loads(m.group(1))
            raise

    def close(self):
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions = []
        for sess in sessions:
            try:
                sess.close()
            except Exception:
                pass
