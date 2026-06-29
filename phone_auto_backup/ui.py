from __future__ import annotations

import queue
import re
import tkinter as tk
from tkinter import ttk


COPYING_PATTERN = re.compile(r"^(?:Copying|Sending|Converting) (?P<index>\d+)/(?P<total>\d+): (?P<file>.+)$")
FOUND_PATTERN = re.compile(r"^Found (?P<scanned>\d+) (?:PC )?files, (?P<new>\d+) new\.$")
PROGRESS_PATTERN = re.compile(r"Progress: (?P<percent>\d+(?:\.\d+)?)%")


class BackupWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Phone Auto Backup")
        self.root.geometry("680x360")
        self.root.minsize(560, 320)
        self.root.configure(bg="#f6f7f9")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        self._events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._build()
        self.hide()
        self.root.after(100, self._drain_events)

    def run(self) -> None:
        self.root.mainloop()

    def stop(self) -> None:
        self._events.put(("stop", None))

    def hide(self) -> None:
        self.root.withdraw()

    def show(self) -> None:
        self._events.put(("show", None))

    def update_status(self, message: str) -> None:
        self._events.put(("status", message))

    def _build(self) -> None:
        self.container = ttk.Frame(self.root, padding=24)
        self.container.pack(fill=tk.BOTH, expand=True)

        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 10))
        style.configure("File.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Stats.TLabel", font=("Segoe UI", 10))

        ttk.Label(self.container, text="Phone Auto Backup", style="Title.TLabel").pack(
            anchor=tk.W
        )

        self.status_label = ttk.Label(
            self.container,
            text="Waiting for phone...",
            style="Status.TLabel",
            foreground="#4b5563",
        )
        self.status_label.pack(anchor=tk.W, pady=(8, 18))

        ttk.Label(self.container, text="Current file", foreground="#6b7280").pack(anchor=tk.W)
        self.file_label = ttk.Label(
            self.container,
            text="Nothing transferring yet",
            style="File.TLabel",
            wraplength=620,
        )
        self.file_label.pack(anchor=tk.W, fill=tk.X, pady=(4, 16))

        self.progress = ttk.Progressbar(self.container, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X)

        self.count_label = ttk.Label(
            self.container,
            text="",
            style="Stats.TLabel",
            foreground="#4b5563",
        )
        self.count_label.pack(anchor=tk.W, pady=(8, 18))

        ttk.Label(self.container, text="Stats", foreground="#6b7280").pack(anchor=tk.W)
        self.stats_label = ttk.Label(
            self.container,
            text="Speed, remaining data, and ETA will appear once copying starts.",
            style="Stats.TLabel",
            foreground="#111827",
            wraplength=620,
        )
        self.stats_label.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))

    def _drain_events(self) -> None:
        while True:
            try:
                event, payload = self._events.get_nowait()
            except queue.Empty:
                break

            if event == "show":
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            elif event == "status" and payload is not None:
                self._apply_status(payload)
            elif event == "stop":
                self.root.destroy()
                return

        self.root.after(100, self._drain_events)

    def _apply_status(self, message: str) -> None:
        if "Progress:" in message and "Remaining:" in message:
            progress = PROGRESS_PATTERN.search(message)
            if progress:
                self.progress.configure(value=float(progress.group("percent")))
            self.stats_label.configure(text=message)
            return

        self.status_label.configure(text=message)

        if message in ("Indexing phone media...", "Indexing PC media...", "Indexing camera uploads..."):
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
            self.file_label.configure(text=message)
            if message == "Indexing phone media...":
                self.stats_label.configure(text="Scanning Android's media index.")
            elif message == "Indexing PC media...":
                self.stats_label.configure(text="Scanning PC source folder.")
            else:
                self.stats_label.configure(text="Scanning camera upload folders.")
            return

        found = FOUND_PATTERN.match(message)
        if found:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.count_label.configure(
                text=f"{found.group('new')} new files out of {found.group('scanned')} scanned"
            )
            self.stats_label.configure(text="Preparing transfer order.")
            return

        copying = COPYING_PATTERN.match(message)
        if copying:
            index = int(copying.group("index"))
            total = max(int(copying.group("total")), 1)
            self.progress.stop()
            self.progress.configure(mode="determinate", value=(index - 1) / total * 100)
            self.file_label.configure(text=copying.group("file"))
            self.count_label.configure(text=f"File {index} of {total}")
            return

        if message.startswith("Backup complete.") or message.startswith("Phone transfer complete.") or message.startswith("Camera backup complete."):
            self.progress.stop()
            self.progress.configure(mode="determinate", value=100)
            self.file_label.configure(text="Complete")
            self.stats_label.configure(text=message)
