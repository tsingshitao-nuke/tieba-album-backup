# -*- coding: utf-8 -*-
"""Playwright 浏览器后端（阶段一，已在本机真机验证可用）。

要点：
- **必须 headless=False**。实测无头（含 Edge --headless --dump-dom）会被安全验证拦截。
- 复用系统已装浏览器，回退链 msedge → chrome → 内置 Chromium。
- 持久化 user-data-dir，登录一次后长期免登录。
"""
import time

from .base import BrowserBackend, BrowserError


class PlaywrightBackend(BrowserBackend):
    name = "playwright"

    CHANNELS = ["msedge", "chrome", None]
    CHANNEL_NAMES = {"msedge": "Microsoft Edge", "chrome": "Google Chrome",
                     None: "内置 Chromium"}
    LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled",
                   "--no-first-run", "--no-default-browser-check"]

    def __init__(self, profile_dir=None, log=None, headless=False, timeout=60,
                 viewport=None):
        super(PlaywrightBackend, self).__init__(profile_dir, log, headless, timeout)
        self.viewport = viewport or {"width": 1400, "height": 900}
        self._pw = None
        self._ctx = None
        self._page = None
        self._channel = None

    # ------------------------------------------------------------------
    def start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError("缺少 playwright，请先 pip install playwright（%s）" % exc)

        self.log("正在启动浏览器…")
        self._pw = sync_playwright().start()
        last_err = None
        for ch in self.CHANNELS:
            label = self.CHANNEL_NAMES.get(ch, ch)
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(
                    self.profile_dir, channel=ch, headless=self.headless,
                    viewport=self.viewport, args=self.LAUNCH_ARGS)
                self._channel = ch
                self.log("已启动浏览器：%s" % label)
                break
            except Exception as exc:                        # noqa: BLE001
                last_err = exc
                self.log("尝试 %s 失败：%s" % (label, exc))
        if self._ctx is None:
            self._safe_stop()
            raise BrowserError(
                "找不到可用浏览器（已尝试 Edge / Chrome / 内置 Chromium）。"
                "请安装 Microsoft Edge 或 Google Chrome；若要用内置 Chromium 请先执行 "
                "`playwright install chromium`。最后一次错误：%s" % last_err)

        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._started = True
        return self

    # ------------------------------------------------------------------
    @property
    def page(self):
        return self._page

    def cookies(self):
        try:
            return {c["name"]: c["value"] for c in self._ctx.cookies()}
        except Exception:
            return {}

    def is_logged_in(self):
        return "BDUSS" in self.cookies()

    def _title(self):
        try:
            return self._page.title() or ""
        except Exception:
            return ""

    def _wait_verification(self, timeout=180):
        """等待百度安全验证消失。"""
        start = time.time()
        warned = False
        while time.time() - start < timeout:
            title = self._title()
            if "安全验证" in title:
                if not warned:
                    self.log("  检测到「百度安全验证」，请在弹出的浏览器窗口中完成验证…")
                    warned = True
                time.sleep(2)
                continue
            try:
                if self._page.locator("#good_right").count() > 0:
                    return True
            except Exception:
                pass
            html = ""
            try:
                html = self._page.content()
            except Exception:
                pass
            if "grbm_ele_wrapper" in html or "ag_container" in html:
                return True
            time.sleep(1)
        if warned:
            self.log("  安全验证等待超时，继续尝试解析…")
        return False

    def get_html(self, url, settle_ms=2500, scroll=False, max_scroll_rounds=20):
        if not self._started:
            self.start()
        self.log("  打开页面：%s" % url)
        self._page.goto(url, wait_until="load", timeout=self.timeout * 1000)
        self._wait_verification()
        if scroll:
            self.scroll_to_bottom(max_scroll_rounds)
        if settle_ms:
            self._page.wait_for_timeout(int(settle_ms))
        return self._page.content()

    def scroll_to_bottom(self, max_rounds=20, delay=1.2):
        """滚动到底部，直到页面高度不再变化（相册列表可能懒加载）。"""
        last = -1
        for _ in range(max(1, int(max_rounds))):
            try:
                self._page.mouse.wheel(0, 4000)
            except Exception:
                break
            time.sleep(delay)
            try:
                h = self._page.evaluate("document.body.scrollHeight")
            except Exception:
                break
            if h == last:
                break
            last = h
        return last

    def open_login(self, url, timeout=360):
        """打开登录窗口，轮询 BDUSS，登录后关闭浏览器。"""
        if not self._started:
            self.start()
        self.log("请在打开的浏览器窗口中登录百度账号…")
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
        except Exception as exc:                            # noqa: BLE001
            self.log("导航失败：%s" % exc)

        start = time.time()
        ok = False
        while time.time() - start < timeout:
            if self.is_logged_in():
                ok = True
                break
            time.sleep(2)
        if ok:
            self.log("登录成功，会话已保存到本地，之后无需再登录。")
        else:
            self.log("登录超时（%.0f 秒）或未检测到登录态。" % timeout)
        self.close()
        return ok

    # ------------------------------------------------------------------
    def _safe_stop(self):
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None

    def close(self):
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        self._ctx = None
        self._page = None
        self._safe_stop()
        super(PlaywrightBackend, self).close()
