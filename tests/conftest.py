"""Configure Qt before collection can create the first QApplication."""
import os
from pathlib import Path


# Tests are also launched from the IDE's background agent, without a visible
# desktop window. Set this before importing any test module, not in individual
# fixtures (QApplication may already exist by the time a fixture runs).
# An explicitly selected platform remains available for native UI checks.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if os.name == "nt" and os.environ.get("WINDIR"):
    os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ["WINDIR"]) / "Fonts"))
