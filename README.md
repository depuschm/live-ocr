# live-ocr

Cross-platform screen OCR that continuously reads on-screen text and prints it to the console.

Point it at your screen (or a region of it), type `start`, and any text that appears gets extracted and printed. Text is only re-read when it actually changes, so a static screen costs almost nothing.

```
> start
Capturing every 1.0s. Type 'stop' to pause.

--- 14:22:07 ---
Deploy failed: connection timed out after 30s
Retrying in 60 seconds...
```

## Why

Most screenshot-OCR tools are one-shot: you take a picture, you get text back. This one watches instead, which is more useful when the thing you care about keeps changing — logs scrolling past, a video you can't copy from, a UI that won't let you select text.

It also installs entirely through pip. No Tesseract binary, no package manager step, no PATH surgery.

## Install

```bash
pip install mss numpy rapidocr-onnxruntime
```

First run downloads the OCR models (~10 seconds, once).

<details>
<summary>Using Tesseract instead</summary>

A Tesseract backend is included as a fallback. It needs the system binary installed separately, and it's noticeably weaker on anti-aliased UI fonts, so prefer RapidOCR unless you have a reason not to.

```bash
pip install mss numpy pytesseract
# plus the tesseract binary for your OS
```

The backend is chosen automatically — RapidOCR if present, Tesseract otherwise.
</details>

## Usage

```bash
python screenread.py
```

| Command | What it does |
| --- | --- |
| `start` | Begin capturing |
| `stop` | Pause without shutting down |
| `interval 0.5` | Seconds between captures (default `1.0`) |
| `region 0,0,800,600` | Limit capture to `left,top,width,height` |
| `region full` | Go back to the whole monitor |
| `monitors` | List available monitors and their geometry |
| `monitor 2` | Switch monitor |
| `quit` | Exit |

`stop` pauses the capture loop but keeps the OCR engine loaded, so toggling back on is instant.

### Narrow the region

Using `region` is the single biggest win for both speed and accuracy. OCR on a full 4K desktop is slow and picks up menu bars, tab titles, and dock icons you don't want. Run `monitors` to see the coordinate space, then crop to just the part you care about.

## Platform notes

**macOS** — you'll need to grant screen recording permission under System Settings → Privacy & Security → Screen Recording, for whichever terminal you're running from. Until you do, captures come back as blank or desktop-wallpaper-only images. A restart of the terminal app is usually required after granting it.

**Linux / Wayland** — `mss` may return black frames under Wayland, since it compositor-blocks direct screen access. Running an X11 session is the quick workaround. Under XWayland, results vary by compositor.

**Windows** — works without additional setup. On multi-DPI setups, `monitors` reports the scaled coordinates, which is what `region` expects.

## How it works

1. `mss` grabs the target region as a raw frame.
2. A hash of a downsampled copy of that frame is compared to the previous one. If nothing changed, the frame is discarded before it reaches OCR — this is what keeps a static screen from burning CPU.
3. Frames that survive go to the OCR engine. Results below a confidence threshold (default `0.5`) are dropped.
4. The extracted text is compared to the last result and only printed if it differs, which filters out noise like a blinking cursor changing pixels without changing text.

## Known limitations

- Small or low-contrast text degrades badly. Upscaling the frame before OCR helps, and isn't currently done automatically.
- Text over busy backgrounds (video, gradients) is unreliable.
- The capture interval is a floor, not a guarantee — OCR on a large region can take longer than the interval itself.

## Requirements

Python 3.8+

## License

MIT
