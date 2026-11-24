"""D-Sync executable entry point.

PyInstaller uses this file as its packaging entry point. Absolute imports are
required here (``python -m dsync`` uses relative imports inside the package,
whereas this file is a standalone script).
"""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

# Allow running this file directly
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dsync.cli import main  # noqa: E402

if __name__ == "__main__":
    # When packaged as a windowed app, allow multiprocessing to respawn
    multiprocessing.freeze_support()
    raise SystemExit(main())
