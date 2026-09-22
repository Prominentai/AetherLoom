"""
Build script to package the AetherLoom app using Python + PyInstaller.
- Uses the current Python interpreter by default; override with PACK_PYTHON.
- Produces a windowed (no console) one-file executable with icon `app_icon.ico`.
- Assembles the EXE and external resources into `packaging_build/product/`.
- Build intermediates and logs stay under `packaging_build/`.
- Excludes heavyweight AI libraries and audits the finished EXE before release.
- Missing application dependencies are reported, never auto-installed from error text.

Usage (from project root):
    python packaging_build/build_package.py

Note: run this from Windows. A failed build or dependency audit preserves the previous product.
"""

import os
import sys
import subprocess
import tempfile
import time
import re
from pathlib import Path
if __package__:
    from .release_product import assemble_product, product_resources, bundled_icon_resources, check_product_destination
    from .bundle_policy import exclusion_options
else:
    from release_product import assemble_product, product_resources, bundled_icon_resources, check_product_destination
    from bundle_policy import exclusion_options

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
MAX_RETRIES = 1
# Only distributable runtime resources belong in the executable. User settings,
# API keys, caches, backups, test files, and local environments are never inputs.
RUNTIME_FILES = (
    'AetherLoom.py', 'Duck_Dec_local.py',
    'get_apps.py',
    'README.md', 'autocomplete.txt',
    'requirements.txt', 'THIRD_PARTY_NOTICES.txt',
)
CORE_MODULE_FILES = (
    'rh_model_apps.py', 'rh_model_runtime.py', 'rh_model_errors.py', 'rh_model_app_ui.py', 'rh_llm_execution.py',
    'rh_multi_inputs.py', 'rh_multi_input_ui.py',
    'rh_standard_catalog.json', 'RH_MODEL_CATALOG_LICENSE.txt',
    '__init__.py', 'api_credentials.py', 'api_manager.py', 'api_manager_ui.py',
    'api_model_capabilities.py', 'api_model_probe.py', 'decode_browser.py',
    'local_browser_ui.py', 'local_media.py', 'local_preview.py', 'media_limits.py',
    'hover_preview.py', 'image_input_preview.py', 'video_compat.py', 'video_preview.py',
    'prompt_history.py', 'selection_text_tools.py', 'rh_outputs.py', 'rh_parameters.py', 'rh_result_actions.py',
    'rh_storage.py', 'rh_submission_queue.py', 'rh_tasks.py', 'rh_ui.py',
    'thumbnail_resources.py', 'autocomplete.py', 'application.py',
    'paths.py', 'resources.py', 'platform_utils.py',
    'ui/__init__.py', 'ui/widgets.py', 'ui/compare.py', 'ui/main_window.py',
    'ui/layout.py', 'ui/presentation.py', 'ui/menus.py', 'ui/local_browser.py', 'ui/settings.py', 'ui/preferences.py', 'ui/home.py', 'ui/decode.py',
    'rh_progress.py', 'rh_dashboard.py', 'ui/design.py', 'ui/popups.py', 'ui/responsive.py', 'ui/navigation.py', 'ui/themed_icons.py', 'tasks/__init__.py', 'tasks/media.py', 'tasks/decoding.py',
    'services/__init__.py', 'services/decoding.py',
    'rh_execution.py', 'rh_execution_ui.py', 'rh_output_groups.py', 'rh_connections.py', 'rh_connection_panel.py', 'rh_app_install.py',
    'rh_app_add_dialog.py', 'rh_app_reference.py', 'rh_app_thumbnails.py',
    'task_documents.py', 'rh_task_details.py', 'rh_model_picker.py',
    'rh_model_cards.py', 'rh_model_style.py', 'rh_model_thumbnails.py',
    'rh_model_favorites.py', 'rh_model_favorite_editor.py', 'rh_model_library.py',
    'rh_model_covers.py', 'rh_model_import.py', 'rh_model_import_ui.py', 'rh_model_browser.py', 'rh_model_browser_storage.py',
    'rh_model_bases.py', 'rh_model_http.py', 'rh_model_dialogs.py',
    'api_provider_editor.py', 'api_model_selector.py', 'translation.py',
    'agent_catalog.py', 'agent_auth.py', 'agent_client.py', 'agent_ui.py', 'agent_search.py', 'image_prompts.py',
    'image_model_catalog.py', 'mask_editor.py', 'mask_history.py', 'mask_assets.py', 'mask_canvas.py', 'mask_panel.py', 'image_import.py', 'media_import.py',
    'canvas/__init__.py', 'canvas/model.py', 'canvas/storage.py', 'canvas/engine.py',
    'canvas/model_nodes.py', 'canvas/model_editor.py',
    'canvas/collections.py', 'canvas/collection_editor.py',
    'canvas/utility_nodes.py', 'canvas/advanced_nodes.py', 'canvas/utility_editor.py',
    'canvas/bounding_nodes.py', 'canvas/image_processing_nodes.py', 'canvas/mask_region_nodes.py', 'canvas/tile_nodes.py',
    'canvas/prompt_nodes.py', 'canvas/prompt_editor.py', 'canvas/text_files.py', 'canvas/text_file_ui.py',
    'canvas/input_requirements.py', 'canvas/dependencies.py', 'canvas/mask_processing.py',
    'canvas/image_compare.py', 'canvas/image_input_ui.py', 'canvas/manual_selection.py',
    'canvas/subgraphs.py', 'canvas/video_nodes.py', 'canvas/workflow_library.py',
    'canvas/preview_data.py', 'canvas/result_browser.py',
    'canvas/save_results.py',
    'canvas/media_inputs.py',
    'canvas/file_nodes.py',
    'canvas/cache_cleanup.py',
    'canvas/run_outputs.py', 'canvas/inline_text.py', 'canvas/inline_controls.py', 'canvas/node_form.py',
    'canvas/selection.py',
    'canvas/preferences.py', 'canvas/page_preferences.py', 'canvas/workspace_tools.py',
    'canvas/graphics.py', 'canvas/appearance.py', 'canvas/controls.py', 'canvas/editors.py', 'canvas/page.py', 'canvas/workflow_queue.py', 'canvas/workflow_queue_panel.py',
)
API_MODULE_FILES = (
    '__init__.py', 'call_llm.py', 'call_rh.py', 'call_translate.py',
    'call_vision.py', 'call_images.py', 'translators.py', 'provider_client.py',
)

PYEXE = os.environ.get('PACK_PYTHON', DEFAULT_PYTHON)

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
    for path, relative in bundled_icon_resources(root):
        entries.append(f"{path};{relative.parent.as_posix()}")
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


def audit_built_bundle(python_exe, executable):
    """Use the build interpreter's PyInstaller to inspect the actual EXE archive."""
    command = [str(python_exe), '-B', str(Path(__file__).with_name('bundle_policy.py')),
               str(executable)]
    try:
        audit_env = dict(os.environ, PYTHONIOENCODING='utf-8')
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding='utf-8', errors='replace', timeout=120, env=audit_env)
        output = result.stdout or ''
        print(output, end='' if output.endswith('\n') else '\n')
        with open(LOG_FILE, 'a', encoding='utf-8') as logf:
            logf.write('\n-- Bundle dependency audit --\n' + output)
        if result.returncode != 0:
            print('Bundle audit rejected this build. Product will not be replaced.')
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        print('Cannot verify bundle dependencies; refusing to publish:', exc)
        return False


# Run the build with a fixed exclusion policy and a mandatory artifact audit.

def run_packaging(python_exe, attempts=MAX_RETRIES, output_dir=None):
    output_dir = Path(output_dir) if output_dir is not None else DIST_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    data_entries = gather_data_entries(PROJECT_ROOT)
    # windowed / no console
    pyinst_opts = [
        "--noconsole",
        "--onefile",
        "--noconfirm",
        "--hidden-import=Grid_Reversal_Dec_local",
        # imageio reads its distribution version while importing MoviePy.
        "--copy-metadata=imageio",
        f"--icon={str(ICON_FILE) if ICON_FILE.exists() else ''}",
        f"--distpath={str(output_dir)}",
        f"--workpath={str(BUILD_DIR)}",
        f"--specpath={str(SPEC_DIR)}",
        # ensure local imports are found
        f"--paths={str(PROJECT_ROOT)}",
    ]
    pyinst_opts.extend(exclusion_options())
    add_data_opts = []
    for e in data_entries:
        add_data_opts.extend(["--add-data", e])


    # command will be written to log after we choose the python executable

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
                same_exec = Path(python_for_pyi).resolve() == Path(sys.executable).resolve()
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
                ret = 0 if (output_dir / 'AetherLoom.exe').is_file() else 1
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
        except SystemExit as e:
            ret = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            print('PyInstaller exited with:', ret)
        except Exception as e:
            print('Exception while running PyInstaller programmatically or subprocess:', e)
            ret = 1
        if ret == 0 and not (output_dir / 'AetherLoom.exe').is_file():
            print('Build completed without producing AetherLoom.exe.')
            ret = 1
        if ret == 0:
            if not audit_built_bundle(python_for_pyi, output_dir / 'AetherLoom.exe'):
                return False
            print("Packaging and dependency audit succeeded.")
            return True

        print(f"Packaging failed on attempt {attempt} (exit {ret}).")
        if missing_modules:
            print('Missing modules:', ', '.join(sorted(missing_modules)))
            print('Application dependencies will not be installed automatically. '
                  'Check requirements.txt and the bundle exclusion policy.')
        print('Build stopped. See log at:', LOG_FILE)
        return False

    print("Packaging failed after attempts. See log for details:", str(LOG_FILE))
    return False


def main():
    print('Project root:', PROJECT_ROOT)
    print('Release product:', PACK_DIR / 'product')
    if not ENTRY_SCRIPT.is_file():
        print('Entry script not found:', ENTRY_SCRIPT)
        return 2
    try:
        # Validate external resources before installing tools or starting a build.
        product_resources(PROJECT_ROOT)
        bundled_icon_resources(PROJECT_ROOT)
        for name in ('Grid_Reversal_Dec_local.py', 'aetherloom_core/rh_standard_catalog.json'):
            if not (PROJECT_ROOT / name).is_file():
                raise FileNotFoundError(f'Missing bundled module/resource: {PROJECT_ROOT / name}')
        check_product_destination(PROJECT_ROOT)
        for directory in (PACK_DIR, DIST_DIR, BUILD_DIR, SPEC_DIR):
            directory.resolve().relative_to(PROJECT_ROOT.resolve())
            directory.mkdir(parents=True, exist_ok=True)
        if not ensure_pyinstaller(PYEXE):
            print('PyInstaller unavailable. Exiting.')
            return 3
        # An old EXE in dist must never be mistaken for this build's output.
        with tempfile.TemporaryDirectory(prefix='release-dist-', dir=BUILD_DIR) as staging:
            if not run_packaging(PYEXE, output_dir=Path(staging)):
                print('Packaging failed. See:', LOG_FILE)
                return 4
            product = assemble_product(PROJECT_ROOT, Path(staging) / 'AetherLoom.exe')
        print('Done. Distribute the whole directory:', product)
        return 0
    except (OSError, ValueError) as exc:
        print('Release packaging failed:', exc)
        return 4


if __name__ == '__main__':
    sys.exit(main())
