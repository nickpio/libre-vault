"""Frozen entry point. Dispatch spawned workers before importing GTK."""
import multiprocessing
import sys


if __name__ == '__main__':
    multiprocessing.freeze_support()
    if sys.argv[1:] == ['--build-smoke-test']:
        from frozen_smoke import main
    else:
        from librevault.app import main
    raise SystemExit(main())
