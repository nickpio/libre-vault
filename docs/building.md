# Building

For development, run directly from the checkout using the [installation instructions](../README.md#installation). A build is only needed for a standalone executable or AppImage.

## Standalone executable

On Linux, install the runtime dependencies, Python's `venv` and `pip` tools, and `binutils` from your distribution. Include the PyGObject Cairo bridge (`python3-gi-cairo` on Debian/Ubuntu or `python-cairo` on Arch). Debian/Ubuntu also need `python3-venv`.

From the checkout root:

```sh
python3 -m venv --system-site-packages .venv-build
.venv-build/bin/python -m pip install -r requirements-build.txt
PYTHON=.venv-build/bin/python ./build.sh
```

The output is `dist/libre-vault`, containing Python, GTK, Python dependencies, and application resources. Poppler PDF previews are bundled when its introspection bindings are installed at build time.

```sh
./dist/libre-vault
./dist/libre-vault /path/to/vault
```

The executable can be moved outside the checkout and uses the same settings and storage locations as running from source. PyInstaller extracts the bundle to a temporary directory on startup, which must allow executable files.

For Windows and macOS build environments and output paths, see [Desktop platforms](platforms.md#native-builds-and-checks).

## Linux AppImage

Install `appimagetool` separately, then run:

```sh
PYTHON=.venv-build/bin/python ./build.sh --appimage --appimagetool /path/to/appimagetool
```

If `appimagetool` is on `PATH`, omit `--appimagetool`. The output is `dist/LibreVault-<architecture>.AppImage`. The build does not download packaging tools automatically. AppImage runtimes may need FUSE; supported runtimes also offer `--appimage-extract-and-run` on systems without it.

Build on the oldest Linux distribution you intend to support, using the target CPU architecture. AppImage packaging does not remove the build system's glibc and native-library compatibility requirements. Cross-distribution validation, reproducible builds, and a complete dependency license inventory remain [release work](roadmap.md).

## Verify a build

Always run GUI build checks through the isolated test runner:

```sh
./check.sh --script scripts/check_build.py
./check.sh --script scripts/check_build.py dist/LibreVault-x86_64.AppImage
```

The check copies the artifact outside the checkout, clears `PYTHONPATH`, and exercises GTK startup, editing and saving, YAML aliases, Cairo, math rendering, image loading, and a spawned graph worker. It requires a background Hyprland workspace or private Xvfb display; do not run it on the active desktop.
