#!/usr/bin/env python3
"""
live_ocr.py - cross-platform screen OCR with a desktop UI.

Captures the screen on an interval, runs OCR, and displays any text that
changed in a scrolling window.

Install:
    pip install mss numpy rapidocr-onnxruntime
"""

import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

import numpy as np


# --------------------------------------------------------------------------
# Image preprocessing
# --------------------------------------------------------------------------
# OCR models are trained mostly on photographed or scanned documents: dark
# text, light background, reasonably large glyphs. Screen text breaks all
# three assumptions, so a little preparation buys a lot of accuracy.

MAX_PIXELS_AFTER_SCALE = 8_000_000  # keep upscaled frames to a sane size


def _resize(img, factor):
    try:
        import cv2  # usually present as a RapidOCR dependency

        return cv2.resize(
            img, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC
        )
    except ImportError:
        # Nearest-neighbour fallback. Blockier, but it keeps glyph edges hard,
        # which OCR tolerates better than a blur.
        return np.repeat(np.repeat(img, factor, axis=0), factor, axis=1)


def preprocess(frame, scale=2):
    """RGB frame in, cleaned-up RGB frame out."""
    gray = (
        frame[:, :, 0] * 0.299 + frame[:, :, 1] * 0.587 + frame[:, :, 2] * 0.114
    )

    # Contrast stretch on percentiles rather than min/max, so one stray white
    # pixel doesn't flatten everything else.
    lo, hi = np.percentile(gray, (2, 98))
    if hi - lo > 1:
        gray = np.clip((gray - lo) * (255.0 / (hi - lo)), 0, 255)

    # Dark-mode UIs and terminals are light-on-dark, which is the inverse of
    # what the models expect. Flip them.
    if gray.mean() < 110:
        gray = 255.0 - gray

    img = gray.astype(np.uint8)

    # Small UI text often sits below the resolution the model handles well.
    if scale > 1:
        if img.size * scale * scale > MAX_PIXELS_AFTER_SCALE:
            scale = max(1, int((MAX_PIXELS_AFTER_SCALE / img.size) ** 0.5))
        if scale > 1:
            img = _resize(img, scale)

    return np.stack([img] * 3, axis=-1)


# --------------------------------------------------------------------------
# OCR backend
# --------------------------------------------------------------------------


class OCREngine:
    """Wraps whichever OCR library is installed behind one read() call."""

    def __init__(self, min_confidence=0.5):
        self.min_confidence = min_confidence
        self.backend = None
        self._engine = None

    def load(self):
        """Slow. Call this off the UI thread."""
        try:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
            self.backend = "RapidOCR"
            return
        except ImportError:
            pass

        try:
            import pytesseract

            self._engine = pytesseract
            self.backend = "Tesseract"
            return
        except ImportError:
            pass

        raise RuntimeError(
            "No OCR backend found.\n\n"
            "Install one:\n"
            "  pip install rapidocr-onnxruntime   (recommended)\n"
            "  pip install pytesseract            (also needs the tesseract binary)"
        )

    def read(self, img):
        if self.backend == "RapidOCR":
            return self._read_rapidocr(img)
        return self._read_tesseract(img)

    def _read_rapidocr(self, img):
        raw = self._engine(img)

        # RapidOCR's return shape has changed across versions, so normalise
        # rather than assuming one layout.
        if raw is None:
            return ""
        if isinstance(raw, tuple):
            raw = raw[0]
        if raw is None:
            return ""

        if hasattr(raw, "txts"):  # newer object-style result
            texts = raw.txts or []
            scores = getattr(raw, "scores", None) or [1.0] * len(texts)
            return "\n".join(
                t for t, s in zip(texts, scores) if s >= self.min_confidence
            )

        lines = []
        for item in raw:  # classic [box, text, confidence] rows
            try:
                text, conf = item[1], float(item[2])
            except (IndexError, TypeError, ValueError):
                continue
            if conf >= self.min_confidence:
                lines.append(text)
        return "\n".join(lines)

    def _read_tesseract(self, img):
        from PIL import Image

        return self._engine.image_to_string(Image.fromarray(img)).strip()


# --------------------------------------------------------------------------
# Capture worker
# --------------------------------------------------------------------------


class ScreenReader(threading.Thread):
    """
    Captures and OCRs in the background. Never touches a widget - results go
    onto a queue that the UI thread drains, because tkinter is not thread-safe.
    """

    SEEN_HISTORY = 400  # lines remembered for dedupe

    def __init__(self, ocr, out_queue):
        super().__init__(daemon=True)
        self.ocr = ocr
        self.out = out_queue

        self.running = threading.Event()  # capturing vs paused
        self.alive = threading.Event()    # thread should keep existing
        self.alive.set()

        self.interval = 1.0
        self.monitor_index = 1            # mss: 0 = all screens, 1 = primary
        self.region = None                # dict, or None for full monitor
        self.enhance = True
        self.new_lines_only = True

        self._last_frame_hash = None
        self._last_text = None
        self._seen = deque(maxlen=self.SEEN_HISTORY)
        self._seen_set = set()

    def run(self):
        import mss

        # mss instances are not safe to share across threads, so this is
        # created here in the worker rather than in __init__.
        with mss.mss() as sct:
            while self.alive.is_set():
                if not self.running.wait(timeout=0.2):
                    continue

                try:
                    area = self.region or sct.monitors[self.monitor_index]
                    shot = sct.grab(area)
                except Exception as e:
                    self.out.put(("error", f"Capture failed: {e}"))
                    self.running.clear()
                    continue

                frame = np.array(shot)[:, :, :3][:, :, ::-1]  # BGRA -> RGB

                if self._unchanged(frame):
                    time.sleep(self.interval)
                    continue

                try:
                    prepared = preprocess(frame) if self.enhance else frame
                    text = self.ocr.read(prepared)
                except Exception as e:
                    self.out.put(("error", f"OCR failed: {e}"))
                    time.sleep(self.interval)
                    continue

                self._emit(text)
                time.sleep(self.interval)

    def _emit(self, text):
        if not text:
            return

        if self.new_lines_only:
            fresh = self._new_lines(text)
            if fresh:
                self.out.put(("text", "\n".join(fresh)))
        elif text != self._last_text:
            self._last_text = text
            self.out.put(("text", text))

    def _new_lines(self, text):
        """
        Return only lines not seen recently. Without this, one changed line in
        a scrolling log reprints the whole screen every interval.
        """
        fresh = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line in self._seen_set:
                continue
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])  # about to be evicted
            self._seen.append(line)
            self._seen_set.add(line)
            fresh.append(line)
        return fresh

    def _unchanged(self, frame):
        """Cheap gate so identical frames never reach the OCR engine."""
        small = frame[::8, ::8]
        h = hash(small.tobytes())
        if h == self._last_frame_hash:
            return True
        self._last_frame_hash = h
        return False

    def reset(self):
        self._last_frame_hash = None
        self._last_text = None
        self._seen.clear()
        self._seen_set.clear()


# --------------------------------------------------------------------------
# Region selector
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


class App:
    def __init__(self, root):
        self.root = root
        root.title("live-ocr")
        root.geometry("720x540")
        root.minsize(520, 340)

        self.queue = queue.Queue()
        self.ocr = OCREngine()
        self.reader = ScreenReader(self.ocr, self.queue)

        self.autoscroll = tk.BooleanVar(value=True)
        self.on_top = tk.BooleanVar(value=False)
        self.enhance = tk.BooleanVar(value=True)
        self.new_only = tk.BooleanVar(value=True)

        self._build_widgets()
        self._load_engine_async()
        self.root.after(100, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- layout -----------------------------------------------------------

    def _build_widgets(self):
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar.pack(fill="x")

        self.toggle_btn = ttk.Button(
            bar, text="Start", width=10, command=self._toggle, state="disabled"
        )
        self.toggle_btn.pack(side="left")

        ttk.Button(bar, text="Select region", command=self._pick_region).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(bar, text="Full screen", command=self._clear_region).pack(
            side="left", padx=(6, 0)
        )

        ttk.Label(bar, text="Interval").pack(side="left", padx=(16, 4))
        self.interval_box = ttk.Spinbox(
            bar, from_=0.2, to=10.0, increment=0.2, width=5,
            command=self._set_interval,
        )
        self.interval_box.set("1.0")
        self.interval_box.bind("<Return>", lambda e: self._set_interval())
        self.interval_box.pack(side="left")

        ttk.Button(bar, text="Clear", command=self._clear_text).pack(side="right")
        ttk.Button(bar, text="Copy all", command=self._copy_all).pack(
            side="right", padx=(0, 6)
        )

        opts = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        opts.pack(fill="x")
        ttk.Checkbutton(opts, text="Auto-scroll", variable=self.autoscroll).pack(
            side="left"
        )
        ttk.Checkbutton(
            opts, text="Always on top", variable=self.on_top, command=self._set_on_top
        ).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(
            opts, text="Enhance image", variable=self.enhance,
            command=self._set_flags,
        ).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(
            opts, text="New lines only", variable=self.new_only,
            command=self._set_flags,
        ).pack(side="left", padx=(12, 0))

        wrap = ttk.Frame(self.root, padding=(8, 0, 8, 0))
        wrap.pack(fill="both", expand=True)

        self.text = tk.Text(
            wrap, wrap="word", font=("TkFixedFont", 11),
            bg="#1e1e1e", fg="#e8e8e8", insertbackground="#e8e8e8",
            relief="flat", padx=10, pady=8, state="disabled",
        )
        scroll = ttk.Scrollbar(wrap, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.text.tag_configure("stamp", foreground="#6f9dd6", spacing1=8)
        self.text.tag_configure("error", foreground="#e06c75")

        self.status = ttk.Label(
            self.root, text="Loading OCR engine...", anchor="w",
            padding=(10, 4), relief="sunken",
        )
        self.status.pack(fill="x", side="bottom")

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

    # -- queue drain ------------------------------------------------------

    def _drain_queue(self):
        """Runs on the UI thread. The only place widgets get written to."""
        try:
            while True:
                kind, payload = self.queue.get_nowait()

                if kind == "text":
                    self._append(payload)
                elif kind == "ready":
                    self.toggle_btn.config(state="normal")
                    self.reader.start()
                    self._status(f"Ready - {payload}")
                elif kind == "error":
                    self._append(payload, error=True)
                    self._status(payload)
                    self.toggle_btn.config(text="Start")
                elif kind == "fatal":
                    self._append(payload, error=True)
                    self._status("No OCR backend installed")
        except queue.Empty:
            pass

        self.root.after(100, self._drain_queue)

    # -- text pane --------------------------------------------------------

    def _append(self, body, error=False):
        at_bottom = self.text.yview()[1] > 0.99

        self.text.config(state="normal")
        self.text.insert("end", f"\n{time.strftime('%H:%M:%S')}\n", "stamp")
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
        else:
            self.reader.reset()
            self.reader.running.set()
            self.toggle_btn.config(text="Stop")
            scope = "region" if self.reader.region else "full screen"
            self._status(f"Capturing {scope} every {self.reader.interval}s")

    def _set_interval(self):
        try:
            self.reader.interval = max(0.2, float(self.interval_box.get()))
        except ValueError:
            self.interval_box.set(str(self.reader.interval))

    def _set_flags(self):
        self.reader.enhance = self.enhance.get()
        self.reader.new_lines_only = self.new_only.get()
        self.reader.reset()

    def _pick_region(self):
        was_running = self.reader.running.is_set()
        self.reader.running.clear()
        self.root.withdraw()  # keep our own window out of the capture
        self.root.update()
        time.sleep(0.2)

        region = RegionSelector(self.root).select()

        self.root.deiconify()
        if region:
            self.reader.region = region
            self.reader.reset()
            self._status(
                f"Region set: {region['width']}x{region['height']} "
                f"at {region['left']},{region['top']}"
            )
        if was_running:
            self.reader.running.set()

    def _clear_region(self):
        self.reader.region = None
        self.reader.reset()
        self._status("Capturing full screen")

    def _set_on_top(self):
        self.root.attributes("-topmost", self.on_top.get())

    def _status(self, msg):
        self.status.config(text=msg)

    def _on_close(self):
        self.reader.running.clear()
        self.reader.alive.clear()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
