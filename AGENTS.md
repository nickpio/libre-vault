# Testing

Always run tests through `./check.sh` so GUI windows open on the background test workspace or a private Xvfb display. Use `./check.sh --smoke` for the GUI smoke test and `./check.sh --script <path>` for standalone GUI probes, including build smoke checks. Pass unittest arguments to `./check.sh` for targeted tests.

Do not use `--foreground` or run GUI tests directly on the active desktop. If a background display is unavailable, report the limitation and any skipped GUI tests.
