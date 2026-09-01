# -*- coding: utf-8 -*-
"""CDP 浏览器后端（阶段二「瘦身版」）—— 用系统 Edge/Chrome + CDP 协议，不依赖 Playwright。

为什么能去掉 Playwright：
    Playwright 打包进来 100MB+（主要是它的 node 驱动）。而我们只需要浏览器做两件事：
    打开登录页、取相册列表页渲染后的 DOM。这两件事用 CDP 的
    `Page.navigate` + `Runtime.evaluate('document.documentElement.outerHTML')` 就够了，
    websocket-client 是纯 Python，体积可忽略。

注意事项：
    - **仍然必须是有头浏览器**：实测无头（含 Edge --headless --dump-dom）会被百度安全验证拦截。
    - 用独立的 --user-data-dir，不影响用户正常的 Edge 配置，登录态也能持久化。
"""
import json
import os
import socket
import subprocess
import time

from .base import BrowserBackend, BrowserError

try:
    import websocket                       # websocket-client
except ImportError:                        # pragma: no cover
    websocket = None

TIEBA_HOME = "https://tieba.baidu.com/"

BROWSER_CANDIDATES = [
    ("msedge", [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]),
    ("chrome", [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]),
]


def find_browser():
    """按 msedge → chrome 顺序找系统浏览器，返回 (名称, 路径)。"""
    for name, paths in BROWSER_CANDIDATES:
        for p in paths:
            if p and os.path.isfile(p):
                return name, p
    return None, None


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CdpBackend(BrowserBackend):
    name = "cdp"

    def __init__(self, profile_dir=None, log=None, headless=False, timeout=60,
                 viewport=None, browser_path=None):
        super(CdpBackend, self).__init__(profile_dir, log, headless, timeout)
        self.viewport = viewport
        self.browser_path = browser_path
        self._proc = None
        self._port = None
        self._ws = None
        self._msg_id = 0
        self._page_ws = None
        self._target_ws_url = ""

    # ------------------------------------------------------------------
    def _http_json(self, path, retries=60, interval=0.5):
        import urllib.request
        url = "http://127.0.0.1:%d%s" % (self._port, path)
        last = None
        for _ in range(retries):
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    return json.loads(resp.read().decode("utf-8", "ignore"))
            except Exception as exc:                        # noqa: BLE001
                last = exc
                time.sleep(interval)
        raise BrowserError("CDP 调试端口无响应：%s" % last)

    def _ws_connect(self, url, timeout=30):
        if websocket is None:
            raise BrowserError("缺少 websocket-client，请先 pip install websocket-client")
        return websocket.create_connection(url, timeout=timeout,
                                           suppress_origin=True)

    def _send(self, ws, method, params=None, wait=True):
        self._msg_id += 1
        mid = self._msg_id
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        if not wait:
            return None
        deadline = time.time() + max(10, self.timeout)
        while time.time() < deadline:
            raw = ws.recv()
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except Exception:                               # noqa: BLE001
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise BrowserError("CDP %s 失败：%s" % (method, msg["error"]))
                return msg.get("result") or {}
        raise BrowserError("CDP %s 超时" % method)

    # ------------------------------------------------------------------
    def start(self):
        name, path = (self.name, self.browser_path) if self.browser_path else find_browser()
        if not path:
            _, path = find_browser()
            name = "msedge"
        if not path or not os.path.isfile(path):
            raise BrowserError(
                "未找到 Microsoft Edge 或 Google Chrome。"
                "CDP 版依赖系统浏览器，请安装其一，或改用 Playwright 版。")

        os.makedirs(self.profile_dir or ".", exist_ok=True)
        self._port = _free_port()
        cmd = [path,
               "--remote-debugging-port=%d" % self._port,
               "--user-data-dir=%s" % os.path.abspath(self.profile_dir),
               "--no-first-run", "--no-default-browser-check",
               "--disable-blink-features=AutomationControlled",
               "--disable-popup-blocking"]
        if self.viewport:
            cmd.append("--window-size=%d,%d" % (self.viewport.get("width", 1400),
                                                self.viewport.get("height", 900)))
        cmd.append("about:blank")
        self.log("正在启动浏览器（%s，CDP 端口 %d）…" % (name, self._port))
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        except Exception as exc:                            # noqa: BLE001
            raise BrowserError("启动浏览器失败：%s" % exc)

        # 等调试端口就绪
        try:
            self._http_json("/json/version", retries=max(20, int(self.timeout)))
        except BrowserError:
            self.close()
            raise BrowserError("浏览器已启动但 CDP 调试端口未就绪（可能被安全软件拦截）。")

        # 连到已存在的页面 target
        self._attach_page()
        # CDP 的 cookie 与「当前页面源」绑定：停在 about:blank 时 Network.getCookies 返回 0 个，
        # 会让 is_logged_in() 永远为假。先落到贴吧首页建立 cookie 上下文。
        try:
            self._send(self._ws, "Page.navigate", {"url": TIEBA_HOME})
            self._wait_ready(timeout=20)
        except Exception:                                   # noqa: BLE001
            pass
        self._started = True
        return self

    def _attach_page(self):
        targets = self._http_json("/json/list", retries=10, interval=0.5) or []
        page = None
        for t in targets:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                page = t
                break
        if not page:
            raise BrowserError("未找到可用的页面 target")
        self._target_ws_url = page["webSocketDebuggerUrl"]
        self._ws = self._ws_connect(self._target_ws_url)
        try:
            self._send(self._ws, "Page.enable")
            self._send(self._ws, "Runtime.enable")
            self._send(self._ws, "Network.enable")
        except BrowserError:
            pass
        return self._ws

    # ------------------------------------------------------------------
    def _evaluate(self, expression, timeout=None):
        res = self._send(self._ws, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        value = ((res or {}).get("result") or {}).get("value")
        return value

    def _wait_ready(self, timeout=None):
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            try:
                state = self._evaluate("document.readyState")
            except Exception:                               # noqa: BLE001
                state = None
            if state == "complete":
                return True
            time.sleep(0.4)
        return False

    def get_html(self, url, settle_ms=2500, scroll=False, max_scroll_rounds=20):
        if not self._started:
            self.start()
        self.log("  打开页面：%s" % url)
        self._send(self._ws, "Page.navigate", {"url": url})
        self._wait_ready()
        if scroll:
            self.scroll_to_bottom(max_scroll_rounds)
        if settle_ms:
            time.sleep(settle_ms / 1000.0)
        html = self._evaluate("document.documentElement.outerHTML") or ""
        # 安全验证检测
        if "百度安全验证" in html[:4000]:
            self.log("  检测到「百度安全验证」，请在弹出的浏览器窗口中完成验证…")
            for _ in range(int(self.timeout / 2)):
                time.sleep(2)
                html = self._evaluate("document.documentElement.outerHTML") or ""
                if "百度安全验证" not in html[:4000]:
                    break
        return html

    def scroll_to_bottom(self, max_rounds=20, delay=1.0):
        last = -1
        for _ in range(max(1, int(max_rounds))):
            try:
                self._evaluate("window.scrollTo(0, document.body.scrollHeight);"
                               "document.body.scrollHeight")
            except Exception:                               # noqa: BLE001
                break
            time.sleep(delay)
            try:
                h = self._evaluate("document.body.scrollHeight")
            except Exception:                               # noqa: BLE001
                break
            if h == last:
                break
            last = h
        return last

    # ------------------------------------------------------------------
    def cookies(self):
        try:
            res = self._send(self._ws, "Network.getCookies")
            return {c.get("name"): c.get("value") for c in (res or {}).get("cookies") or []}
        except Exception:                                   # noqa: BLE001
            return {}

    def is_logged_in(self):
        if "BDUSS" in self.cookies():
            return True
        # 兜底：贴吧首页登录后顶栏会出现用户名元素（cookie 读不到时仍可判断）
        try:
            if self._evaluate("!!document.querySelector('.u_username')"):
                return True
        except Exception:                                   # noqa: BLE001
            pass
        return False

    def open_login(self, url, timeout=360):
        if not self._started:
            self.start()
        self.log("请在打开的浏览器窗口中登录百度账号…")
        try:
            self._send(self._ws, "Page.navigate", {"url": url})
            self._wait_ready()
        except Exception as exc:                            # noqa: BLE001
            self.log("导航失败：%s" % exc)

        start = time.time()
        ok = False
        while time.time() - start < timeout:
            if self.is_logged_in():
                ok = True
                break
            time.sleep(2)
        self.log("登录成功，会话已保存。" if ok else "登录超时或未检测到登录态（图片接口本身不需要登录）。")
        self.close()
        return ok

    # ------------------------------------------------------------------
    def close(self):
        for ws in (self._ws,):
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
        self._ws = None
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=8)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        super(CdpBackend, self).close()
