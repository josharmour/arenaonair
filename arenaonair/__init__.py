"""Allow ``python -m arenaonair.app`` directly from a source checkout.

Installed distributions use src/arenaonair; this shim is not packaged.
"""
from pathlib import Path

__path__.append(str(Path(__file__).resolve().parent.parent / "src" / "arenaonair"))
__version__ = "0.1.0"
