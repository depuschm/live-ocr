#!/usr/bin/env python3
"""
capture.py - regions, preprocessing, OCR, and the capture worker.

Kept separate from the UI so the capture logic stays testable without a
display attached.
"""

import base64
import struct
import zlib
import threading
import time
from collections import deque

import numpy as np

from sinks import make_event, make_snapshot
from window_track import RelativeRegion, WindowNotAvailable, WindowTracker


# --------------------------------------------------------------------------
# Image preprocessing
# --------------------------------------------------------------------------
# OCR models are trained mostly on photographed or scanned documents: dark
# text, light background, reasonably large glyphs. Screen text breaks all
# three assumptions, so a little preparation buys a lot of accuracy.

MAX_PIXELS_AFTER_SCALE = 8_000_000
PAD = 30  # white margin added after scaling


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
    gray = frame[:, :, 0] * 0.299 + frame[:, :, 1] * 0.587 + frame[:, :, 2] * 0.114

    # Percentiles rather than min/max, so one stray white pixel does not
    # flatten everything else.
    lo, hi = np.percentile(gray, (2, 98))
    if hi - lo > 1:
        gray = np.clip((gray - lo) * (255.0 / (hi - lo)), 0, 255)

    # Dark-mode UIs and terminals are light-on-dark, the inverse of what the
    # models expect.
    if gray.mean() < 110:
        gray = 255.0 - gray

    img = gray.astype(np.uint8)

    if scale > 1:
        if img.size * scale * scale > MAX_PIXELS_AFTER_SCALE:
            scale = max(1, int((MAX_PIXELS_AFTER_SCALE / img.size) ** 0.5))
        if scale > 1:
            img = _resize(img, scale)

    # Text detectors miss glyphs that touch the frame edge, which is the norm
    # for tight regions (a single character, a short number). A white margin
    # fixes it; after the inversion above the background is light, so white
    # blends in.
    img = np.pad(img, PAD, constant_values=255)

    return np.stack([img] * 3, axis=-1)


# --------------------------------------------------------------------------
# Thumbnails for the preview pane
# --------------------------------------------------------------------------


def thumbnail(frame, max_w=240, max_h=150):
    h, w = frame.shape[:2]
    step = max(1, int(np.ceil(max(w / max_w, h / max_h))))
    return np.ascontiguousarray(frame[::step, ::step])


def to_png_b64(rgb):
    """
    Encode an RGB array as a base64 PNG.

    Tk's PhotoImage accepts base64 PNG but rejects base64 PPM, and a ~20-line
    encoder here avoids pulling in Pillow just to draw a preview thumbnail.
    Costs about 0.3ms per thumbnail.
    """
    rgb = np.ascontiguousarray(rgb.astype(np.uint8))
    h, w = rgb.shape[:2]
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))  # filter byte per scanline

    def chunk(typ, data):
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode("ascii")


# --------------------------------------------------------------------------
# Regions
# --------------------------------------------------------------------------


class Region:
    """
    One capture area, with its own dedupe state.

    Each region tracks the lines it has already reported independently -
    sharing that state would mean the same value appearing in two regions got
    suppressed in whichever was read second.
    """

    SEEN_HISTORY = 400

    def __init__(self, name, mode="screen", window_title=None):
        self.name = name
        self.mode = mode                 # "screen" or "window"
        self.window_title = window_title
        self.enabled = True

        self.abs_region = None           # dict, used in screen mode
        self.rel = None                  # RelativeRegion, used in window mode
        self.tracker = None              # WindowTracker, used in window mode

        self._seen = deque(maxlen=self.SEEN_HISTORY)
        self._seen_set = set()
        self._last_hash = None
        self._last_text = None

    # -- geometry ---------------------------------------------------------

    def attach_window(self, title):
        tracker = WindowTracker()
        box = tracker.attach(title)       # raises WindowNotAvailable
        self.tracker = tracker
        self.mode = "window"
        self.window_title = title
        return box

    def set_area(self, region, scale_with_window=False):
        """Store a freshly selected absolute rectangle in the right form."""
        if self.mode == "window" and self.tracker is not None:
            box = self.tracker.box()      # raises WindowNotAvailable
            mode = "proportional" if scale_with_window else "anchored"
            self.rel = RelativeRegion(box, region, mode=mode)
            self.abs_region = None
        else:
            self.abs_region = dict(region)
            self.rel = None
        self.reset()

    def resolve(self, sct):
        """Absolute coordinates to grab right now."""
        if self.mode == "window":
            # A saved region comes back with a title but no tracker, and the
            # window may not exist yet. Retry each cycle so it starts working
            # whenever the app is opened.
            if self.tracker is None:
                if not self.window_title:
                    raise WindowNotAvailable(f"{self.name}: no window attached")
                self.tracker = WindowTracker()
                like = (self.rel.base_w, self.rel.base_h) if self.rel is not None else None
                try:
                    self.tracker.attach(self.window_title, like=like)
                except WindowNotAvailable:
                    self.tracker = None
                    raise

            try:
                box = self.tracker.box()
            except WindowNotAvailable:
                # Drop the handle so the next cycle looks the window up again,
                # e.g. a new window replacing one that closed.
                self.tracker = None
                raise
            if self.rel is not None:
                return self.rel.resolve(box)
            left, top, width, height = box
            return {"left": left, "top": top, "width": width, "height": height}

        if self.abs_region is not None:
            return dict(self.abs_region)
        return dict(sct.monitors[1])      # whole primary monitor

    # -- persistence ------------------------------------------------------

    def to_dict(self):
        return {
            "name": self.name,
            "mode": self.mode,
            "window_title": self.window_title,
            "enabled": self.enabled,
            "abs_region": self.abs_region,
            "rel": self.rel.to_dict() if self.rel is not None else None,
        }

    @classmethod
    def from_dict(cls, d):
        """
        Rebuild a saved region. Window-mode regions are left unattached; the
        tracker is created lazily on first resolve, so a region whose window
        isn't open yet waits instead of failing to load.
        """
        region = cls(
            d.get("name", "Region"),
            mode=d.get("mode", "screen"),
            window_title=d.get("window_title"),
        )
        region.enabled = bool(d.get("enabled", True))
        region.abs_region = d.get("abs_region")
        rel = d.get("rel")
        region.rel = RelativeRegion.from_dict(rel) if rel else None
        return region

    def describe(self):
        if self.mode == "window":
            scope = "whole window" if self.rel is None else "region"
            return f"window {self.window_title!r} - {scope}"
        if self.abs_region is None:
            return "full screen"
        r = self.abs_region
        return f"screen {r['width']}x{r['height']} at {r['left']},{r['top']}"

    # -- dedupe -----------------------------------------------------------

    def new_lines(self, text):
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

    def frame_changed(self, frame):
        h = hash(frame[::8, ::8].tobytes())
        if h == self._last_hash:
            return False
        self._last_hash = h
        return True

    def reset(self):
        self._seen.clear()
        self._seen_set.clear()
        self._last_hash = None
        self._last_text = None


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


def grab_rgb(sct, area):
    shot = sct.grab(area)
    return np.array(shot)[:, :, :3][:, :, ::-1]  # BGRA -> RGB


class ScreenReader(threading.Thread):
    """
    Walks the region list once per interval. Never touches a widget - results
    go onto a queue the UI thread drains, because tkinter is not thread-safe.
    """

    def __init__(self, ocr, out_queue):
        super().__init__(daemon=True)
        self.ocr = ocr
        self.out = out_queue

        self.running = threading.Event()  # capturing vs paused
        self.alive = threading.Event()
        self.alive.set()

        self.interval = 1.0
        self.enhance = True
        self.new_lines_only = True
        self.preview_processed = False    # preview shows what OCR sees

        self.regions = []                 # list[Region], order matters
        self.preview_request = None       # Region awaiting a preview grab

        self.bus = None                   # optional EventBus
        self.profile = ""                 # stamped onto published events
        self.snapshot_events = False      # one whole-screen event per pass

        self._texts = {}                  # region name -> latest OCR text
        self._texts_changed = False

        self._last_status = {}

    def run(self):
        import mss

        # mss instances are not safe to share across threads, so this is
        # created here in the worker rather than in __init__.
        with mss.mss() as sct:
            while self.alive.is_set():
                if self.preview_request is not None:
                    self._do_preview(sct, self.preview_request)
                    self.preview_request = None

                if not self.running.is_set():
                    time.sleep(0.15)      # still responsive to preview requests
                    continue

                for region in list(self.regions):  # snapshot; UI may reorder
                    if not self.alive.is_set() or not self.running.is_set():
                        break
                    if region.enabled:
                        self._scan(sct, region)

                self._publish_snapshot()
                time.sleep(self.interval)

    # -- per-region work --------------------------------------------------

    def _scan(self, sct, region):
        try:
            area = region.resolve(sct)
        except WindowNotAvailable as e:
            # Wait for the window rather than reading whatever moved into
            # those coordinates.
            self._status(region.name, f"{region.name}: waiting - {e}")
            return

        try:
            frame = grab_rgb(sct, area)
        except Exception as e:
            self._status(region.name, f"{region.name}: capture failed - {e}")
            return

        if not region.frame_changed(frame):
            return

        try:
            prepared = preprocess(frame) if self.enhance else frame
        except Exception as e:
            self._status(region.name, f"{region.name}: preprocessing failed - {e}")
            return

        # Preview after preprocessing, so "what OCR sees" is literally true -
        # with enhancement off, prepared is the raw frame and both agree.
        self._send_preview(region, prepared if self.preview_processed else frame)

        try:
            text = self.ocr.read(prepared)
        except Exception as e:
            self._status(region.name, f"{region.name}: OCR failed - {e}")
            return

        self._status(region.name, None)
        if self._texts.get(region.name) != text:
            self._texts[region.name] = text
            self._texts_changed = True
        self._emit(region, text)

    def _send_preview(self, region, img):
        self.out.put(("preview", (region.name, to_png_b64(thumbnail(img)))))

    def _emit(self, region, text):
        if not text:
            return
        if self.new_lines_only:
            fresh = region.new_lines(text)
            if fresh:
                self.out.put(("text", (region.name, "\n".join(fresh))))
                self._publish(region, fresh)
        elif text != region._last_text:
            region._last_text = text
            self.out.put(("text", (region.name, text)))
            self._publish(region, text.splitlines())

    def _publish(self, region, lines):
        """One event per line - simpler for a downstream consumer to handle."""
        if self.bus is None or self.snapshot_events:
            return
        for line in lines:
            line = line.strip()
            if line:
                self.bus.publish(make_event(self.profile, region.name, line))

    def _publish_snapshot(self):
        """Only when something changed, so a static screen sends nothing."""
        if self.bus is None or not self.snapshot_events or not self._texts_changed:
            return
        self._texts_changed = False
        live = {r.name for r in self.regions if r.enabled}
        texts = {n: t for n, t in self._texts.items() if n in live}
        self.bus.publish(make_snapshot(self.profile, texts))

    def _do_preview(self, sct, region):
        try:
            frame = grab_rgb(sct, region.resolve(sct))
            if self.preview_processed and self.enhance:
                frame = preprocess(frame)
        except Exception as e:
            self.out.put(("status", f"{region.name}: preview failed - {e}"))
            return
        self._send_preview(region, frame)

    def _status(self, key, msg):
        """Push a status line, but only when it changes for that region."""
        if self._last_status.get(key) == msg:
            return
        self._last_status[key] = msg
        if msg:
            self.out.put(("status", msg))

    def reset(self):
        self._last_status.clear()
        self._texts.clear()
        self._texts_changed = False
        for region in self.regions:
            region.reset()
