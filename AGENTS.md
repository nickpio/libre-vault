# Testing

Always run tests through `./check.sh`. On a tiling window manager, GUI windows open on the background test workspace or a private Xvfb display. On floating desktops (macOS, Windows, and known Linux desktops such as GNOME and KDE), new windows do not take focus, so GUI tests may run on the desktop when no private display exists. Use `./check.sh --smoke` for the GUI smoke test and `./check.sh --script <path>` for standalone GUI probes, including build smoke checks. Pass unittest arguments to `./check.sh` for targeted tests.

Do not use `--foreground` or bypass `./check.sh` to run GUI tests. If `./check.sh` has no display it can use, report the limitation and any skipped GUI tests.
