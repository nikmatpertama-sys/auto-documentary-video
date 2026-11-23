
"""Backward-compatible entry point for the complete documentary pipeline.

The previous standalone downloader was removed because it deleted/recreated a
shared footage directory and downloaded unrelated hard-coded electricity clips.
All current footage search, filtering, verification and selection lives in main.py.
"""
from pathlib import Path
import subprocess
import sys


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    subprocess.run([sys.executable, str(root / "main.py")], cwd=root, check=True)
