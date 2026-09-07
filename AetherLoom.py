"""Start AetherLoom; runtime implementation lives in aetherloom_core."""
import multiprocessing


def main():
    from aetherloom_core.application import main as run
    return run()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
