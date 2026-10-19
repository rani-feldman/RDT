"""Render each deck slide to a 1920x1080 PNG with headless Chromium, and save its speaker notes."""
"""python tools/media/render.py  -> WORK/render/png/<slide>.png and WORK/render/notes.json
The slides use the GUI stills from capture.py (WORK/cap/stills)."""
import json, re, subprocess

from common import HERE, WORK, chrome

DECK = HERE / "deck"
OUT = WORK / "render"
STILLS = WORK / "cap/stills"
BLOBS = {
    "6f28d5835225bdc5a645e4f6a8490ae0": "chart", "874516ae613668bf60a2eaa8bedea531": "console",
    "f4607d22c7a6256ba5317f3388d69b4b": "disconnected", "d7207b58c356d13a5f09f32425420ae9": "dlg-item",
    "d378af7061fad79c03c1d5c0f82e2e2c": "dlg-mcus", "e69390c381e0fca5f83e95d3609d4999": "dlg-profile",
    "3461a275b25c91156f1cf6129a5ccd26": "dlg-shortcuts", "502adfad3cab6147b28119f803e3e8ae": "memory",
    "ef9490600b48e36a0b0c9a4190228906": "mismatch", "0efe740cbb1bbd6c044ce8c8ed978424": "structures",
    "945e8cfd2e9c4109aba9ef3cc307b121": "theme-instrument", "a4c675d21679d21431c86a96f76f780c": "theme-islands",
    "6fb46b2a5083b1dc7ffb1fbd93ea5ea9": "theme-light", "048273a4ae1e4c09bbb81ce3aef94c66": "theme-light_pro",
    "b3a9fa0f6e90319ecef7aa49220d5ea6": "theme-studio", "ad64fae9dd4161788ab2822f64d20585": "update-available",
    "cdaea4a9897aec81681e0bb0d500cbf1": "watch", "037fd3e4273beaf9783b720743e8cab5": "write",
}
CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
html, body { width:1920px; height:1080px; overflow:hidden; background:#000; }
section { width:1920px; height:1080px; position:relative; overflow:hidden; }
section > *, div > * { min-width:0; }
ul, ol { padding-left:1.3em; }
li { margin-bottom:0.15em; }
table { border-collapse:collapse; width:100%; }
th, td { padding:0.35em 0.6em; text-align:left; vertical-align:top; border-bottom:1px solid #D3D8D2; }
th { font-weight:600; border-bottom:2px solid #9AA3AD; }
x-icon { display:block; background:#1F8A5F; border-radius:12px; height:10px !important; width:72px !important; }
aside { display:none; }
p { white-space:pre-wrap; }
"""


def main():
    order = json.loads((DECK / "deck.json").read_text())["order"]
    fonts = ""  # IBM Plex Sans and JetBrains Mono come from the installed fonts
    (OUT / "png").mkdir(parents=True, exist_ok=True)
    notes = {}
    for sid in order:
        html = (DECK / "slides" / f"{sid}.html").read_text()
        m = re.search(r"<aside>(.*?)</aside>", html, re.S)
        notes[sid] = m.group(1).strip() if m else ""
        for blob, name in BLOBS.items():
            html = html.replace(f"/_blob/{blob}", f"file://{STILLS}/{name}.png")
        page = OUT / "page.html"
        page.write_text(f"<!doctype html><html><head><meta charset='utf-8'><style>{fonts}\n{CSS}</style>"
                        f"</head><body>{html}</body></html>")
        png = OUT / "png" / f"{sid}.png"
        subprocess.run([chrome(), "--no-sandbox", "--hide-scrollbars",
                        "--allow-file-access-from-files", "--force-device-scale-factor=1",
                        "--window-size=1920,1080", "--virtual-time-budget=3000",
                        f"--screenshot={png}", f"file://{page}"], check=True, capture_output=True)
        print(sid)
    (OUT / "notes.json").write_text(json.dumps({"order": order, "notes": notes}, indent=1))


if __name__ == "__main__":
    main()
