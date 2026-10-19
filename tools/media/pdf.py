"""Join the rendered slides into the presentation PDF, in deck order (INS-16).
python tools/media/pdf.py [OUT.pdf]  (default docs/media/rdt-presentation.pdf)"""
import json, sys
from pathlib import Path

from PIL import Image

from common import ROOT, WORK

out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs/media/rdt-presentation.pdf"
order = json.loads((WORK / "render/notes.json").read_text())["order"]
pages = [Image.open(WORK / "render/png" / f"{sid}.png").convert("RGB") for sid in order]
pages[0].save(out, save_all=True, append_images=pages[1:], resolution=96)
print(out, len(pages), "pages")
