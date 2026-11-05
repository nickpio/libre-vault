"""Run with ./check.sh --script scripts/check_build.py [dist/libre-vault]."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


def main():
    root = Path(__file__).resolve().parents[1]
    executable = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / 'dist' / ('libre-vault.exe' if sys.platform == 'win32' else 'libre-vault')
    with TemporaryDirectory(prefix='librevault-artifact-') as directory:
        copy = Path(directory) / executable.name
        shutil.copy2(executable, copy)
        env = dict(os.environ, XDG_CACHE_HOME=str(Path(directory) / 'cache'))
        env.pop('PYTHONPATH', None)
        return subprocess.run([str(copy), '--build-smoke-test'], cwd=directory,
                              env=env, timeout=120).returncode


if __name__ == '__main__':
    raise SystemExit(main())
