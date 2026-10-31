"""Exercise GTK pointer hit-testing in a child process, since failures abort."""
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TextHitTestingTests(unittest.TestCase):
    def test_hidden_syntax_survives_hit_testing_and_layout_changes(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), '--probe'],
            cwd=ROOT, capture_output=True, text=True, timeout=20)
        if result.returncode == 77:
            self.skipTest('GTK 3 and a display are required')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Hit-testing passed', result.stdout)


def probe():
    try:
        import gi
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gio, GLib, Gtk
    except (ImportError, ValueError):
        return 77
    if not Gtk.init_check()[0]:
        return 77

    sys.path.insert(0, str(ROOT))
    from tempfile import TemporaryDirectory
    from librevault.app import Window

    app = Gtk.Application(application_id='org.librevault.HitTestingTests',
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    with TemporaryDirectory(prefix='librevault-hit-testing-') as directory:
        note = Path(directory) / 'Welcome.md'
        example = (ROOT / 'example-vault' / 'Welcome.md').read_text(encoding='utf-8')
        note.write_text(example, encoding='utf-8')
        window = Window(app, directory)
        window.set_accept_focus(False)
        window.set_focus_on_map(False)
        pane = window.current()
        # This probe measures layout on a real display. Desktop typing must
        # not change its temporary fixture while pointer positions are tested.
        pane.editor.set_editable(False)

        def settle():
            # Allow GTK to draw and allocate lines as well as run idle callbacks.
            loop = GLib.MainLoop()
            def stop():
                loop.quit()
                return False
            GLib.timeout_add(30, stop)
            loop.run()

        try:
            for text in (example, '# Héading **bold**\n\n> A quoted `value`\n\n'
                         '- [x] Finished\n- [ ] Pending\n\n```\nliteral\n```',
                         'Last **formatted** line'):
                pane.buffer.set_text(text)
                for width in (800, 1360):
                    window.resize(width, 650)
                    for offset in (0, len(text)):
                        pane.buffer.place_cursor(pane.buffer.get_iter_at_offset(offset))
                        settle()
                        # Include the padding below every line. This used to
                        # abort inside gtk_text_iter_set_visible_line_index.
                        positions = set()
                        for line in range(pane.buffer.get_line_count()):
                            rect = pane.editor.get_iter_location(pane.buffer.get_iter_at_line(line))
                            positions.update(range(rect.y, rect.y + rect.height + 15))
                        for y in sorted(positions):
                            for x in (0, 32, width // 2, width):
                                pane.editor.get_iter_at_location(x, y)
                                pane.editor.get_iter_at_position(x, y)
                        pane.scroll.get_vadjustment().set_value(80)
                        settle()
                assert pane.text() == text, (pane.text(), text)
            print('Hit-testing passed: hidden syntax, Unicode, resize, scroll, cursor reveal')
        finally:
            window.destroy()
            settle()
    return 0


if __name__ == '__main__':
    if '--probe' in sys.argv:
        raise SystemExit(probe())
    unittest.main()
