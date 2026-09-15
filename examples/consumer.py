#!/usr/bin/env python3
"""
consumer.py - a reference consumer for live-ocr events.

This is a worked example, not part of the app. Copy it and replace handle()
with whatever your connector should do.

Two ways to receive events, matching the two sinks:

    python consumer.py tail ~/.live-ocr/captures/default.jsonl
        Follow the JSONL file. Survives restarts on either side and can
        replay history. This is the one to start with.

    python consumer.py serve 8000
        Run an HTTP endpoint at http://localhost:8000/ for the webhook sink.
        Live push, but events are lost while this is not running.

Standard library only - no dependencies.

Event shape:

    {
      "v": 1,
      "ts": "2026-09-15T02:19:55+00:00",
      "profile": "Default",
      "region": "Build log",
      "text": "error: connection refused"
    }
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


# --------------------------------------------------------------------------
# Your connector goes here
# --------------------------------------------------------------------------


def handle(event):
    """
    Called once per captured line, from either transport.

    Replace this body with the real work: insert a row, call an API, update a
    dashboard, trigger an automation. Keep it quick, or push onto your own
    queue - the tailer is single-threaded and the HTTP handler holds the
    connection open while this runs.
    """
    print(f"[{event['ts']}] {event['profile']} / {event['region']}: {event['text']}")

    # Example of acting on content rather than just logging it:
    if "error" in event["text"].lower():
        print("   ^ looks like an error - this is where you'd raise an alert")


# --------------------------------------------------------------------------
# Transport 1: tail the JSONL file
# --------------------------------------------------------------------------


def tail(path, from_start=False):
    """
    Follow a JSONL file, surviving rotation.

    live-ocr rotates the file once it passes its size cap, so the path can be
    replaced underneath us. Comparing the file's identity - inode on Unix,
    (index, volume) via st_ino/st_dev on Windows - detects the swap. Watching
    only for the size shrinking would miss a rotation that happened while
    this process was stopped.
    """
    path = Path(path)
    print(f"Tailing {path}  (Ctrl-C to stop)")

    fh = None
    file_id = None
    first_open = True

    try:
        while True:
            try:
                stat = path.stat()
                current_id = (stat.st_dev, stat.st_ino)
            except FileNotFoundError:
                if fh:
                    fh.close()
                    fh, file_id = None, None
                time.sleep(1.0)
                continue

            if fh is None or current_id != file_id:
                rotated = fh is not None
                if fh:
                    print("  (file rotated - reopening)")
                    fh.close()
                fh = open(path, "r", encoding="utf-8")
                file_id = current_id
                # Skip existing history on the very first open unless asked
                # otherwise; a file that appeared via rotation is read whole.
                if first_open and not from_start and not rotated:
                    fh.seek(0, 2)
                first_open = False

            line = fh.readline()
            if not line:
                time.sleep(0.3)  # caught up; poll rather than spin
                continue

            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A partially-flushed final line. Back up and retry it whole.
                fh.seek(fh.tell() - len(line) - 1)
                time.sleep(0.2)
                continue

            dispatch(event)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if fh:
            fh.close()


# --------------------------------------------------------------------------
# Transport 2: receive webhook POSTs
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)

        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        # Answer first, work second: live-ocr uses a short timeout, and a slow
        # handler would make it report the endpoint as unreachable.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

        dispatch(event)

    def log_message(self, *args):
        pass  # quiet; handle() does the reporting


def serve(port):
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Listening on http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    print("Point live-ocr's webhook URL at that address.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


# --------------------------------------------------------------------------
# Shared
# --------------------------------------------------------------------------


def dispatch(event):
    """Validate lightly, then hand off. One bad event must not kill the loop."""
    if not isinstance(event, dict) or "text" not in event:
        print(f"  (skipping unrecognised event: {event!r})")
        return
    if event.get("v") != 1:
        print(f"  (unexpected schema version {event.get('v')!r}, trying anyway)")

    event.setdefault("ts", "")
    event.setdefault("profile", "")
    event.setdefault("region", "")

    try:
        handle(event)
    except Exception as e:
        print(f"  handler failed: {e}")


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip())
        return 1

    mode = argv[1]
    if mode == "tail":
        if len(argv) < 3:
            print("usage: consumer.py tail <path.jsonl> [--from-start]")
            return 1
        tail(argv[2], from_start="--from-start" in argv)
    elif mode == "serve":
        serve(int(argv[2]) if len(argv) > 2 else 8000)
    else:
        print(f"unknown mode {mode!r}; expected 'tail' or 'serve'")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
