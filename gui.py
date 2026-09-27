"""
Window for the live call translator: live subtitles, mic mute button (Ctrl+Alt+M), always-on-top.

  pyw -3 gui.py [same options as live_translator.py]
"""
import asyncio
import ctypes
import queue
import threading
import time
import tkinter as tk
from tkinter import simpledialog

import live_translator as lt

BG, PANEL, FG, MUTED_FG = "#16181d", "#20232a", "#e6e6e6", "#8b93a1"
ON_BG, OFF_BG, OK_FG, BAD_FG = "#1f7a3a", "#b3261e", "#5fd38d", "#ffb74d"
STYLES = {  # caption kind -> (color, font)
    "me_src": (MUTED_FG, ("Segoe UI", 11)),
    "me_dst": ("#62c7f5", ("Segoe UI", 12)),
    "them_src": (MUTED_FG, ("Segoe UI", 11)),
    "them_dst": ("#ffd54f", ("Segoe UI", 15, "bold")),
}


class GuiSink(lt.Sink):
    """Engine thread -> Tk thread: every report becomes a queue message."""

    def __init__(self, q):
        self.q = q

    def caption(self, kind, label, text):
        self.q.put(("caption", kind, label, text))

    def note(self, text):
        self.q.put(("note", text))

    def status(self, label, text, ok):
        self.q.put(("status", label, text, ok))

    def lag(self, seconds):
        self.q.put(("lag", seconds))


class CaptionView:
    """Streams transcript deltas into a Text widget; each label keeps one open line until its phrase ends."""

    IDLE = 0.8

    def __init__(self, text):
        self.text = text
        self.open = {}  # label -> [mark, phrase, last_update]
        self.count = 0
        for kind, (color, font) in STYLES.items():
            text.tag_configure(kind, foreground=color, font=font, spacing1=4)
            text.tag_configure(f"lbl_{kind}", foreground=color, font=("Segoe UI", 9, "bold"), spacing1=4)
        text.tag_configure("note", foreground="#667085", font=("Segoe UI", 9, "italic"))
        text.tag_configure("error", foreground="#ff6b6b", font=("Segoe UI", 11, "bold"))

    def _write(self, fn):
        at_bottom = self.text.yview()[1] >= 0.999
        fn()
        if at_bottom:
            self.text.see("end")

    def put(self, kind, label, delta):
        def write():
            entry = self.open.get(label)
            if entry is None:
                self.count += 1
                mark = f"open{self.count}"
                self.text.insert("end", f"{label}   ", (f"lbl_{kind}",))
                self.text.insert("end", "\n", (kind,))
                self.text.mark_set(mark, "end-2c")  # just before this line's newline
                self.text.mark_gravity(mark, "right")
                entry = self.open[label] = [mark, "", 0.0]
            self.text.insert(entry[0], delta, (kind,))
            entry[1] += delta
            entry[2] = time.monotonic()
            if entry[1].rstrip().endswith((".", "?", "!", "…")):
                self.close(label)
        self._write(write)

    def close(self, label):
        self.text.mark_unset(self.open.pop(label)[0])

    def note(self, text, tag="note"):
        self._write(lambda: self.text.insert("end", text + "\n", (tag,)))

    def tick(self):
        now = time.monotonic()
        for label in [k for k, e in self.open.items() if now - e[2] > self.IDLE]:
            self.close(label)


class App:
    def __init__(self, root, args):
        self.root, self.args = root, args
        self.q = queue.Queue()
        self.engine = self.loop = self.task = self.thread = None
        self.muted = False
        self.statuses = {}

        root.title("Live Translator")
        root.geometry("640x460")
        root.minsize(560, 260)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)

        bar = tk.Frame(root, bg=PANEL, padx=8, pady=6)
        bar.pack(fill="x")
        self.mute_btn = tk.Button(bar, command=self.toggle_mute, font=("Segoe UI", 10, "bold"),
                                  fg="white", relief="flat", padx=12, pady=4, cursor="hand2")
        self.mute_btn.pack(side="left")
        for text, command in (("Ключ", self.ask_key), ("Перезапуск", self.restart)):
            tk.Button(bar, text=text, command=command, bg=BG, fg=FG, relief="flat", padx=8,
                      activebackground=PANEL, activeforeground=FG, font=("Segoe UI", 9),
                      cursor="hand2").pack(side="right", padx=(6, 0))

        row = tk.Frame(root, bg=BG, padx=10)
        row.pack(fill="x", pady=(4, 0))
        # packed first so the status text, not these, gets squeezed on a narrow window
        self.topmost = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text="Поверх окон", variable=self.topmost, command=self.apply_topmost,
                       bg=BG, fg=MUTED_FG, selectcolor=BG, activebackground=BG, activeforeground=FG,
                       font=("Segoe UI", 9)).pack(side="right")
        self.lag_lbl = tk.Label(row, text="", bg=BG, fg=MUTED_FG, font=("Segoe UI", 9))
        self.lag_lbl.pack(side="right", padx=(0, 8))
        self.status_lbl = tk.Label(row, text="запуск…", bg=BG, fg=MUTED_FG, anchor="w", font=("Segoe UI", 9))
        self.status_lbl.pack(side="left", fill="x", expand=True)

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True)
        text = tk.Text(body, bg=BG, fg=FG, wrap="word", bd=0, padx=12, pady=8,
                       highlightthickness=0, insertwidth=0, cursor="arrow")
        scroll = tk.Scrollbar(body, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.bind("<Key>", lambda e: None if (e.state & 0x4 and e.keysym.lower() == "c") else "break")
        self.captions = CaptionView(text)

        self.render_mute()
        self.apply_topmost()
        if lt.start_hotkey(lambda: self.q.put(("toggle_mute",))):
            self.captions.note(f"{lt.HOTKEY_NAME} — выключить/включить микрофон из любого окна.")
        else:
            self.captions.note(f"{lt.HOTKEY_NAME} занят другой программой — микрофон выключай кнопкой.")
        root.after(30, self.poll)
        root.after(100, self.start)

    # --- engine lifecycle -------------------------------------------------

    def start(self):
        if not lt.load_api_key() and not self.args.passthrough and not self.ask_key(restart=False):
            self.set_status("Нужен ключ OpenAI — нажми «Ключ»", False)
            return
        self.statuses.clear()
        self.set_status("подключение…", True)
        self.engine = lt.Engine(self.args, GuiSink(self.q))
        self.engine.set_muted(self.muted)
        self.loop = asyncio.new_event_loop()
        self.task = self.loop.create_task(self.engine.run())
        self.thread = threading.Thread(target=self._run_engine, args=(self.loop, self.task), daemon=True)
        self.thread.start()

    def _run_engine(self, loop, task):
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except lt.Fatal as e:
            self.q.put(("fatal", str(e)))
        except Exception as e:
            self.q.put(("fatal", f"{type(e).__name__}: {e}"))
        finally:
            loop.close()

    def stop(self):
        if self.thread and self.thread.is_alive():
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:  # loop already closed
                pass
            self.thread.join(timeout=3)
        self.engine = None

    def restart(self):
        self.stop()
        self.captions.note("— перезапуск —")
        self.start()

    def close(self):
        self.stop()
        self.root.destroy()

    # --- controls ---------------------------------------------------------

    def ask_key(self, restart=True):
        key = simpledialog.askstring(
            "Ключ OpenAI",
            "Вставь ключ API OpenAI (sk-...).\nОн сохранится в файл .env рядом с программой.",
            show="*", parent=self.root)
        if not key or not key.strip():
            return None
        lt.save_api_key(key.strip())
        if restart:
            self.restart()
        return key

    def toggle_mute(self):
        self.muted = not self.muted
        if self.engine:
            self.engine.set_muted(self.muted)
        self.render_mute()

    def render_mute(self):
        state = "ВЫКЛ" if self.muted else "ВКЛ"
        bg = OFF_BG if self.muted else ON_BG
        self.mute_btn.configure(text=f"●  Микрофон {state}   ({lt.HOTKEY_NAME})", bg=bg, activebackground=bg)

    def apply_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())

    def set_status(self, text, ok):
        self.status_lbl.configure(text=text, fg=OK_FG if ok else BAD_FG)

    # --- engine events ----------------------------------------------------

    def poll(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.captions.tick()
        self.root.after(30, self.poll)

    def handle(self, msg):
        kind = msg[0]
        if kind == "caption":
            self.captions.put(*msg[1:])
        elif kind == "note":
            self.captions.note(msg[1])
        elif kind == "status":
            _, label, text, ok = msg
            self.statuses[label] = (text, ok)
            self.set_status("   ".join(f"{k} ✓" if o else f"{k}: {t}" for k, (t, o) in self.statuses.items()),
                            all(o for _, o in self.statuses.values()))
        elif kind == "lag":
            self.lag_lbl.configure(text=f"задержка ≈ {msg[1]:.1f} с")
        elif kind == "fatal":
            self.set_status("остановлено — ошибка", False)
            self.captions.note(msg[1], "error")
            if "OPENAI_API_KEY" in msg[1]:
                self.root.after(200, self.ask_key)
        elif kind == "toggle_mute":
            self.toggle_mute()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except (AttributeError, OSError):
        pass
    args, _ = lt.build_parser().parse_known_args()
    root = tk.Tk()
    App(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
