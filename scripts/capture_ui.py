"""Capture real GTK widgets with a disposable design-review vault.

Run through ./check.sh --script scripts/capture_ui.py [output-directory].
Use --broadway on hosts with a headless GTK build, or the usual private Xvfb.
"""
from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

import cairo
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk

from librevault.app import Window
from tests.gui_wait import wait_for_work


NOTES = {
    'Start here.md': '# A home for your ideas\n\nCollect what catches your attention. Connect the dots later.\n\n## Make it yours\n\nStart with a thought, a question, or a small observation. Your notes are always plain Markdown files.\n',
    'Studio/A slower kind of work.md': '''# A slower kind of work

Good work starts with paying attention. A few thoughts on making room for the things that matter.

## Leave a little breathing room

The best ideas rarely arrive on schedule. They show up on a walk, in the margin of a book, or in the quiet between two tasks.

Instead of filling every hour, **protect a little open space**. Give a thought somewhere to land before deciding what it needs to become.

## Small rituals, lasting momentum

- Begin the day with a page, before opening your inbox.
- Keep a running list of questions worth returning to.
- End with one small thing to pick up tomorrow.

> [!tip] A gentle reminder
> Consistency is a rhythm, not a streak. Missing a day is part of the practice.

## Follow the threads

This connects to [[Studio/Attention as a practice|Attention as a practice]] and the notes from [[Reading/The Creative Act|The Creative Act]]. There is something useful in the overlap.

---

What would a good day feel like if you measured it in attention, rather than output?
''',
    'Studio/Attention as a practice.md': '# Attention as a practice\n\nNotice first. Name it later.\n\n## A small experiment\n\nSpend ten minutes with one thing. No second screen, no timer to race.\n\nSee [[Studio/A slower kind of work]].\n',
    'Studio/Ideas for a rainy day.md': '# Ideas for a rainy day\n\n- Visit the secondhand bookshop\n- Sketch the view from the kitchen\n- Revisit [[Studio/A slower kind of work]]\n',
    'Reading/The Creative Act.md': '# The Creative Act\n\nA few passages about receptivity, practice, and noticing.\n\nSee [[Studio/Attention as a practice]].\n',
    'Reading/A field guide to noticing.md': '# A field guide to noticing\n\nLook for the small changes.\n',
    'Journal/Friday, November 6.md': '# Friday, November 6\n\nThe light came in slowly this morning.\n\n## Today\n\n- [ ] Make time for a walk\n- [x] Write a page\n',
}


def settle(seconds=0.4):
    deadline = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        for _ in range(100):
            if not context.pending():
                break
            context.iteration(False)
        time.sleep(0.01)


def capture(widget, path):
    settle()
    width, height = widget.get_allocated_width(), widget.get_allocated_height()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    widget.draw(cairo.Context(surface))
    surface.write_to_png(str(path))
    print(f'{path}: {width}x{height}', flush=True)


def main():
    if not Gtk.init_check()[0]:
        raise SystemExit('An isolated GTK display is required; run through ./check.sh.')
    output = Path(sys.argv[1] if len(sys.argv) > 1 else 'artifacts/ui-overhaul').resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = Gtk.Application(application_id='org.librevault.DesignReview')
    app.register(None)
    with TemporaryDirectory(prefix='librevault-design-') as directory:
        root = Path(directory) / 'Commonplace'
        root.mkdir()
        for name, text in NOTES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        window = Window(app, preferences_path=Path(directory) / 'preferences.json')
        window.resize(1440, 980)
        wait_for_work(window)
        capture(window, output / 'welcome-dark.png')
        window.load_vault(root)
        wait_for_work(window)
        window.open_note('Reading/The Creative Act.md')
        window.open_note('Studio/A slower kind of work.md')
        window.bookmark_current_note()
        settle()
        pane = window.current()
        pane.buffer.place_cursor(pane.buffer.get_end_iter())
        pane.editor.grab_focus()
        pane.scroll.get_vadjustment().set_value(0)
        capture(window, output / 'workspace-dark.png')
        window.preferences = replace(window.preferences, theme='light')
        window.apply_preferences()
        capture(window, output / 'workspace-light.png')
        pane.set_mode('reading')
        capture(window, output / 'reading-light.png')
        pane.set_mode('live')
        window.show_command_palette()
        capture(window, output / 'commands-light.png')
        window.palette.destroy()
        window.show_settings()
        capture(window.settings_dialog, output / 'settings-light.png')
        window.settings_dialog.destroy()
        window.preferences = replace(window.preferences, theme='lights_out')
        window.apply_preferences()
        capture(window, output / 'workspace-lights-out.png')
        window.preferences = replace(window.preferences, theme='light')
        window.apply_preferences()
        window.resize(1000, 760)
        capture(window, output / 'compact-light.png')
        window.destroy()
        settle(0.1)


if __name__ == '__main__':
    main()
