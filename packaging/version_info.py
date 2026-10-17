"""Write the Windows file properties (version, product name) PyInstaller puts into the exes.

    python packaging/version_info.py 1.2.0 build/version_info.txt
"""

import sys
from pathlib import Path

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Rani Feldman'),
      StringStruct('FileDescription', 'RDT - Remote Debugger Tool'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'rdt'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 Rani Feldman, MIT licence'),
      StringStruct('ProductName', 'RDT'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [0x409, 1200])])
  ]
)
"""


def main(version: str, out: str) -> None:
    parts = [int(p) for p in version.split(".")][:4]
    t = tuple(parts + [0] * (4 - len(parts)))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(TEMPLATE.format(t=t, v=version), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
