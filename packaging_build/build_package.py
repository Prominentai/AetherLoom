"""
Build script to package the AetherLoom app using Python + PyInstaller.
- Uses the current Python interpreter by default; override with PACK_PYTHON.
- Produces a windowed (no console) one-file executable with icon `app_icon.ico`.
- Places build outputs, logs, and specs under `packaging_build/`.
- Tries to auto-install PyInstaller and retry on ModuleNotFoundError if possible.

Usage (from project root):
    python packaging_build/build_package.py

Note: run this from Windows. The script streams PyInstaller output and attempts up to 3 retries.
"""

import os
import sys
import subprocess
import shutil
import time
import re
from pathlib import Path

# === User-editable defaults ===
DEFAULT_PYTHON = sys.executable
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACK_DIR = PROJECT_ROOT / 'packaging_build'
DIST_DIR = PACK_DIR / 'dist'
BUILD_DIR = PACK_DIR / 'build'
SPEC_DIR = PACK_DIR / 'specs'
LOG_FILE = PACK_DIR / 'pack_build.log'
ENTRY_SCRIPT = PROJECT_ROOT / 'AetherLoom.py'
ICON_FILE = PROJECT_ROOT / 'app_icon.ico'
MAX_RETRIES = 3
# Only distributable runtime resources belong in the executable. User settings,
# API keys, caches, backups, test files, and local environments are never inputs.
RUNTIME_FILES = (
    'AetherLoom.py', 'Duck_Dec_local.py', 'Grid_Reversal_Dec_local.py',
    'get_apps.py',
    'app_icon.ico', 'app_icon.png', 'README.md', 'autocomplete.txt',
    'requirements.txt',
)
CORE_MODULE_FILES = (
    '__init__.py', 'api_credentials.py', 'api_manager.py', 'api_manager_ui.py',
    'api_model_capabilities.py', 'api_model_probe.py', 'decode_browser.py',
    'local_browser_ui.py', 'local_media.py', 'local_preview.py', 'media_limits.py',
    'prompt_history.py', 'rh_outputs.py', 'rh_parameters.py', 'rh_result_actions.py',
    'rh_storage.py', 'rh_submission_queue.py', 'rh_tasks.py', 'rh_ui.py',
    'thumbnail_resources.py', 'autocomplete.py', 'application.py',
    'paths.py', 'resources.py', 'platform_utils.py',
    'ui/__init__.py', 'ui/widgets.py', 'ui/compare.py', 'ui/main_window.py',
    'ui/layout.py', 'ui/presentation.py', 'ui/menus.py', 'ui/local_browser.py', 'ui/settings.py', 'ui/preferences.py', 'ui/home.py', 'ui/decode.py',
    'rh_progress.py', 'rh_dashboard.py', 'ui/responsive.py', 'tasks/__init__.py', 'tasks/media.py', 'tasks/decoding.py',
    'services/__init__.py', 'services/decoding.py',
    'rh_execution.py', 'rh_execution_ui.py', 'rh_output_groups.py', 'rh_connections.py', 'rh_app_install.py',
    'task_documents.py', 'rh_task_details.py',
    'canvas/__init__.py', 'canvas/model.py', 'canvas/storage.py', 'canvas/engine.py',
    'canvas/graphics.py', 'canvas/editors.py', 'canvas/page.py', 'canvas/workflow_queue.py', 'canvas/workflow_queue_panel.py',
)
API_MODULE_FILES = (
    '__init__.py', 'call_llm.py', 'call_rh.py', 'call_translate.py',
    'call_vision.py', 'translators.py', 'provider_client.py',
)
ICON_SUFFIXES = {'.svg', '.png', '.ico', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}

# Helpful: make sure directories exist
for p in (PACK_DIR, DIST_DIR, BUILD_DIR, SPEC_DIR):
    p.mkdir(parents=True, exist_ok=True)

PYEXE = DEFAULT_PYTHON
# allow override via env var
PYEXE = os.environ.get('PACK_PYTHON', PYEXE)

if not Path(PYEXE).exists():
    print(f"Warning: specified Python executable not found: {PYEXE}")
    print("You can set PACK_PYTHON env var to a valid python.exe path.")

if not ENTRY_SCRIPT.exists():
    print(f"Error: entry script not found: {ENTRY_SCRIPT}")
    sys.exit(2)

# Build PyInstaller base command generator

def gather_data_entries(root: Path):
    """Collect allowlisted runtime files as Windows PyInstaller src;dest entries."""
    root = Path(root)
    entries = []
    for name in RUNTIME_FILES:
        path = root / name
        if path.is_file():
            entries.append(f"{path};.")
    for name in API_MODULE_FILES:
        path = root / 'api_calls' / name
        if path.is_file():
            entries.append(f"{path};api_calls")
    for name in CORE_MODULE_FILES:
        path = root / 'aetherloom_core' / name
        if path.is_file():
            destination = (Path('aetherloom_core') / Path(name).parent).as_posix()
            entries.append(f"{path};{destination}")
    icon_dir = root / 'icons'
    if icon_dir.is_dir():
        for path in sorted(icon_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in ICON_SUFFIXES:
                entries.append(f"{path};icons")
    return entries


def ensure_pyinstaller(python_exe):
    """Ensure PyInstaller is importable under the given python; install if necessary."""
    print("Checking PyInstaller availability...")
    try:
        out = subprocess.check_output([python_exe, "-m", "PyInstaller", "--version"], stderr=subprocess.STDOUT, text=True)
        print("PyInstaller available:", out.strip())
        return True
    except subprocess.CalledProcessError:
        pass
    except FileNotFoundError:
        print("Python executable not found. Aborting.")
        return False

    print("PyInstaller not found. Attempting to install via pip (this may take a few minutes)...")
    try:
        subprocess.check_call([python_exe, "-m", "pip", "install", "--upgrade", "pyinstaller"], stderr=subprocess.STDOUT)
        print("PyInstaller installed.")
        return True
    except Exception as e:
        print("Failed to install PyInstaller:", e)
        return False


# Run the packaging with retries and simple auto-fix for ModuleNotFoundError

def run_packaging(python_exe, attempts=MAX_RETRIES):
    data_entries = gather_data_entries(PROJECT_ROOT)
    # windowed / no console
    pyinst_opts = [
        "--noconsole",
        "--onefile",
        f"--icon={str(ICON_FILE) if ICON_FILE.exists() else ''}",
        f"--distpath={str(DIST_DIR)}",
        f"--workpath={str(BUILD_DIR)}",
        f"--specpath={str(SPEC_DIR)}",
        # ensure local imports are found
        f"--paths={str(PROJECT_ROOT)}",
    ]
    add_data_opts = []
    for e in data_entries:
        add_data_opts.extend(["--add-data", e])


    # command will be written to log after we choose the python executable

    last_err = None
    # If the current interpreter environment contains launcher-zip entries,
    # create an isolated venv to run PyInstaller to avoid referencing launcher zips.
    try:
        import sys as _sys
        launcher_present = any('.launcher' in (p or '') for p in _sys.path)
    except Exception:
        launcher_present = False

    venv_dir = PACK_DIR / 'venv_build'
    venv_python = None
    if launcher_present:
        try:
            print('Detected launcher-style Python environment; creating isolated venv...')
            if not venv_dir.exists():
                try:
                    subprocess.check_call([python_exe, '-m', 'venv', str(venv_dir)])
                except Exception:
                    # fallback: try virtualenv if venv module is not available
                    try:
                        subprocess.check_call([python_exe, '-m', 'pip', 'install', '--upgrade', 'virtualenv'])
                        subprocess.check_call([python_exe, '-m', 'virtualenv', str(venv_dir)])
                    except Exception as _e:
                        raise
            # venv python path
            venv_python = venv_dir / 'Scripts' / 'python.exe'
            if not venv_python.exists():
                venv_python = venv_dir / 'bin' / 'python'
            if venv_python.exists():
                # ensure pip and pyinstaller are available inside venv
                try:
                    subprocess.check_call([str(venv_python), '-m', 'pip', 'install', '--upgrade', 'pip'])
                    subprocess.check_call([str(venv_python), '-m', 'pip', 'install', 'pyinstaller', 'packaging'])
                    print('Isolated venv prepared at', venv_dir)
                except Exception as e:
                    print('Failed to prepare isolated venv:', e)
                    venv_python = None
            else:
                print('Could not locate venv python executable; continuing without venv')
                venv_python = None
        except Exception as e:
            print('Error creating venv:', e)
            venv_python = None
    # choose python executable to run PyInstaller (prefer isolated venv if prepared)
    python_for_pyi = str(venv_python) if venv_python else python_exe
    base_cmd = [python_for_pyi, "-m", "PyInstaller"]
    cmd = base_cmd + pyinst_opts + add_data_opts + [str(ENTRY_SCRIPT)]
    # write full command to log
    with open(LOG_FILE, 'a', encoding='utf-8') as logf:
        logf.write(f"\n=== Packaging run at {time.ctime()} ===\n")
        logf.write('COMMAND: ' + ' '.join(cmd) + '\n')

    for attempt in range(1, attempts + 1):
        print(f"Packaging attempt {attempt}/{attempts}...")
        with open(LOG_FILE, 'a', encoding='utf-8') as logf:
            logf.write(f"\n-- Attempt {attempt} --\n")
        # Prepare a filtered PYTHONPATH for the PyInstaller subprocess to avoid
        # pulling in launcher-zip paths that may reference missing files.
        env = os.environ.copy()
        try:
            import sys as _sys
            filtered = [p for p in _sys.path if p and os.path.exists(p) and '.launcher' not in p and not p.lower().endswith('.zip')]
            # ensure project root is first
            if str(PROJECT_ROOT) not in filtered:
                filtered.insert(0, str(PROJECT_ROOT))
            env['PYTHONPATH'] = os.pathsep.join(filtered)
        except Exception:
            env = os.environ.copy()

        missing_modules = set()
        # If we're invoking the same python that's running this script, call
        # PyInstaller programmatically after temporarily filtering sys.path to
        # avoid launcher zip entries which reference missing files.
        try:
            same_exec = False
            try:
                same_exec = Path(python_exe).resolve() == Path(sys.executable).resolve()
            except Exception:
                same_exec = False
            if same_exec:
                # build arg list for PyInstaller.__main__.run
                arglist = []
                arglist.extend(pyinst_opts)
                for e in data_entries:
                    arglist.extend(["--add-data", e])
                arglist.append(str(ENTRY_SCRIPT))
                # temporarily filter sys.path
                old_path = sys.path[:]
                try:
                    # exclude only launcher-specific entries; keep stdlib zip entries
                    sys.path = [p for p in sys.path if p and '.launcher' not in p]
                    if str(PROJECT_ROOT) not in sys.path:
                        sys.path.insert(0, str(PROJECT_ROOT))
                    import PyInstaller.__main__ as _pyi_main
                    # capture PyInstaller output by running it in the same process
                    _pyi_main.run(arglist)
                finally:
                    sys.path = old_path
                # we cannot easily parse streamed output here; assume PyInstaller
                # wrote warnings and errors to files; check for warn file
                warn_file = BUILD_DIR / 'AetherLoom' / 'warn-AetherLoom.txt'
                if warn_file.exists():
                    with open(warn_file, 'r', encoding='utf-8', errors='ignore') as wf:
                        for line in wf:
                            print(line, end='')
                            m = re.search(r"ModuleNotFoundError: No module named '([\w_.-]+)'", line)
                            if m:
                                missing_modules.add(m.group(1))
                ret = 0 if (DIST_DIR.exists() and any(DIST_DIR.iterdir())) else 1
            else:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, text=True, env=env)
                with proc.stdout:
                    for line in proc.stdout:
                        print(line, end='')
                        with open(LOG_FILE, 'a', encoding='utf-8') as logf:
                            logf.write(line)
                        # detect ModuleNotFoundError
                        m = re.search(r"ModuleNotFoundError: No module named '([\w_.-]+)'", line)
                        if m:
                            missing_modules.add(m.group(1))
                ret = proc.wait()
        except Exception as e:
            print('Exception while running PyInstaller programmatically or subprocess:', e)
            ret = 1
        if ret == 0:
            print("Packaging succeeded.")
            return True

        print(f"Packaging failed on attempt {attempt} (exit {ret}).")
        last_err = ret

        # try auto-fix: install missing modules
        if missing_modules:
            print("Detected missing modules:", ', '.join(missing_modules))
            installed_any = False
            # special-case known pseudo-module packaging.licenses -> install packaging
            if 'packaging.licenses' in missing_modules:
                try:
                    print("Installing 'packaging' package to satisfy packaging.licenses...")
                    subprocess.check_call([python_exe, "-m", "pip", "install", "--upgrade", "packaging"], stderr=subprocess.STDOUT)
                    installed_any = True
                except Exception as e:
                    print("Failed to install 'packaging':", e)
            for mod in missing_modules:
                if mod == 'packaging.licenses':
                    continue
                try:
                    print(f"Attempting to install missing module: {mod}")
                    subprocess.check_call([python_exe, "-m", "pip", "install", mod], stderr=subprocess.STDOUT)
                    installed_any = True
                except Exception as e:
                    print(f"Failed to install {mod}: {e}")
            if installed_any:
                print("Re-running packaging after installing missing modules...")
                time.sleep(2)
                continue

        # If PyInstaller missing or other error, try to (re)install PyInstaller
        if not ensure_pyinstaller(python_exe):
            print("Could not ensure PyInstaller. Aborting further retries.")
            break

        # no automatic resolution; break and let user inspect logs
        print("No automatic fix detected. See log at:", str(LOG_FILE))
        break

    print("Packaging failed after attempts. See log for details:", str(LOG_FILE))
    return False


if __name__ == '__main__':
    print("Project root:", PROJECT_ROOT)
    print("Output dist:", DIST_DIR)
    ok = ensure_pyinstaller(PYEXE)
    if not ok:
        print("PyInstaller unavailable. Exiting.")
        sys.exit(3)
    success = run_packaging(PYEXE)
    if success:
        print('\nDone. Built artifacts are in: ', DIST_DIR)
        # list outputs
        for f in DIST_DIR.iterdir():
            print(' -', f.name)
        sys.exit(0)
    else:
        print('\nPackaging did not complete successfully. Check', LOG_FILE)
        sys.exit(4)
