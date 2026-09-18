#!/usr/bin/env python3
"""
live_ocr.py - cross-platform screen OCR with a desktop UI.

Define one or more capture regions, optionally anchored to application
windows, and watch the text in them stream into a scrolling log.

Install:
    pip install mss numpy rapidocr-onnxruntime pywinctl
"""

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import config
from capture import OCREngine, Region, ScreenReader
from sinks import EventBus, JsonlSink, WebhookSink, make_event
from window_track import WindowNotAvailable, WindowTracker

SCREEN_TARGET = "Whole screen"
SINK_KEYS = ["jsonl_enabled", "jsonl_path", "webhook_enabled", "webhook_url", "webhook_cooldown", "snapshot_events"]


# --------------------------------------------------------------------------
# Region selector overlay
# --------------------------------------------------------------------------


class RegionSelector:
    """Translucent fullscreen overlay. Drag a rectangle, get back coordinates."""

    def __init__(self, parent):
        self.parent = parent
        self.result = None

    def select(self):
        top = tk.Toplevel(self.parent)
        top.attributes("-fullscreen", True)
        try:
            top.attributes("-alpha", 0.25)
        except tk.TclError:
            pass
        top.attributes("-topmost", True)
        top.configure(bg="black")
        top.config(cursor="crosshair")

        canvas = tk.Canvas(top, bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_text(
            top.winfo_screenwidth() // 2,
            40,
            text="Drag to select a region  -  Esc to cancel",
            fill="white",
            font=("TkDefaultFont", 16),
        )

        state = {"x": 0, "y": 0, "cx": 0, "cy": 0, "rect": None}

        def on_press(e):
            state["x"], state["y"] = e.x_root, e.y_root
            state["cx"], state["cy"] = e.x, e.y
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(
                e.x, e.y, e.x, e.y, outline="#4da3ff", width=2
            )

        def on_drag(e):
            if state["rect"]:
                canvas.coords(state["rect"], state["cx"], state["cy"], e.x, e.y)

        def on_release(e):
            left, top_ = min(state["x"], e.x_root), min(state["y"], e.y_root)
            width, height = abs(e.x_root - state["x"]), abs(e.y_root - state["y"])
            if width > 10 and height > 10:
                self.result = {
                    "left": left,
                    "top": top_,
                    "width": width,
                    "height": height,
                }
            top.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", lambda e: top.destroy())

        top.focus_force()
        self.parent.wait_window(top)
        return self.result


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------


class OutputsDialog(tk.Toplevel):
    """
    Modal editor for the sink configuration.

    Edits a copy and only writes back on Save, so Cancel genuinely cancels.
    """

    def __init__(self, parent, cfg, default_path):
        super().__init__(parent)
        self.title("Outputs")
        self.resizable(False, False)
        self.result = None
        self._test_result = None

        self.file_on = tk.BooleanVar(value=cfg["jsonl_enabled"])
        self.path = tk.StringVar(value=cfg["jsonl_path"] or str(default_path))
        self.hook_on = tk.BooleanVar(value=cfg["webhook_enabled"])
        self.url = tk.StringVar(value=cfg["webhook_url"])
        self.cooldown = tk.StringVar(value=str(cfg["webhook_cooldown"]))
        self.snapshot = tk.BooleanVar(value=cfg["snapshot_events"])

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        f = ttk.LabelFrame(body, text="JSON Lines file", padding=8)
        f.pack(fill="x")
        ttk.Checkbutton(
            f, text="Append each captured line to a file", variable=self.file_on
        ).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(6, 0))
        ttk.Entry(row, textvariable=self.path, width=52).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse", command=self._browse).pack(side="left", padx=(6, 0))
        ttk.Label(
            f, foreground="#777", wraplength=440, justify="left",
            text="Durable and replayable: survives the consumer being offline. "
                 "Rotates at 5 MB, keeping one previous file.",
        ).pack(anchor="w", pady=(6, 0))

        w = ttk.LabelFrame(body, text="Webhook", padding=8)
        w.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(
            w, text="POST each captured line as JSON", variable=self.hook_on
        ).pack(anchor="w")
        row2 = ttk.Frame(w)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Entry(row2, textvariable=self.url, width=52).pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="Test", command=self._test).pack(side="left", padx=(6, 0))
        row3 = ttk.Frame(w)
        row3.pack(fill="x", pady=(6, 0))
        ttk.Label(row3, text="Minimum seconds between posts per region").pack(side="left")
        ttk.Spinbox(
            row3, from_=0, to=3600, increment=1, width=6, textvariable=self.cooldown
        ).pack(side="left", padx=(8, 0))

        ttk.Checkbutton(
            body, variable=self.snapshot,
            text="Send one snapshot of all regions per pass instead of single lines",
        ).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            body, foreground="#777", wraplength=440, justify="left",
            text="For consumers that need every region's value together. "
                 "Set the cooldown low (0-1 s) so snapshots are not throttled.",
        ).pack(anchor="w")

        self.note = ttk.Label(body, text="", wraplength=440, justify="left")
        self.note.pack(anchor="w", pady=(10, 0))

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Save", command=self._save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 6))

        self.transient(parent)
        self.grab_set()
        self.bind("<Escape>", lambda e: self.destroy())
        parent.wait_window(self)

    def _browse(self):
        chosen = filedialog.asksaveasfilename(
            parent=self, title="Capture file", defaultextension=".jsonl",
            initialfile=Path(self.path.get()).name or "captures.jsonl",
            filetypes=[("JSON Lines", "*.jsonl"), ("All files", "*.*")],
        )
        if chosen:
            self.path.set(chosen)

    def _test(self):
        url = self.url.get().strip()
        if not url:
            self.note.config(text="Enter a URL first.", foreground="#b05")
            return
        self.note.config(text="Sending test event...", foreground="")
        self._test_result = None

        def work():
            sample = make_event("test", "Test region", "live-ocr test event")
            try:
                WebhookSink(url, cooldown=0, timeout=5).deliver(sample)
                self._test_result = (f"Delivered to {url}", "#2a7")
            except Exception as e:
                self._test_result = (f"Failed: {e}", "#b05")

        threading.Thread(target=work, daemon=True).start()
        self._poll_test()

    def _poll_test(self):
        """
        Poll from the UI thread. after() cannot be called from the worker -
        it registers a Tcl command, which is only safe on the main thread.
        """
        if not self.winfo_exists():
            return
        if self._test_result is None:
            self.after(120, self._poll_test)
            return
        msg, colour = self._test_result
        self.note.config(text=msg, foreground=colour)

    def _save(self):
        try:
            cooldown = max(0.0, float(self.cooldown.get()))
        except ValueError:
            self.note.config(text="Cooldown must be a number.", foreground="#b05")
            return

        path = self.path.get().strip()
        if self.file_on.get() and not path:
            self.note.config(text="Choose a file path.", foreground="#b05")
            return
        url = self.url.get().strip()
        if self.hook_on.get() and not url.lower().startswith(("http://", "https://")):
            self.note.config(text="URL must start with http:// or https://", foreground="#b05")
            return

        self.result = {
            "jsonl_enabled": self.file_on.get(),
            "jsonl_path": path,
            "webhook_enabled": self.hook_on.get(),
            "webhook_url": url,
            "webhook_cooldown": cooldown,
            "snapshot_events": self.snapshot.get(),
        }
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        root.title("live-ocr")

        self.queue = queue.Queue()
        self.ocr = OCREngine()
        self.reader = ScreenReader(self.ocr, self.queue)

        self.profile = config.get_active()
        saved_regions, settings = config.load(self.profile)

        self.autoscroll = tk.BooleanVar(value=True)
        self.on_top = tk.BooleanVar(value=settings["always_on_top"])
        self.enhance = tk.BooleanVar(value=settings["enhance"])
        self.new_only = tk.BooleanVar(value=settings["new_lines_only"])
        self.scale_with_window = tk.BooleanVar(value=settings["scale_with_window"])
        self.preview_processed = tk.BooleanVar(value=settings["preview_processed"])

        self.reader.regions = saved_regions
        self.reader.interval = settings["interval"]
        self.reader.enhance = settings["enhance"]
        self.reader.new_lines_only = settings["new_lines_only"]
        self.reader.preview_processed = settings["preview_processed"]

        self.sink_cfg = {k: settings[k] for k in SINK_KEYS}
        self.bus = EventBus(on_error=self._sink_error, on_reply=self._sink_reply)
        self.bus.start()
        self.reader.bus = self.bus
        self.reader.profile = self.profile

        self._preview_img = None   # keep a reference or Tk drops the image
        self._after_id = None
        self._counter = len(saved_regions)

        self._build_widgets()
        self._restore_geometry()
        self.interval_box.set(str(settings["interval"]))
        self._set_on_top()
        self._redraw_list(keep=0 if saved_regions else None)
        self._refresh_profiles()
        self._refresh_windows()
        self._apply_sinks()
        n = len(saved_regions)
        self._status(
            f"Profile {self.profile!r} - {n} region{'s' if n != 1 else ''}"
        )
        self._load_engine_async()
        self._after_id = self.root.after(100, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F9>", lambda e: self._save_shot())

    # -- layout -----------------------------------------------------------

    def _build_widgets(self):
        self._build_profile_bar()
        self._build_toolbar()

        panes = ttk.PanedWindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=(4, 0))

        left = ttk.Frame(panes, padding=(0, 0, 6, 0))
        panes.add(left, weight=0)
        self._build_region_panel(left)

        right = ttk.Frame(panes)
        panes.add(right, weight=1)
        self._build_output(right)

        bar = ttk.Frame(self.root, relief="sunken")
        bar.pack(fill="x", side="bottom")
        self.pass_info = ttk.Label(bar, text="", anchor="e", padding=(10, 4))
        self.pass_info.pack(side="right")
        self.status = ttk.Label(bar, text="Loading OCR engine...", anchor="w", padding=(10, 4))
        self.status.pack(side="left", fill="x", expand=True)

    def _build_profile_bar(self):
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        bar.pack(fill="x")

        ttk.Label(bar, text="Profile").pack(side="left", padx=(0, 4))
        self.profile_box = ttk.Combobox(bar, state="readonly", width=26)
        self.profile_box.pack(side="left")
        self.profile_box.bind("<<ComboboxSelected>>", self._on_switch_profile)

        ttk.Button(bar, text="New", width=5, command=self._new_profile).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(bar, text="Duplicate", command=self._duplicate_profile).pack(
            side="left", padx=(3, 0)
        )
        ttk.Button(bar, text="Rename", command=self._rename_profile).pack(
            side="left", padx=(3, 0)
        )
        ttk.Button(bar, text="Delete", command=self._delete_profile).pack(
            side="left", padx=(3, 0)
        )
        ttk.Button(bar, text="Open folder", command=self._open_folder).pack(
            side="right"
        )

        ttk.Separator(self.root, orient="horizontal").pack(fill="x", pady=(8, 0))

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        bar.pack(fill="x")

        self.toggle_btn = ttk.Button(
            bar, text="Start", width=9, command=self._toggle, state="disabled"
        )
        self.toggle_btn.pack(side="left")

        ttk.Label(bar, text="Interval").pack(side="left", padx=(14, 4))
        self.interval_box = ttk.Spinbox(
            bar, from_=0.2, to=10.0, increment=0.2, width=5,
            command=self._set_interval,
        )
        self.interval_box.set("1.0")
        self.interval_box.bind("<Return>", lambda e: self._set_interval())
        self.interval_box.pack(side="left")

        ttk.Checkbutton(
            bar, text="Enhance image", variable=self.enhance, command=self._set_flags
        ).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(
            bar, text="New lines only", variable=self.new_only, command=self._set_flags
        ).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(
            bar, text="Always on top", variable=self.on_top, command=self._set_on_top
        ).pack(side="left", padx=(10, 0))

        ttk.Button(bar, text="Save shot", command=self._save_shot).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Clear log", command=self._clear_text).pack(side="right")
        ttk.Button(bar, text="Outputs...", command=self._edit_outputs).pack(
            side="right", padx=(0, 12)
        )
        ttk.Button(bar, text="Copy all", command=self._copy_all).pack(
            side="right", padx=(0, 6)
        )
        ttk.Checkbutton(bar, text="Auto-scroll", variable=self.autoscroll).pack(
            side="right", padx=(0, 12)
        )

    def _build_region_panel(self, parent):
        ttk.Label(parent, text="Regions").pack(anchor="w")

        self.region_list = tk.Listbox(
            parent, width=30, height=6, exportselection=False,
            activestyle="none", bg="#252525", fg="#e8e8e8",
            selectbackground="#3a6ea5", highlightthickness=0, relief="flat",
        )
        self.region_list.pack(fill="both", expand=True, pady=(2, 4))
        self.region_list.bind("<<ListboxSelect>>", self._on_select_region)
        self.region_list.bind("<Double-Button-1>", lambda e: self._rename_region())

        btns = ttk.Frame(parent)
        btns.pack(fill="x")
        ttk.Button(btns, text="Add", width=6, command=self._add_region).pack(side="left")
        ttk.Button(btns, text="Delete", width=7, command=self._delete_region).pack(
            side="left", padx=(3, 0)
        )
        ttk.Button(btns, text="Up", width=4, command=lambda: self._move(-1)).pack(
            side="left", padx=(3, 0)
        )
        ttk.Button(btns, text="Down", width=6, command=lambda: self._move(1)).pack(
            side="left", padx=(3, 0)
        )

        btns2 = ttk.Frame(parent)
        btns2.pack(fill="x", pady=(3, 0))
        ttk.Button(btns2, text="Rename", width=8, command=self._rename_region).pack(
            side="left"
        )
        ttk.Button(btns2, text="Reselect area", command=self._reselect_region).pack(
            side="left", padx=(3, 0)
        )
        ttk.Button(btns2, text="On/Off", width=7, command=self._toggle_enabled).pack(
            side="left", padx=(3, 0)
        )
        ttk.Button(btns2, text="Combine", width=8, command=self._toggle_combine).pack(
            side="left", padx=(3, 0)
        )
        ttk.Button(btns2, text="Image", width=7, command=self._toggle_send_image).pack(
            side="left", padx=(3, 0)
        )

        target = ttk.LabelFrame(parent, text="New region target", padding=6)
        target.pack(fill="x", pady=(8, 0))
        self.target_box = ttk.Combobox(target, state="readonly", width=26)
        self.target_box.pack(fill="x")
        row = ttk.Frame(target)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Refresh windows", command=self._refresh_windows).pack(
            side="left"
        )
        ttk.Checkbutton(
            target, text="Scale with window", variable=self.scale_with_window
        ).pack(anchor="w", pady=(4, 0))

        prev = ttk.LabelFrame(parent, text="Preview", padding=4)
        prev.pack(fill="x", pady=(8, 0))
        self.preview = tk.Canvas(
            prev, width=240, height=150, bg="#151515", highlightthickness=0
        )
        self.preview.pack()
        ttk.Checkbutton(
            prev, text="Show what OCR sees", variable=self.preview_processed,
            command=self._set_preview_mode,
        ).pack(anchor="w", pady=(4, 0))
        self.preview_info = ttk.Label(prev, text="No region selected", wraplength=240)
        self.preview_info.pack(anchor="w", pady=(2, 0))

    def _build_output(self, parent):
        self.text = tk.Text(
            parent, wrap="word", font=("TkFixedFont", 11),
            width=40, height=12,
            bg="#1e1e1e", fg="#e8e8e8", insertbackground="#e8e8e8",
            relief="flat", padx=10, pady=8, state="disabled",
        )
        scroll = ttk.Scrollbar(parent, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.text.tag_configure("stamp", foreground="#6f9dd6", spacing1=8)
        self.text.tag_configure("error", foreground="#e06c75")

    # -- engine -----------------------------------------------------------

    def _load_engine_async(self):
        """Model load takes ~10s on first run, so keep it off the UI thread."""

        def work():
            try:
                self.ocr.load()
                self.queue.put(("ready", self.ocr.backend))
            except Exception as e:
                self.queue.put(("fatal", str(e)))

        threading.Thread(target=work, daemon=True).start()

    # -- region list ------------------------------------------------------

    def _selected_index(self):
        sel = self.region_list.curselection()
        return sel[0] if sel else None

    def _selected_region(self):
        i = self._selected_index()
        return self.reader.regions[i] if i is not None else None

    def _redraw_list(self, keep=None):
        self.region_list.delete(0, "end")
        for r in self.reader.regions:
            mark = ("" if r.enabled else "  (off)")
            mark += "  (image)" if r.send_image else ("" if r.combine else "  (alone)")
            self.region_list.insert("end", f"{r.name}{mark}")
        if keep is not None and 0 <= keep < len(self.reader.regions):
            self.region_list.selection_set(keep)
            self.region_list.activate(keep)
        self.reader.preview_region = self._selected_region()
        self._update_preview_info()

    def _add_region(self):
        target = self.target_box.get().strip() or SCREEN_TARGET
        self._counter += 1
        region = Region(f"Region {self._counter}")

        if target != SCREEN_TARGET:
            try:
                region.attach_window(target)
            except WindowNotAvailable as e:
                self._status(str(e))
                return

        area = self._drag_select()
        if area is None:
            self._status("Cancelled")
            return

        try:
            region.set_area(area, self.scale_with_window.get())
        except WindowNotAvailable as e:
            self._status(f"Could not anchor region - {e}")
            return

        self.reader.regions.append(region)
        self._redraw_list(keep=len(self.reader.regions) - 1)
        self._save()
        self._status(f"Added {region.name}: {region.describe()}")
        self._request_preview()

    def _reselect_region(self):
        region = self._selected_region()
        if region is None:
            self._status("Select a region first")
            return
        area = self._drag_select()
        if area is None:
            return
        try:
            region.set_area(area, self.scale_with_window.get())
        except WindowNotAvailable as e:
            self._status(f"Could not anchor region - {e}")
            return
        self._save()
        self._status(f"{region.name}: {region.describe()}")
        self._request_preview()

    def _drag_select(self):
        """Hide the app, let the user drag, restore. Returns a dict or None."""
        was_running = self.reader.running.is_set()
        self.reader.running.clear()
        self.root.withdraw()  # keep our own window out of the capture
        self.root.update()
        time.sleep(0.2)

        area = RegionSelector(self.root).select()

        self.root.deiconify()
        if was_running:
            self.reader.running.set()
        return area

    def _delete_region(self):
        i = self._selected_index()
        if i is None:
            self._status("Select a region first")
            return
        name = self.reader.regions.pop(i).name
        self._save()
        self._redraw_list(keep=min(i, len(self.reader.regions) - 1))
        self._status(f"Deleted {name}")

    def _move(self, delta):
        i = self._selected_index()
        if i is None:
            return
        j = i + delta
        if not (0 <= j < len(self.reader.regions)):
            return
        regions = self.reader.regions
        regions[i], regions[j] = regions[j], regions[i]
        self._redraw_list(keep=j)
        self._save()

    def _rename_region(self):
        region = self._selected_region()
        if region is None:
            return
        name = simpledialog.askstring(
            "Rename region", "Name:", initialvalue=region.name, parent=self.root
        )
        if name and name.strip():
            region.name = name.strip()
            self._redraw_list(keep=self._selected_index())
            self._save()

    def _restore_geometry(self):
        """
        Size from what the layout actually asks for rather than hardcoded
        pixels, so a different DPI, font scale, or platform theme still fits.

        Setting minsize to the required size is what stops controls being
        clipped: the window cannot be dragged smaller than its own contents.
        """
        self.root.update_idletasks()
        req_w = self.root.winfo_reqwidth()
        req_h = self.root.winfo_reqheight()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        self.root.minsize(min(req_w, screen_w - 40), min(req_h, screen_h - 80))

        saved = config.get_geometry()
        if saved:
            try:
                self.root.geometry(saved)
                return
            except tk.TclError:
                pass

        # A little headroom over the minimum, so the log pane opens usable.
        self.root.geometry(
            f"{min(req_w + 260, screen_w - 80)}x{min(req_h + 40, screen_h - 120)}"
        )

    # -- profiles ---------------------------------------------------------

    def _refresh_profiles(self):
        names = config.list_profiles()
        if self.profile not in names:
            names = sorted(set(names + [self.profile]), key=str.lower)
        self.profile_box["values"] = names
        self.profile_box.set(self.profile)
        self.root.title(f"live-ocr - {self.profile}")

    def _switch_to(self, name):
        """Save what we have, then load the other profile in its place."""
        self._save()
        was_running = self.reader.running.is_set()
        self.reader.running.clear()

        regions, settings = config.load(name)
        self.profile = name
        self.reader.regions = regions
        self.reader.interval = settings["interval"]
        self.reader.enhance = settings["enhance"]
        self.reader.new_lines_only = settings["new_lines_only"]
        self.reader.reset()

        self.enhance.set(settings["enhance"])
        self.new_only.set(settings["new_lines_only"])
        self.scale_with_window.set(settings["scale_with_window"])
        self.on_top.set(settings["always_on_top"])
        self.preview_processed.set(settings["preview_processed"])
        self.reader.preview_processed = settings["preview_processed"]
        self.interval_box.set(str(settings["interval"]))
        self._set_on_top()

        self.sink_cfg = {k: settings[k] for k in SINK_KEYS}
        self.reader.profile = name
        self._apply_sinks()

        self._counter = len(regions)
        self._redraw_list(keep=0 if regions else None)
        self._refresh_profiles()
        config.set_active(name)

        if was_running and regions:
            self.reader.running.set()
        else:
            self.toggle_btn.config(text="Start")

        n = len(regions)
        self._status(f"Profile {name!r} - {n} region{'s' if n != 1 else ''}")

    def _on_switch_profile(self, _event=None):
        name = self.profile_box.get()
        if name and name != self.profile:
            self._switch_to(name)

    def _ask_profile_name(self, title, initial=""):
        name = simpledialog.askstring(
            title, "Profile name:", initialvalue=initial, parent=self.root
        )
        if name is None:
            return None
        name = name.strip()
        if not name:
            self._status("Name cannot be empty")
            return None
        if config.exists(name) and name != self.profile:
            self._status(f"A profile named {name!r} already exists")
            return None
        return name

    def _new_profile(self):
        name = self._ask_profile_name("New profile")
        if name is None:
            return
        self._save()
        config.save(name, [], dict(config.DEFAULT_SETTINGS))
        self._switch_to(name)

    def _duplicate_profile(self):
        name = self._ask_profile_name("Duplicate profile", f"{self.profile} copy")
        if name is None:
            return
        config.save(name, self.reader.regions, self._settings())
        self._switch_to(name)

    def _rename_profile(self):
        name = self._ask_profile_name("Rename profile", self.profile)
        if name is None or name == self.profile:
            return
        self._save()
        if not config.rename(self.profile, name):
            self._status(f"Could not rename to {name!r}")
            return
        self.profile = name
        config.set_active(name)
        self._refresh_profiles()
        self._status(f"Renamed profile to {name!r}")

    def _delete_profile(self):
        names = config.list_profiles()
        if len(names) <= 1:
            self._status("Cannot delete the only profile")
            return
        if not messagebox.askyesno(
            "Delete profile",
            f"Delete profile {self.profile!r} and its regions?",
            parent=self.root,
        ):
            return
        gone = self.profile
        config.delete(gone)
        remaining = [n for n in config.list_profiles() if n != gone]
        self.profile = remaining[0]
        self._switch_to(self.profile)
        self._status(f"Deleted profile {gone!r}")

    def _sink_error(self, msg):
        """Called from the sink thread - hand to the UI through the queue."""
        self.queue.put(("status", msg))

    def _sink_reply(self, reply):
        """A consumer answered a webhook. Sink thread - go through the queue."""
        self.queue.put(("reply", reply))

    def _apply_sinks(self):
        sinks = []
        cfg = self.sink_cfg
        self.reader.snapshot_events = cfg["snapshot_events"]
        if cfg["jsonl_enabled"] and cfg["jsonl_path"]:
            sinks.append(JsonlSink(cfg["jsonl_path"]))
        if cfg["webhook_enabled"] and cfg["webhook_url"]:
            sinks.append(
                WebhookSink(cfg["webhook_url"], cooldown=cfg["webhook_cooldown"])
            )
        self.bus.set_sinks(sinks)
        return sinks

    def _edit_outputs(self):
        dlg = OutputsDialog(
            self.root, self.sink_cfg, config.default_capture_path(self.profile)
        )
        if dlg.result is None:
            return
        self.sink_cfg = dlg.result
        active = self._apply_sinks()
        self._save()
        self._status(
            "Outputs: " + (", ".join(s.describe() for s in active) or "none")
        )

    def _open_folder(self):
        self._save()  # so the current profile is actually on disk to look at
        if config.open_folder():
            self._status(f"Opened {config.CONFIG_DIR}")
        else:
            self._status(f"Could not open {config.CONFIG_DIR}")

    def _settings(self):
        return {
            "interval": self.reader.interval,
            "enhance": self.enhance.get(),
            "new_lines_only": self.new_only.get(),
            "scale_with_window": self.scale_with_window.get(),
            "always_on_top": self.on_top.get(),
            "preview_processed": self.preview_processed.get(),
            **self.sink_cfg,
        }

    def _toggle_enabled(self):
        region = self._selected_region()
        if region is None:
            self._status("Select a region first")
            return
        region.enabled = not region.enabled
        self._redraw_list(keep=self._selected_index())
        self._save()
        self._status(f"{region.name}: {'enabled' if region.enabled else 'disabled'}")

    def _toggle_combine(self):
        region = self._selected_region()
        if region is None:
            self._status("Select a region first")
            return
        region.combine = not region.combine
        self._redraw_list(keep=self._selected_index())
        self._save()
        how = "combined with other regions" if region.combine else "read on its own"
        self._status(f"{region.name}: {how}")

    def _toggle_send_image(self):
        region = self._selected_region()
        if region is None:
            self._status("Select a region first")
            return
        region.send_image = not region.send_image
        self._redraw_list(keep=self._selected_index())
        self._save()
        how = ("sent as an image, not read as text" if region.send_image
               else "read as text")
        self._status(f"{region.name}: {how}")

    def _save(self):
        """Persist the active profile. Silent on success."""
        if not config.save(self.profile, self.reader.regions, self._settings()):
            self._status(f"Could not write {config.path_for(self.profile)}")
        config.set_active(self.profile)

    def _on_select_region(self, _event=None):
        self._update_preview_info()
        self._request_preview()

    def _update_preview_info(self):
        region = self._selected_region()
        if region is None:
            self.preview_info.config(text="No region selected")
        else:
            self.preview_info.config(text=f"{region.name}\n{region.describe()}")

    def _set_preview_mode(self):
        self.reader.preview_processed = self.preview_processed.get()
        self._save()
        self._request_preview()  # redraw immediately rather than next cycle

    def _save_shot(self):
        """
        Save what live-ocr captures right now: the window a region is anchored
        to, plus every region's area, for checking alignment. Also on F9.
        """
        title = next((r.window_title for r in self.reader.regions
                      if r.mode == "window" and r.window_title), None)
        self.reader.shot_request = (title, str(config.CAPTURE_DIR / "shots"))
        self._status("Saving window shot...")

    def _request_preview(self):
        region = self._selected_region()
        self.reader.preview_region = region  # kept live while capturing
        if region is not None:
            self.reader.preview_request = region

    def _show_preview(self, name, b64png):
        region = self._selected_region()
        if region is None or region.name != name:
            return  # a different region's frame arrived; ignore it
        try:
            img = tk.PhotoImage(data=b64png)
        except tk.TclError:
            return
        self._preview_img = img  # hold a reference or Tk garbage-collects it
        self.preview.delete("all")
        cw = int(self.preview["width"])
        ch = int(self.preview["height"])
        self.preview.create_image(cw // 2, ch // 2, image=img)

    # -- queue drain ------------------------------------------------------

    def _publish_own_windows(self):
        """
        Tell the capture thread where live-ocr's windows are. Tk may only be
        queried from this thread, so the reader gets a fresh list instead.
        """
        rects = []
        for w in [self.root, *self.root.winfo_children()]:
            if not isinstance(w, (tk.Tk, tk.Toplevel)) or not w.winfo_viewable():
                continue
            if w.state() in ("iconic", "withdrawn"):  # some platforms stay "viewable"
                continue
            x, y = w.winfo_rootx(), w.winfo_rooty()  # client area
            fx, fy = w.winfo_x(), w.winfo_y()        # outer frame, incl. title bar
            border = max(0, x - fx)
            rects.append((min(x, fx), min(y, fy),
                          x + w.winfo_width() + border, y + w.winfo_height() + border))
        self.reader.own_windows = rects

    def _drain_queue(self):
        """Runs on the UI thread. The only place widgets get written to."""
        self._publish_own_windows()
        try:
            while True:
                kind, payload = self.queue.get_nowait()

                if kind == "text":
                    name, body = payload
                    self._append(body, source=name)
                elif kind == "preview":
                    self._show_preview(*payload)
                elif kind == "status":
                    self._status(payload)
                elif kind == "shot":
                    self._status(f"Saved {payload}")
                    self._append(payload, source="shot")
                elif kind == "pass":
                    secs, n = payload
                    self.pass_info.config(text=f"last pass {secs:.1f} s, {n} read")
                elif kind == "reply":
                    self._append(payload["message"], source=f"<- {payload.get('consumer', 'consumer')}")
                elif kind == "ready":
                    self.toggle_btn.config(state="normal")
                    self.reader.start()
                    self._status(f"Ready - {payload}")
                elif kind == "fatal":
                    self._append(payload, error=True)
                    self._status("No OCR backend installed")
        except queue.Empty:
            pass

        self._after_id = self.root.after(100, self._drain_queue)

    # -- text pane --------------------------------------------------------

    def _append(self, body, source=None, error=False):
        at_bottom = self.text.yview()[1] > 0.99
        label = f"{time.strftime('%H:%M:%S')}"
        if source:
            label += f"  {source}"

        self.text.config(state="normal")
        self.text.insert("end", f"\n{label}\n", "stamp")
        self.text.insert("end", body + "\n", "error" if error else "")
        self.text.config(state="disabled")

        if self.autoscroll.get() and at_bottom:
            self.text.see("end")

    def _clear_text(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")

    def _copy_all(self):
        content = self.text.get("1.0", "end").strip()
        if content:
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self._status("Copied to clipboard")

    # -- controls ---------------------------------------------------------

    def _toggle(self):
        if self.reader.running.is_set():
            self.reader.running.clear()
            self.toggle_btn.config(text="Start")
            self._status("Paused")
            return

        if not self.reader.regions:
            self._status("Add at least one region first")
            return

        self.reader.reset()
        self.reader.running.set()
        self.toggle_btn.config(text="Stop")
        n = len(self.reader.regions)
        self._status(
            f"Capturing {n} region{'s' if n != 1 else ''} every {self.reader.interval}s"
        )

    def _set_interval(self):
        try:
            self.reader.interval = max(0.2, float(self.interval_box.get()))
        except ValueError:
            self.interval_box.set(str(self.reader.interval))
            return
        self._save()

    def _set_flags(self):
        self.reader.enhance = self.enhance.get()
        self.reader.new_lines_only = self.new_only.get()
        self.reader.reset()
        self._save()

    def _refresh_windows(self):
        titles = [SCREEN_TARGET]
        if WindowTracker.available():
            titles += WindowTracker.list_titles()
        self.target_box["values"] = titles
        if not self.target_box.get():
            self.target_box.current(0)
        if len(titles) == 1:
            self._status("pywinctl not installed - screen regions only")

    def _set_on_top(self):
        self.root.attributes("-topmost", self.on_top.get())

    def _status(self, msg):
        self.status.config(text=msg)

    def _on_close(self):
        self.bus.stop()
        config.set_geometry(self.root.geometry())
        self._save()
        self.reader.running.clear()
        self.reader.alive.clear()
        if self._after_id is not None:
            # Cancel the pending drain, or Tk complains about an invalid
            # command once the widgets are gone.
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
