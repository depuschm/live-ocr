#!/usr/bin/env python3
"""
window_track.py - attach capture regions to an application window.

Absolute screen coordinates break as soon as the target window moves. This
module tracks a window's live bounds and stores regions relative to it, so a
selection keeps pointing at the same part of the UI.

Optional dependency:
    pip install pywinctl
"""


def _pwc():
    """Import pywinctl lazily so the rest of the app runs without it."""
    try:
        import pywinctl

        return pywinctl
    except ImportError:
        return None


# --------------------------------------------------------------------------
# Relative regions
# --------------------------------------------------------------------------


class RelativeRegion:
    """
    A capture area stored relative to a window rather than to the screen.

    Two modes, because "the window got bigger" has two reasonable answers:

    anchored (default)
        Keep the region the same pixel size and pin it to whichever corner it
        was nearest. Correct for most UI chrome - a status bar, a toolbar, a
        sidebar - because text does not grow when the window does.

    proportional
        Scale the region with the window. Correct when content genuinely
        reflows with size, such as a full-width document body.
    """

    def __init__(self, win_box, region, mode="anchored"):
        wl, wt, ww, wh = win_box
        dx = region["left"] - wl
        dy = region["top"] - wt
        self.w = max(1, int(region["width"]))
        self.h = max(1, int(region["height"]))
        self.mode = mode

        ww = max(1, ww)
        wh = max(1, wh)
        self.base_w, self.base_h = ww, wh

        # Pin to the nearer edge on each axis, judged by the region's centre.
        self.from_right = (dx + self.w / 2) > ww / 2
        self.from_bottom = (dy + self.h / 2) > wh / 2

        self.off_x = (ww - (dx + self.w)) if self.from_right else dx
        self.off_y = (wh - (dy + self.h)) if self.from_bottom else dy

        # Fractions, used only in proportional mode.
        self.fx, self.fy = dx / ww, dy / wh
        self.fw, self.fh = self.w / ww, self.h / wh

    def to_dict(self):
        return {
            "w": self.w, "h": self.h, "mode": self.mode,
            "base_w": self.base_w, "base_h": self.base_h,
            "from_right": self.from_right, "from_bottom": self.from_bottom,
            "off_x": self.off_x, "off_y": self.off_y,
            "fx": self.fx, "fy": self.fy, "fw": self.fw, "fh": self.fh,
        }

    @classmethod
    def from_dict(cls, d):
        """Rebuild from stored fields, bypassing the window-relative __init__."""
        obj = cls.__new__(cls)
        obj.w, obj.h = d["w"], d["h"]
        obj.mode = d.get("mode", "anchored")
        obj.base_w, obj.base_h = d["base_w"], d["base_h"]
        obj.from_right, obj.from_bottom = d["from_right"], d["from_bottom"]
        obj.off_x, obj.off_y = d["off_x"], d["off_y"]
        obj.fx, obj.fy = d["fx"], d["fy"]
        obj.fw, obj.fh = d["fw"], d["fh"]
        return obj

    def resolve(self, win_box):
        """Return absolute screen coordinates for the window's current bounds."""
        wl, wt, ww, wh = win_box
        ww, wh = max(1, ww), max(1, wh)

        if self.mode == "proportional":
            left = wl + self.fx * ww
            top = wt + self.fy * wh
            width = self.fw * ww
            height = self.fh * wh
        else:
            width, height = self.w, self.h
            left = wl + (ww - self.off_x - width) if self.from_right else wl + self.off_x
            top = wt + (wh - self.off_y - height) if self.from_bottom else wt + self.off_y

        left, top = int(round(left)), int(round(top))
        width, height = int(round(width)), int(round(height))

        # Clamp inside the window so a shrunken window cannot produce a
        # region that spills onto whatever is behind it.
        left = max(wl, min(left, wl + ww - 1))
        top = max(wt, min(top, wt + wh - 1))
        width = max(1, min(width, wl + ww - left))
        height = max(1, min(height, wt + wh - top))

        return {"left": left, "top": top, "width": width, "height": height}


# --------------------------------------------------------------------------
# Window tracking
# --------------------------------------------------------------------------


class WindowNotAvailable(Exception):
    """Raised when the attached window is gone, minimised, or unreadable."""


class WindowTracker:
    """Finds a window by title and reports its live bounds."""

    def __init__(self):
        self._handle = None
        self.title = None

    @staticmethod
    def available():
        return _pwc() is not None

    @staticmethod
    def list_titles():
        """Visible, non-empty window titles, de-duplicated and sorted."""
        pwc = _pwc()
        if pwc is None:
            return []
        try:
            titles = {t.strip() for t in pwc.getAllTitles() if t and t.strip()}
        except Exception:
            return []
        return sorted(titles, key=str.lower)

    def attach(self, title):
        pwc = _pwc()
        if pwc is None:
            raise WindowNotAvailable("pywinctl is not installed")
        try:
            matches = pwc.getWindowsWithTitle(title)
        except Exception as e:
            raise WindowNotAvailable(f"Could not query windows: {e}")
        if not matches:
            raise WindowNotAvailable(f"No window titled {title!r}")

        self._handle = matches[0]
        self.title = title
        return self.box()

    def detach(self):
        self._handle = None
        self.title = None

    @property
    def attached(self):
        return self._handle is not None

    def box(self):
        """
        Current (left, top, width, height).

        Raises WindowNotAvailable rather than returning stale coordinates, so
        the caller pauses instead of capturing whatever moved into that spot.
        """
        if self._handle is None:
            raise WindowNotAvailable("No window attached")

        try:
            if getattr(self._handle, "isMinimized", False):
                raise WindowNotAvailable(f"{self.title!r} is minimised")
            left = int(self._handle.left)
            top = int(self._handle.top)
            width = int(self._handle.width)
            height = int(self._handle.height)
        except WindowNotAvailable:
            raise
        except Exception:
            # pywinctl raises assorted platform-specific errors once the
            # underlying window is destroyed.
            raise WindowNotAvailable(f"{self.title!r} is no longer open")

        if width < 1 or height < 1:
            raise WindowNotAvailable(f"{self.title!r} has no visible area")

        return (left, top, width, height)
