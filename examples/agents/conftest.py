"""Make the example agent packages importable. The Zetra pytest plugin is registered by the package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
