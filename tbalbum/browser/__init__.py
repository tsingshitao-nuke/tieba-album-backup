# -*- coding: utf-8 -*-
"""浏览器后端。只有相册列表页需要真实浏览器（贴吧对 curl / 无头 CLI 一律返回安全验证）。"""

from .base import BrowserBackend, BrowserError          # noqa: F401

try:
    from .playwright_backend import PlaywrightBackend   # noqa: F401
except Exception:                                       # pragma: no cover
    PlaywrightBackend = None


def create_backend(kind="playwright", **kwargs):
    """按名称创建后端。kind: playwright / cdp。"""
    kind = (kind or "playwright").lower()
    if kind == "playwright":
        if PlaywrightBackend is None:
            raise BrowserError("Playwright 后端不可用（缺少 playwright 依赖）")
        return PlaywrightBackend(**kwargs)
    if kind in ("cdp", "edge"):
        from .cdp_backend import CdpBackend
        return CdpBackend(**kwargs)
    raise BrowserError("未知浏览器后端：%s" % kind)
