# live-ocr

Cross-platform screen OCR that continuously reads on-screen text into a desktop window.

Define one or more capture regions, anchor them to application windows, and watch the text in them stream into a labelled log. Regions follow their window as it moves and resizes, save between runs, and group into named profiles for different projects. Captures can be streamed to a file or a webhook for another service to consume.

## Why

Most screenshot-OCR tools are one-shot: you take a picture, you get text back. This one watches instead, which is more useful when the thing you care about keeps changing — logs scrolling past, a video you can't copy from, a UI that won't let you select text.

It also installs entirely through pip. No Tesseract binary, no package manager step, no PATH surgery.

## Install

```bash
pip install mss numpy rapidocr-onnxruntime pywinctl
python live_ocr.py
```

First run downloads the OCR models (~10 seconds, once). The window opens immediately and Start stays disabled until they're ready.

`pywinctl` is optional and only powers window attachment — without it you can still define regions against the screen. The UI uses tkinter, which ships with Python on Windows and macOS but is packaged separately on some Linux distributions:

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

1. Under **New region target**, pick **Whole screen** or an application window (**Refresh windows** re-scans if it opened after launch).
2. Click **Add** and drag a box over the area you want to watch.
3. Repeat for as many regions as you need.
4. **Start**.

Each region is read once per interval and its text appears in the log tagged with the region's name. Your setup is saved automatically — see [Profiles](#profiles) for keeping separate sets per project.

### Regions

| Control | What it does |
| --- | --- |
| **Add** | Create a region against the current target and drag out its area. |
| **Delete** | Remove the selected region. |
| **Up / Down** | Reorder. Regions are scanned in list order. |
| **Rename** | Give it a meaningful name — this is what labels its output. Double-clicking works too. |
| **Reselect area** | Redraw the box without recreating the region. |
| **Preview** | Live thumbnail of what the selected region is capturing. Updates while running; selecting a region while stopped grabs a fresh frame. |
| **Show what OCR sees** | Switch the preview from raw pixels to the preprocessed frame. |
| **On/Off** | Skip a region without deleting it. Disabled regions show `(off)` and cost nothing. |

Each region has its own target, so you can mix freely — one following your editor, another pinned to a fixed corner of the screen.

Regions also dedupe independently, so the same value appearing in two of them is reported in both rather than suppressed in whichever is scanned second.

### Profiles

Each profile holds its own regions and settings, so you can keep one set up for build logs, another for a trading screen, and switch between them from the dropdown. The active profile shows in the window title.

| Control | What it does |
| --- | --- |
| **New** | Empty profile. |
| **Duplicate** | Copy the current regions and settings under a new name. |
| **Rename** | Refused if the name is already taken. |
| **Delete** | Asks first. You can't delete the last remaining profile. |
| **Open folder** | Reveal `~/.live-ocr/` in your file manager. Saves first, so what you see is current. |

Profiles live in `~/.live-ocr/profiles/`, one JSON file each — `Build logs` becomes `build-logs.json`. One file per profile means you can copy one to another machine, commit one into a project repo, or delete it by hand without disturbing the others. `~/.live-ocr/state.json` remembers which was last open, plus the window size.

Everything is written after each change rather than on exit, so a crash won't lose your setup, and writes are atomic — a temporary file replaced into place — so an interruption can't leave a truncated file. Switching profiles saves the current one first.

A corrupt or unreadable profile loads empty rather than crashing, and a single malformed region is skipped while the rest load. Delete the file to reset that profile.

### Window regions across restarts

Window-anchored regions store the window title and reattach on their own. If the app isn't open when you launch, that region waits and starts working the moment it appears; you don't need to reselect it. Matching is on the exact title, so an app that puts the current filename in its title bar won't match after you switch files. For titles like that, end the saved title with `*` to match it as a prefix: `My Editor*` matches `My Editor - notes.txt`.

If several windows share a title, a region only attaches to one with about the same proportions (within 5%) as the window it was drawn on. When none fits, it waits rather than reading the wrong window, and it picks up a matching window as soon as one opens, including a new one replacing a closed window. Windows with the same title and the same proportions can't be told apart.

### When a region reads badly

Tick **Show what OCR sees** to swap the preview from raw pixels to the frame that actually reaches the engine — grayscaled, contrast-stretched, inverted if it was light-on-dark, and upscaled. That usually makes the cause obvious: text too faint after the stretch, an inversion that shouldn't have happened, or a region so large the upscale got capped.

From there, shrinking the region is the most reliable fix. Turning **Enhance image** off is worth a try on content that's already dark-on-light at a comfortable size. With enhancement off the two preview modes show the same thing, since the raw frame is then what OCR receives.

### Capture

| Control | What it does |
| --- | --- |
| **Start / Stop** | Begin or pause. Pausing keeps the OCR engine loaded, so resuming is instant. |
| **Interval** | Seconds between passes over the region list. Default `1.0`. |
| **Enhance image** | Preprocessing before OCR. On by default; turn off to compare. |
| **New lines only** | Report only lines not seen recently, instead of the whole capture. |
| **Always on top** | Keeps the window visible while you work in another app. |
| **Auto-scroll** | Follows new output. Turn it off to read back without being yanked to the bottom. |
| **Copy all** | Everything in the log to the clipboard. |
| **Outputs...** | Configure the JSONL file and webhook sinks for this profile. |

### What happens when a window resizes

Two reasonable answers, so it's an explicit choice rather than a guess. Set **Scale with window** before adding a region.

**Off (default).** The region keeps its pixel size and stays pinned to whichever corner it was nearest. Usually what you want, because text doesn't get bigger when a window does — a status bar stays glued to the bottom edge, a toolbar stays top-left.

**On.** The region grows and shrinks proportionally. Use this when content genuinely reflows with size, like a full-width document body.

If a region's window is minimised or closed, that region pauses and the status bar says what it's waiting for, then resumes when the window returns. It won't fall back to reading whatever moved into those coordinates. Other regions keep running.

## Sending captures somewhere else

Captured lines can be handed to another service. Open **Outputs...** to configure two independent sinks, saved per profile.

**JSON Lines file.** One JSON object per line, appended as text is captured. This is the one to start with: it survives your consumer being offline, can be replayed, and is readable with `tail -f`. Defaults to `~/.live-ocr/captures/<profile>.jsonl` and rotates at 5 MB, keeping one previous file as `.1`.

**Webhook.** The same object POSTed to an HTTP endpoint. Live push rather than pull, and `http://localhost:...` works fine — nothing leaves your machine. Events are lost while the endpoint is down, which is why the file is the durable record. **Test** sends a sample event so you can confirm the endpoint before relying on it.

Webhooks are throttled per region by a configurable cooldown, because a line that stays on screen would otherwise fire on every capture cycle. The file sink is not throttled.

Delivery runs on its own thread behind a queue, so a slow or dead consumer never stalls OCR. Failures appear in the status bar and are dropped rather than retried.

### Event format

One event per captured line:

```json
{
  "v": 1,
  "ts": "2026-09-15T02:19:55+00:00",
  "profile": "Default",
  "region": "Build log",
  "text": "error: connection refused"
}
```

`v` is the schema version — check it if you care about forward compatibility.

### Building a connector

`examples/consumer.py` is a working reference for both transports, standard library only. Replace its `handle()` function with whatever your connector should do.

```bash
# follow the file (survives restarts on either side)
python examples/consumer.py tail ~/.live-ocr/captures/default.jsonl

# or receive webhook POSTs
python examples/consumer.py serve 8000
```

The tailer skips existing history by default (`--from-start` to replay), follows rotation by watching the file's identity rather than its size, and retries a partially-written final line instead of dropping it. Worth copying those details if you write your own — they're the parts that bite later.

### Consumers that reply

For consumers that need all regions at once, tick **Send one snapshot of all regions per pass** in **Outputs...**. Instead of one event per line, each pass that changed anything sends every enabled region's latest text together:

```json
{"v": 1, "type": "snapshot", "ts": "2026-09-15T02:19:55+00:00", "profile": "Default",
 "regions": {"Status": "Build passed", "Counter": "42"}}
```

A webhook consumer may answer with a JSON object containing `message` (and optionally `consumer`, used as the log label). live-ocr shows the message in its log. Any other response body is treated as a plain acknowledgement.

## Performance

OCR cost scales linearly with region count — four regions at `1.0` means four OCR passes per second. If it can't keep up, raise the interval or delete regions you aren't reading. Unchanged regions are skipped before reaching OCR, so idle areas are nearly free.

Keeping regions small is the single biggest win for both speed and accuracy. OCR over a full 4K desktop is slow and picks up menu bars, tab titles, and dock icons you don't want.

## How it works

1. `mss` grabs each region's area. For window-anchored regions that area is recomputed from the window's live bounds every cycle.
2. A hash of a downsampled copy is compared to that region's previous frame. If nothing changed it's discarded before reaching OCR, which keeps a static screen from burning CPU.
3. Surviving frames are converted to grayscale, contrast-stretched on the 2nd/98th percentiles, auto-inverted if light-on-dark, and upscaled 2x. OCR models are trained on dark text over light backgrounds at document-scale sizes, and screen text breaks all three assumptions.
4. Results below a confidence threshold (default `0.5`) are dropped.
5. Lines are checked against the last 400 that region has seen, and only new ones are displayed.

Capture and OCR run on a worker thread that never touches a widget — results reach the UI over a queue, since tkinter isn't thread-safe. Preview thumbnails are encoded as base64 PNG, which Tk reads natively, avoiding a Pillow dependency.

## Layout

| File | Contents |
| --- | --- |
| `live_ocr.py` | UI and entry point |
| `capture.py` | Regions, preprocessing, OCR backends, capture worker |
| `window_track.py` | Window bounds tracking and window-relative geometry |
| `config.py` | Profiles: saving and loading regions and settings |
| `sinks.py` | Event dispatch to the file and webhook sinks |
| `examples/consumer.py` | Reference consumer — copy this to build a connector |

## Known limitations

- **Occlusion.** Capture reads screen pixels, so another window covering your target will be read instead. Anchoring tracks position, not content.
- Window bounds include the title bar and borders, so a region pinned near the top edge can shift if the title bar height changes.
- Text over busy backgrounds (video, gradients) is unreliable.
- The interval is a floor, not a guarantee — a pass over several large regions can take longer than the interval itself.
- Line dedupe is exact-match, so a line the OCR reads slightly differently between frames will be reported twice — and sent downstream twice. The webhook cooldown limits the damage; the file sink records both.
- Saved window regions match on the exact title or a `*` prefix. A title that changes in the middle won't reattach.
- Windows with the same title and the same proportions can't be told apart; a region takes whichever the system lists first.

## Requirements

Python 3.8+

## License

MIT
