"""RDT's version: the one place it is written. A release sets it (tools/release.py) and the git tag
``vX.Y.Z`` names the same version; pyproject.toml, the exe file properties, Help > About and
``rdt --version`` all read it from here."""

__version__ = "1.1.0"
