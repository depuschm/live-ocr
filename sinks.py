#!/usr/bin/env python3
"""
sinks.py - deliver captured lines to things outside the app.

Sinks run on their own thread behind a queue. The capture loop publishes and
moves on, so a slow or dead consumer can never stall OCR.

Two sinks ship today:

  JsonlSink    append one JSON object per line to a file
  WebhookSink  POST the same object to an HTTP endpoint

The file is the durable record: it survives the consumer being down, and can
be replayed. The webhook is the live notification, and is throttled.
"""

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


def make_event(profile, region, text):
    """The payload every sink receives. Keep this stable - consumers parse it."""
    return {
        "v": SCHEMA_VERSION,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": profile,
        "region": region,
        "text": text,
    }


def make_snapshot(profile, regions, images=None):
    """
    Every region's latest text in one event, sent once per pass.

    Line events suit logs. Consumers that interpret a whole screen need all
    values together, including ones that did not change.
    """
    return {
        "v": SCHEMA_VERSION,
        "type": "snapshot",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": profile,
        "regions": dict(regions),
        "images": dict(images or {}),   # base64 PNG, for regions set to send pixels
    }


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------


class JsonlSink:
    """
    Append events as JSON Lines, rotating at a size cap.

    Rotation keeps exactly one previous file (.1). A tailing consumer that
    tracks the inode will notice the swap; see examples/consumer.py.
    """

    def __init__(self, path, max_bytes=5_000_000):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._fh = None

    def _open(self):
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        return self._fh

    def _rotate_if_needed(self):
        try:
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                self.close()
                os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
        except OSError:
            pass

    def deliver(self, event):
        self._rotate_if_needed()
        fh = self._open()
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        fh.flush()  # so a tailing consumer sees it immediately

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def describe(self):
        return f"file {self.path}"


class WebhookSink:
    """
    POST events as JSON.

    Throttled per region: a line that keeps matching would otherwise fire on
    every capture cycle. Failures are reported and dropped - there is no retry
    queue, because the JSONL file is the durable record.
    """

    def __init__(self, url, cooldown=10.0, timeout=5.0):
        self.url = url
        self.cooldown = cooldown
        self.timeout = timeout
        self._last_sent = {}

    def deliver(self, event):
        region = event.get("region", "")
        now = time.monotonic()
        last = self._last_sent.get(region)
        if last is not None and now - last < self.cooldown:
            return  # still cooling down for this region
        self._last_sent[region] = now

        body = json.dumps(event).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "live-ocr",
            },
            method="POST",
        )
        # Short timeout: a hung endpoint must not back up the sink queue.
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read(65536)
        # A consumer may answer with a JSON object carrying a "message".
        # Anything else is treated as a plain acknowledgement.
        try:
            reply = json.loads(raw)
        except ValueError:
            return None
        return reply if isinstance(reply, dict) and "message" in reply else None

    def close(self):
        pass

    def describe(self):
        return f"webhook {self.url}"


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


class EventBus(threading.Thread):
    """
    Fan events out to every configured sink, off the capture thread.

    on_error is called with a human-readable string when a sink raises; the
    app routes that to the status bar. One failing sink never stops another.
    """

    def __init__(self, on_error=None, on_reply=None, max_pending=1000):
        super().__init__(daemon=True)
        self.queue = queue.Queue(maxsize=max_pending)
        self.on_error = on_error
        self.on_reply = on_reply  # called with a consumer's reply dict
        self.alive = threading.Event()
        self.alive.set()

        self._sinks = []
        self._lock = threading.Lock()
        self._dropped = 0

    def set_sinks(self, sinks):
        with self._lock:
            for s in self._sinks:
                try:
                    s.close()
                except Exception:
                    pass
            self._sinks = list(sinks)

    def active(self):
        with self._lock:
            return [s.describe() for s in self._sinks]

    def publish(self, event):
        """Never blocks. Drops the event if consumers have fallen far behind."""
        with self._lock:
            if not self._sinks:
                return
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1 and self.on_error:
                self.on_error(f"Sink queue full - dropped {self._dropped} events")

    def run(self):
        while self.alive.is_set():
            try:
                event = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue

            with self._lock:
                sinks = list(self._sinks)

            for sink in sinks:
                try:
                    reply = sink.deliver(event)
                    if reply and self.on_reply:
                        self.on_reply(reply)
                except urllib.error.URLError as e:
                    self._report(f"{sink.describe()}: unreachable ({e.reason})")
                except Exception as e:
                    self._report(f"{sink.describe()}: {e}")

    def _report(self, msg):
        if self.on_error:
            self.on_error(msg)

    def stop(self):
        self.alive.clear()
        self.set_sinks([])
