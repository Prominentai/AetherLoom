"""Stable data and source locations, independent of the working directory."""
import os
import sys
from pathlib import Path

SOURCE_ROOT = str(Path(__file__).resolve().parents[1])
# Frozen assets live inside the bundle, separately from writable user data.
RESOURCE_ROOT = os.path.abspath(getattr(sys, '_MEIPASS', SOURCE_ROOT))


def resource_path(*parts):
    """Locate a read-only asset in the source tree or the extracted EXE bundle."""
    return os.path.join(RESOURCE_ROOT, *parts)


current_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else SOURCE_ROOT
parent_dir = os.path.dirname(current_dir)
if getattr(sys, 'frozen', False):
    try:
        os.chdir(current_dir)
    except OSError:
        pass
# Keep application modules ahead of unrelated files next to the project.
# The interpreter installation and parent directory are not resource roots.
if current_dir in sys.path:
    sys.path.remove(current_dir)
sys.path.insert(0, current_dir)
