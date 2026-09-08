"""Start AetherLoom; runtime implementation lives in aetherloom_core."""
import multiprocessing
import sys


def main():
    try:
        from aetherloom_core.application import main as run
    except (ImportError, OSError):
        from pathlib import Path
        import subprocess
        import traceback
        traceback.print_exc()
        project_dir = Path(__file__).resolve().parent
        print(f'\n[AetherLoom] Python: {sys.executable}', file=sys.stderr)
        print(f'[AetherLoom] Project: {project_dir}', file=sys.stderr)
        print('Install the project dependencies into this same interpreter:', file=sys.stderr)
        command = [sys.executable, '-m', 'pip', 'install', '-r', str(project_dir / 'requirements.txt')]
        print(subprocess.list2cmdline(command), file=sys.stderr)
        return 1
    return run()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.exit(main())
