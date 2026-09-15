# live-ocr

Cross-platform screen OCR that continuously reads on-screen text into a desktop window.

Pick a region of your screen, hit Start, and any text that appears gets extracted and appended to a scrolling log with timestamps. Text is only re-read when it actually changes, so a static screen costs almost nothing.

## Why

Most screenshot-OCR tools are one-shot: you take a picture, you get text back. This one watches instead, which is more useful when the thing you care about keeps changing — logs scrolling past, a video you can't copy from, a UI that won't let you select text.

It also installs entirely through pip. No Tesseract binary, no package manager step, no PATH surgery.

## Install

```bash
pip install mss numpy rapidocr-onnxruntime
python live_ocr.py
```

First run downloads the OCR models (~10 seconds, once). The window opens immediately and Start stays disabled until they're ready.

The UI uses tkinter, which ships with Python on Windows and macOS. Some Linux distributions package it separately:

```bash
sudo apt install python3-tk       # Debian / Ubuntu
sudo dnf install python3-tkinter  # Fedora
```

<details>
<summary>Using Tesseract instead</summary>

A Tesseract backend is included as a fallback. It needs the system binary installed separately, and it's noticeably weaker on anti-aliased UI fonts, so prefer RapidOCR unless you have a reason not to.

```bash
pip install mss numpy pytesseract
# plus the tesseract binary for your OS
```

The backend is chosen automatically — RapidOCR if present, Tesseract otherwise. The status bar shows which one is active.
</details>

## Using it

| Control | What it does |
| --- | --- |
| **Start / Stop** | Begin or pause capturing. Pausing keeps the OCR engine loaded, so resuming is instant. |
| **Select region** | Drag a rectangle over the area to watch. The app hides itself first so it doesn't read its own output. |
| **Full screen** | Clear the region and capture the whole monitor. |
| **Interval** | Seconds between captures. Default `1.0`. |
| **Always on top** | Keeps the window visible while you work in another app. |
| **Auto-scroll** | Follows new output. Turn it off to read back without being yanked to the bottom. |
| **Copy all** | Everything in the pane to the clipboard. |

### Select a region

This is the single biggest win for both speed and accuracy. OCR on a full 4K desktop is slow and picks up menu bars, tab titles, and dock icons you don't want. Crop to just the part you care about and results improve noticeably.

## Platform notes

**macOS** — you'll need to grant screen recording permission under System Settings → Privacy & Security → Screen Recording, for whichever terminal you launch from. Until you do, captures come back blank or show only the desktop wallpaper. Restarting the terminal app after granting it is usually required.

**Linux / Wayland** — `mss` may return black frames under Wayland, since it blocks direct screen access. Running an X11 session is the quick workaround. Under XWayland, results vary by compositor. The region selector's transparency also depends on the window manager — if the overlay appears opaque rather than translucent, that's the cause.

**Windows** — works without additional setup. On multi-DPI setups, region coordinates follow the scaled coordinate space.

## How it works

1. `mss` grabs the target region as a raw frame.
2. A hash of a downsampled copy is compared to the previous frame. If nothing changed, it's discarded before reaching OCR — this is what keeps a static screen from burning CPU.
3. Surviving frames go to the OCR engine. Results below a confidence threshold (default `0.5`) are dropped.
4. Extracted text is compared to the last result and only displayed if it differs, filtering out noise like a blinking cursor changing pixels without changing text.

Capture and OCR run on a worker thread that never touches a widget — results reach the UI over a queue, since tkinter isn't thread-safe.

## Known limitations

- Small or low-contrast text degrades badly. Upscaling before OCR would help and isn't done yet.
- Text over busy backgrounds (video, gradients) is unreliable.
- The interval is a floor, not a guarantee — OCR on a large region can take longer than the interval itself.
- Diffing is whole-block, so one changed line in a scrolling log reprints the entire capture.

## Requirements

Python 3.8+

## License

MIT
