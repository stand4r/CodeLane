"""Configure font metrics before the first offscreen Qt application starts."""
import os
from pathlib import Path


if os.name == "nt" and os.environ.get("WINDIR"):
    os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ["WINDIR"]) / "Fonts"))
