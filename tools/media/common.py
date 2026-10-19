"""Paths and narration helpers shared by the media scripts (INS-16).

Everything they make goes under WORK: build/media in the repo (git-ignored), or RDT_MEDIA_WORK.
The Kokoro model files are not in the repo; put them in WORK/voice (see README.md).
"""
import glob, os, re, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORK = Path(os.environ.get("RDT_MEDIA_WORK", ROOT / "build" / "media")).resolve()
VOICE_DIR = WORK / "voice"
VOICE, SR = "am_michael", 24000

SAY = [  # how the narrator should say things the notes spell for reading
    (r"\bR D T\b", "R D T"), (r"\bRDT’s\b", "R D T’s"), (r"\bRDT\b", "R D T"), (r"\bSCPI\b", "S C P I"),
    (r"\bGUI\b", "G U I"), (r"\bCSV\b", "C S V"), (r"\bELF\b", "elf"), (r"\bMCU’s\b", "M C U’s"),
    (r"\bMCUs\b", "M C Us"), (r"\bMCU\b", "M C U"), (r"\bCOM\b", "com"), (r"\bTCP\b", "T C P"),
    (r"\bUDP\b", "U D P"), (r"\bLAN\b", "lan"), (r"\bJTAG\b", "J tag"), (r"\bSWD\b", "S W D"),
    (r"\bF(\d)\b", r"F \1"), (r"\bp type\b", "p-type"),
]


def say(text):
    for a, b in SAY:
        text = re.sub(a, b, text)
    return text


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def ts(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")


def kokoro():
    from kokoro_onnx import Kokoro
    model, voices = VOICE_DIR / "kokoro-v1.0.onnx", VOICE_DIR / "voices-v1.0.bin"
    if not model.is_file() or not voices.is_file():
        raise SystemExit(f"Kokoro model files missing: put kokoro-v1.0.onnx and voices-v1.0.bin in {VOICE_DIR}")
    return Kokoro(str(model), str(voices))


def chrome():
    """A headless Chromium: $CHROME, else Playwright's, else one on PATH."""
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
    found = sorted(glob.glob(f"{base}/chromium_headless_shell-*/chrome-linux/headless_shell"))
    if found:
        return found[-1]
    for name in ("chromium", "chromium-browser", "google-chrome"):
        if shutil.which(name):
            return shutil.which(name)
    raise SystemExit("No Chromium found; set CHROME to its path")
