
"""Compatibility entry point. The complete pipeline is maintained in main.py."""
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    subprocess.run([sys.executable, str(root / "main.py")], cwd=root, check=True)
