"""Stable data and source locations, independent of the working directory."""
import os
import sys
from pathlib import Path

SOURCE_ROOT = str(Path(__file__).resolve().parents[1])
current_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else SOURCE_ROOT
parent_dir = os.path.dirname(current_dir)
if getattr(sys, 'frozen', False):
    try:
        os.chdir(current_dir)
    except OSError:
        pass
for directory in (current_dir, parent_dir):
    if directory not in sys.path:
        sys.path.insert(0, directory)
