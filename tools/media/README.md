# Video and presentation scripts

These scripts remake the two files the Help menu opens (INS-16): the narrated walkthrough video
`docs/media/rdt-walkthrough.mp4` (with `.srt` captions) and the slides `docs/media/rdt-presentation.pdf`.
Everything runs against the simulator, never a real device. Remake both whenever a release adds or
changes something a user would see.

| File | Does |
| --- | --- |
| `walkthrough.py` | Records the live walkthrough: a real pointer drives the GUI, then the CLI in a terminal, while ffmpeg grabs the screen. One `@step` per narrated action; each step lasts as long as its sentence. |
| `assemble.py` | Turns the recording into the finished video: title and closing cards, a smooth zoom on the pointer, narration, chapter labels and burnt-in captions, plus an `.srt`. |
| `capture.py` | Offscreen stills and short clips of the GUI that the slides use. |
| `render.py` | Renders each slide in `deck/` to a 1920x1080 PNG with headless Chromium and saves its speaker notes. |
| `pdf.py` | Joins the rendered slides into the presentation PDF. |
| `deck_video.py` | The older slide-by-slide narrated video (not in the repo; optional). |
| `common.py` | Paths, the narrator's pronunciations and the Kokoro voice. |
| `bench/` | The workspace the walkthrough opens (the ELFs are copied in from `sim/examples/firmware`). |
| `deck/` | The slides: `deck.json` gives the order, `slides/*.html` one slide each, with its notes in `<aside>`. |

Output goes under `build/media/` in the repo (git-ignored), or wherever `RDT_MEDIA_WORK` points.

## Setup (Linux)

```
sudo apt-get install ffmpeg xvfb openbox xdotool xterm x11-utils \
    libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-image0 libxcb-render-util0 \
    libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 libxcb-shape0
python -m pip install -e .[dev]
python -m pip install kokoro-onnx soundfile opencv-python-headless pillow
```

- **Voice.** Download `kokoro-v1.0.onnx` and `voices-v1.0.bin` from
  <https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0> into `build/media/voice/`.
  They are about 340 MB, so they are not in the repo. The voice is `am_michael`.
- **Fonts.** Install IBM Plex Sans and JetBrains Mono (for example into `~/.fonts`); the slides,
  captions and terminal use them.
- **Chromium** for `render.py`: Playwright's headless shell is found on its own; otherwise set
  `CHROME` to a Chromium binary.

## Remake the video

```
Xvfb :99 -screen 0 1920x1080x24 &
DISPLAY=:99 openbox &
DISPLAY=:99 python tools/media/walkthrough.py          # about 14 minutes, records build/media/live/
python tools/media/capture.py && python tools/media/render.py   # the title and closing cards
python tools/media/assemble.py build/media/rdt-full-walkthrough.mp4
```

`walkthrough.py --voice-only` makes only the narration, to check timing after editing a step. After a
run, `build/media/live/errors.json` must be `[]`: it lists every step that failed. Look at a few frames
of the result before using it. `assemble.py --reuse` skips the slow zoom pass when only captions or
cards changed.

The repo copy is smaller, re-encoded for screen content:

```
ffmpeg -i build/media/rdt-full-walkthrough.mp4 -c:v libx264 -preset slow -crf 27 -tune stillimage \
    -c:a aac -b:a 96k -movflags +faststart docs/media/rdt-walkthrough.mp4
cp build/media/rdt-full-walkthrough.srt docs/media/rdt-walkthrough.srt
```

## Remake the presentation

```
python tools/media/capture.py          # stills, against an in-process simulator
python tools/media/render.py
python tools/media/pdf.py              # writes docs/media/rdt-presentation.pdf
```

## When RDT changes

- A new feature: add a slide in `deck/slides/` (and its id in `deck.json` "order") and one or more
  `@step`s in `walkthrough.py` in the chapter where a user would look for it.
- A changed screen: re-run the scripts; fix any step whose lookup no longer finds its widget
  (`errors.json` names it).
- New GUI stills for a slide: add the shot in `capture.py` and map its blob id in `render.py`'s `BLOBS`.
