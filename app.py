# -*- coding: utf-8 -*-
"""贴吧相册备份工具 —— tkinter 图形界面。

运行: python app.py
打包: 见 build.bat 与 tieba_album_getter.spec

线程模型：所有抓取工作放在 daemon 工作线程里；工作线程只往 queue 里投递消息，
由主线程 after() 轮询后更新控件（tkinter 控件只能在主线程改）。
"""
import os
import queue
import sys
import threading
import tkinter as tk
import traceback
from tkinter import ttk, filedialog, messagebox

import crawler
from crawler import CrawlOptions, crawl_flow, default_out_dir_suggestion, default_profile, extract_kw
from tbalbum.manifest import summarize

APP_TITLE = "贴吧相册备份工具"


def _write_crash_log(exc_tuple):
    """未捕获异常写到 exe 旁的 启动错误.log（窗口程序看不到 traceback）。"""
    try:
        base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
        with open(os.path.join(base, "启动错误.log"), "w", encoding="utf-8") as f:
            traceback.print_exception(*exc_tuple, file=f)
    except Exception:
        pass


if getattr(sys, "frozen", False):
    sys.excepthook = _write_crash_log
    try:
        tk.Tk.report_callback_exception = staticmethod(lambda *a: _write_crash_log(a))
    except Exception:
        pass


class App(tk.Tk):
    def __init__(self):
        super(App, self).__init__()
        self.title(APP_TITLE)
        self.geometry("760x640")
        self.minsize(680, 560)

        self.q = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.last_out_dir = ""

        self._build_ui()
        self.after(120, self._drain)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 5}

        box = ttk.Frame(self)
        box.pack(fill="x", **pad)

        ttk.Label(box, text="相册链接 / 贴吧名：").grid(row=0, column=0, sticky="w")
        self.link_var = tk.StringVar()
        self.link_var.set("https://tieba.baidu.com/f?kw=%E7%BA%A2%E8%AD%A63&ie=utf-8&tab=album")
        ttk.Entry(box, textvariable=self.link_var).grid(row=0, column=1, sticky="ew", columnspan=2)
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="输出目录：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.out_var = tk.StringVar()          # 留空 → 点「开始保存」时再让用户选
        ttk.Entry(box, textvariable=self.out_var).grid(
            row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(box, text="浏览…", command=self._pick_out).grid(row=1, column=2, pady=(8, 0), padx=6)

        # ---- 选项 ----
        opt = ttk.LabelFrame(self, text="选项")
        opt.pack(fill="x", **pad)
        self.comments_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="抓取评论（含楼层、用户名、时间）",
                        variable=self.comments_var,
                        command=self._sync_comment_limit).pack(side="left", padx=(10, 14))

        self.original_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="原版贴吧视觉还原（取消=旧网格）",
                        variable=self.original_var).pack(side="left", padx=(0, 14))

        ttk.Label(opt, text="每图最多").pack(side="left")
        self.limit_var = tk.StringVar()
        self.limit_entry = ttk.Entry(opt, textvariable=self.limit_var, width=6)
        self.limit_entry.pack(side="left")
        ttk.Label(opt, text="条（留空=全部）").pack(side="left", padx=(0, 14))

        ttk.Label(opt, text="并发").pack(side="left")
        self.workers_var = tk.StringVar(value="4")
        ttk.Spinbox(opt, from_=1, to=16, width=4, textvariable=self.workers_var).pack(side="left")

        # ---- 按钮 ----
        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.btn_login = ttk.Button(btns, text="① 登录百度账号（必需）", command=self._do_login)
        self.btn_login.pack(side="left", padx=3)
        self.btn_start = ttk.Button(btns, text="② 开始保存", command=self._do_start)
        self.btn_start.pack(side="left", padx=3)
        self.btn_stop = ttk.Button(btns, text="停止", command=self._do_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=3)
        ttk.Button(btns, text="打开输出目录", command=self._open_out).pack(side="left", padx=3)

        # ---- 进度 ----
        prog = ttk.Frame(self)
        prog.pack(fill="x", **pad)
        self.stage_var = tk.StringVar(value="就绪")
        ttk.Label(prog, textvariable=self.stage_var).pack(anchor="w")
        self.pb = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.pb.pack(fill="x", pady=(3, 0))

        # ---- 日志 ----
        ttk.Label(self, text="运行日志：").pack(anchor="w", padx=10)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.log_box = tk.Text(frame, wrap="word", state="disabled",
                               font=("Microsoft YaHei UI", 9))
        sb = ttk.Scrollbar(frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=sb.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._log("使用步骤：① 先点「登录百度账号」登录一次（贴吧的相册列表页只对登录用户展示，"
                  "登录一次后长期免登录）；② 粘贴相册链接，点「开始保存」。")
        self._log("说明：图片清单、原图、评论这三个接口本身不需要登录，只有「拿到相册列表」这一步需要。")

    # ------------------------------------------------------------------
    def _sync_comment_limit(self):
        state = "normal" if self.comments_var.get() else "disabled"
        self.limit_entry.configure(state=state)

    def _pick_out(self):
        initial = self.out_var.get().strip() or self.last_out_dir or default_out_dir_suggestion()
        d = filedialog.askdirectory(initialdir=initial or None, title="选择保存位置")
        if d:
            self.out_var.set(d)
            self.last_out_dir = d

    def _open_out(self):
        d = self.out_var.get().strip() or self.last_out_dir
        if not d:
            messagebox.showinfo(APP_TITLE, "请先选择输出目录。")
            return
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception as exc:                            # noqa: BLE001
            self._log("无法打开目录：%s" % exc)

    # ------------------------------------------------------------------
    # 队列 → 主线程
    # ------------------------------------------------------------------
    def _log(self, msg):
        self.q.put(("log", str(msg)))

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "prog":
                    _, stage, cur, total, msg = item
                    self._update_progress(stage, cur, total, msg)
                elif kind == "done":
                    self._on_finished(item[1])
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _append_log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _update_progress(self, stage, cur, total, msg):
        try:
            cur = float(cur)
            total = float(total)
            pct = 0 if total <= 0 else min(100.0, max(0.0, cur * 100.0 / total))
        except (TypeError, ValueError):
            pct = 0
        self.pb.configure(value=pct)
        text = "%s（%.0f%%）" % (stage, pct)
        if msg:
            text += "  %s" % msg
        self.stage_var.set(text)

    def _on_finished(self, stats):
        self._set_busy(False)
        if stats:
            if stats.get("albums", 0) == 0:
                messagebox.showwarning(
                    APP_TITLE,
                    "没有解析到任何相册。\n\n"
                    "最常见原因是「尚未登录百度账号」——贴吧的相册列表页不对未登录访客展示内容。\n\n"
                    "请先点「① 登录百度账号」，登录成功后再点「② 开始保存」。")
            else:
                messagebox.showinfo(APP_TITLE,
                                    "保存完成！\n\n"
                                    "相册 %d 本\n图片 %d 张（成功 %d / 失败 %d）\n评论 %d 条\n\n"
                                    "打开输出目录即可查看「相册列表.html」。"
                                    % (stats.get("albums", 0), stats.get("images", 0),
                                       stats.get("ok", 0), stats.get("failed", 0),
                                       stats.get("comments", 0)))
        self.stage_var.set("完成")

    # ------------------------------------------------------------------
    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.btn_login, self.btn_start):
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def _run_thread(self, target):
        if self.worker and self.worker.is_alive():
            self._log("已有任务在运行，请先等待完成或点「停止」。")
            return
        self.stop_event = threading.Event()
        self._set_busy(True)
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _do_login(self):
        def work():
            try:
                ok = crawler.login_flow(profile_dir=default_profile(),
                                        log=lambda m: self._log(m))
                self._log("登录成功，会话已保存。" if ok else "未检测到登录态（图片接口本身不需要登录，可直接使用）。")
            except Exception as exc:                        # noqa: BLE001
                self._log("登录出错：%s" % exc)
            finally:
                self.after(0, lambda: self._set_busy(False))

        self._run_thread(work)

    # ------------------------------------------------------------------
    def _do_start(self):
        link = self.link_var.get().strip()
        kw = extract_kw(link) or link
        if not kw:
            messagebox.showwarning(APP_TITLE, "请填入贴吧相册链接，或直接填贴吧名。")
            return

        out = self.out_var.get().strip() or self.last_out_dir
        if not out:
            # 每次运行都让用户显式选择保存位置，不静默写死
            out = filedialog.askdirectory(initialdir=default_out_dir_suggestion(),
                                          title="选择保存位置（图片将保存在这里）")
            if not out:
                self._log("已取消：未选择输出目录。")
                return
        self.out_var.set(out)
        self.last_out_dir = out

        limit_raw = self.limit_var.get().strip()
        max_comments = None
        if limit_raw:
            try:
                max_comments = max(1, int(limit_raw))
            except ValueError:
                messagebox.showwarning(APP_TITLE, "「每图最多几条」请填整数，或留空表示不限。")
                return
        if not self.comments_var.get():
            max_comments = 0

        try:
            workers = max(1, min(16, int(self.workers_var.get().strip() or 4)))
        except ValueError:
            workers = 4

        fetch_comments = bool(self.comments_var.get()) and max_comments != 0
        original = bool(self.original_var.get())
        opts = CrawlOptions(out_dir=out, fetch_comments=fetch_comments,
                            max_comments=max_comments or None, workers=workers,
                            profile_dir=default_profile(), original=original)

        self._log("=" * 60)
        self._log("开始：贴吧「%s」→ %s" % (kw, out))
        self._log("选项：抓取评论=%s，每图最多 %s 条，并发 %d，还原模式=%s"
                  % ("是" if fetch_comments else "否",
                     max_comments or "不限", workers,
                     "原版贴吧视觉" if original else "旧网格"))
        stop_event = self.stop_event

        def work():
            stats = None
            try:
                man = crawl_flow(
                    kw, out, opts,
                    log=lambda m: self._log(m),
                    progress=lambda s, c, t, m="": self.q.put(("prog", s, c, t, m)),
                    stop_event=stop_event)
                stats = summarize(man)
            except Exception as exc:                        # noqa: BLE001
                self._log("出错：%s" % exc)
            finally:
                self.q.put(("done", stats))

        self._run_thread(work)

    def _do_stop(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self._log("正在停止……（已下载的图片会保留，下次运行可续传）")
            self.stage_var.set("正在停止…")
        else:
            self._log("当前没有正在运行的任务。")


def main():
    App().mainloop()


if __name__ == "__main__":
    import sys as _sys
    # 若带命令行参数（--selftest / --kw / --login 等），走 crawler 的命令行入口；
    # 否则启动图形界面。这样同一个 exe 既能双击用 GUI，也能命令行调试。
    # 注意：crawler.main() 会跳过 argv[0]（当作脚本名），所以必须传完整 sys.argv。
    try:
        if len(_sys.argv) > 1 and any(a.startswith("-") for a in _sys.argv[1:]):
            _sys.exit(crawler.main(_sys.argv))
        main()
    except Exception:
        if getattr(_sys, "frozen", False):
            _write_crash_log(_sys.exc_info())
        raise
