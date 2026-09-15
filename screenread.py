#!/usr/bin/env python3
"""
screenread.py - cross-platform screen OCR, printed to console.

Captures the screen on an interval, runs OCR, and prints any text that
changed since the last frame. Control it live from the console:

    start            begin capturing
    stop             pause capturing
    interval 0.5     seconds between captures (default 1.0)
    region 0,0,800,600   limit capture to left,top,width,height
    region full      go back to the whole monitor
    monitors         list available monitors
    monitor 2        switch monitor
    quit             exit

Install:
    pip install mss numpy
    pip install rapidocr-onnxruntime     # preferred: no system binary needed
    # or, as a fallback:
    pip install pytesseract              # also needs the tesseract binary installed
"""

import sys
import threading
import time

import numpy as np

# --------------------------------------------------------------------------
# OCR backend
# --------------------------------------------------------------------------
# Two backends, tried in order. RapidOCR is the better default because it
# installs entirely through pip and is accurate on screen/UI text. Tesseract
# is the fallback but needs a separate system-level binary and tends to be
# weaker on anti-aliased UI fonts.


class OCREngine:
    def __init__(self, min_confidence=0.5):
        self.min_confidence = min_confidence
        self.backend = None
        self._engine = None
        self._load()

    def _load(self):
        try:
            from rapidocr_onnxruntime import RapidOCR

            print("Loading RapidOCR (first run downloads models, ~10s)...")
            self._engine = RapidOCR()
            self.backend = "rapidocr"
            return
        except ImportError:
            pass

        try:
            import pytesseract  # noqa: F401

            self._engine = pytesseract
            self.backend = "tesseract"
            print("Using pytesseract backend.")
            return
        except ImportError:
            pass

        sys.exit(
            "No OCR backend found. Install one:\n"
            "  pip install rapidocr-onnxruntime   (recommended)\n"
            "  pip install pytesseract            (also needs the tesseract binary)"
        )

    def read(self, img):
        """Take an RGB numpy array, return extracted text as a string."""
        if self.backend == "rapidocr":
            return self._read_rapidocr(img)
        return self._read_tesseract(img)

    def _read_rapidocr(self, img):
        raw = self._engine(img)

        # RapidOCR's return shape has changed across versions, so normalise it
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
# Capture loop
# --------------------------------------------------------------------------


class ScreenReader(threading.Thread):
    def __init__(self, ocr):
        super().__init__(daemon=True)
        self.ocr = ocr
        self.running = threading.Event()   # capturing vs paused
        self.alive = threading.Event()     # thread should keep existing
        self.alive.set()

        self.interval = 1.0
        self.monitor_index = 1             # mss: 0 = all screens, 1 = primary
        self.region = None                 # dict or None for full monitor

        self._last_frame_hash = None
        self._last_text = None

    def run(self):
        import mss

        # mss instances are not safe to share across threads, so it is created
        # here inside the worker rather than in __init__.
        with mss.mss() as sct:
            while self.alive.is_set():
                if not self.running.wait(timeout=0.2):
                    continue

                try:
                    area = self.region or sct.monitors[self.monitor_index]
                    shot = sct.grab(area)
                except Exception as e:
                    print(f"\n[capture error] {e}")
                    self.running.clear()
                    continue

                frame = np.array(shot)[:, :, :3][:, :, ::-1]  # BGRA -> RGB

                if self._unchanged(frame):
                    time.sleep(self.interval)
                    continue

                try:
                    text = self.ocr.read(frame)
                except Exception as e:
                    print(f"\n[ocr error] {e}")
                    time.sleep(self.interval)
                    continue

                if text and text != self._last_text:
                    self._last_text = text
                    stamp = time.strftime("%H:%M:%S")
                    print(f"\n--- {stamp} ---\n{text}\n> ", end="", flush=True)

                time.sleep(self.interval)

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


# --------------------------------------------------------------------------
# Console control
# --------------------------------------------------------------------------


def main():
    ocr = OCREngine()
    reader = ScreenReader(ocr)
    reader.start()

    print(f"\nReady (backend: {ocr.backend}). Type 'start' to begin, 'quit' to exit.")

    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            continue

        parts = raw.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "start":
            reader.reset()
            reader.running.set()
            print(f"Capturing every {reader.interval}s. Type 'stop' to pause.")

        elif cmd == "stop":
            reader.running.clear()
            print("Paused.")

        elif cmd == "interval":
            try:
                reader.interval = max(0.1, float(arg))
                print(f"Interval set to {reader.interval}s.")
            except ValueError:
                print("Usage: interval 0.5")

        elif cmd == "region":
            if arg.lower() in ("full", "none", ""):
                reader.region = None
                print("Region cleared - capturing the full monitor.")
            else:
                try:
                    left, top, width, height = (int(v) for v in arg.split(","))
                    reader.region = {
                        "left": left,
                        "top": top,
                        "width": width,
                        "height": height,
                    }
                    print(f"Region set to {reader.region}.")
                except ValueError:
                    print("Usage: region left,top,width,height   (e.g. region 0,0,800,600)")
            reader.reset()

        elif cmd == "monitors":
            import mss

            with mss.mss() as sct:
                for i, m in enumerate(sct.monitors):
                    label = "all screens" if i == 0 else f"monitor {i}"
                    print(f"  {i}: {label} - {m}")

        elif cmd == "monitor":
            try:
                reader.monitor_index = int(arg)
                reader.region = None
                reader.reset()
                print(f"Switched to monitor {reader.monitor_index}.")
            except ValueError:
                print("Usage: monitor 1")

        elif cmd in ("quit", "exit", "q"):
            break

        else:
            print("Commands: start, stop, interval, region, monitors, monitor, quit")

    reader.running.clear()
    reader.alive.clear()
    print("Bye.")


if __name__ == "__main__":
    main()
