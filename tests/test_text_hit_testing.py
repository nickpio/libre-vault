"""Exercise GTK pointer hit-testing in a child process, since failures abort."""
from tests.gui_wait import wait_for_work
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TextHitTestingTests(unittest.TestCase):
    def test_concealed_prefix_keeps_padding_hits_inside_text(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), '--padding-probe'],
            cwd=ROOT, capture_output=True, text=True, timeout=20)
        if result.returncode == 77:
            self.skipTest('GTK 3 and a display are required')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('Gtk-CRITICAL', result.stderr)
        self.assertIn('Padding hit-testing passed', result.stdout)

    def test_hidden_syntax_survives_hit_testing_and_layout_changes(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), '--probe'],
            cwd=ROOT, capture_output=True, text=True, timeout=20)
        if result.returncode == 77:
            self.skipTest('GTK 3 and a display are required')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('Gtk-CRITICAL', result.stderr)
        self.assertIn('Hit-testing passed', result.stdout)


def probe():
    try:
        import gi
        gi.require_version('Gtk', '3.0')
        gi.require_version('Gdk', '3.0')
        from gi.repository import Gdk, Gio, GLib, Gtk
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
        wait_for_work(window)
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
                         'Last **formatted** line',
                         'Before\nLast [link](destination)',
                         'Before\n[[One]] and [[Two]] have **hidden** syntax.\n',
                         'Before\n[Héllo](Some long destination.md) and [[Welcome|世界]]\nAfter',
                         'Before\n' + '[link](destination) and ' * 35 + '\nAfter'):
                pane.buffer.set_text(text)
                pane.set_mode('live')
                for width in (800, 1360):
                    window.resize(width, 650)
                    for offset in (0, len(text)):
                        pane.buffer.place_cursor(pane.buffer.get_iter_at_offset(offset))
                        settle()
                        # Exercise GTK's native tag dispatch and selection,
                        # which bypass the public get_iter_at_* methods.
                        bottom, height = pane.editor.get_line_yrange(pane.buffer.get_end_iter())
                        event_x, event_y = pane.editor.buffer_to_window_coords(
                            Gtk.TextWindowType.TEXT, 100, bottom + height + 20)
                        for kind in (Gdk.EventType.MOTION_NOTIFY, Gdk.EventType.BUTTON_PRESS,
                                     Gdk.EventType.BUTTON_RELEASE):
                            event = Gdk.Event.new(kind)
                            event.window = pane.editor.get_window(Gtk.TextWindowType.TEXT)
                            event.x, event.y = event_x, event_y
                            event.send_event = True
                            event.time = Gdk.CURRENT_TIME
                            event.set_device(pane.editor.get_display().get_default_seat().get_pointer())
                            if kind != Gdk.EventType.MOTION_NOTIFY:
                                event.button = 1
                            pane.editor.event(event)
                        # Include the padding below every line. This used to
                        # abort inside gtk_text_iter_set_visible_line_index.
                        positions = set(range(0, 700))
                        for line in range(pane.buffer.get_line_count()):
                            line_y, line_height = pane.editor.get_line_yrange(pane.buffer.get_iter_at_line(line))
                            positions.update(range(line_y - 2, line_y + line_height + 15))
                        for y in sorted(positions):
                            for x in (0, 32, width // 2, width):
                                found, iterator = pane.editor.get_iter_at_location(x, y)
                                if found:
                                    pane.editor.get_iter_location(iterator)
                                pane.editor.get_iter_at_position(x, y)
                        pane.scroll.get_vadjustment().set_value(80)
                        settle()
                assert pane.text() == text, (pane.text(), text)
            print('Hit-testing passed: hidden syntax, Unicode, resize, scroll, cursor reveal')
        finally:
            window.destroy()
            settle()
    return 0


def padding_probe():
    try:
        import gi
        gi.require_version('Gtk', '3.0')
        gi.require_version('Gdk', '3.0')
        from gi.repository import Gtk
    except (ImportError, ValueError):
        return 77
    if not Gtk.init_check()[0]:
        return 77

    sys.path.insert(0, str(ROOT))
    from librevault.textview import EditorTextView

    editor = EditorTextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            pixels_below_lines=30)
    window = Gtk.Window()
    window.set_default_size(400, 300)
    window.add(editor)
    window.show_all()
    try:
        for alias in ('Héllo 世界', 'Héllo 世界 ' * 40 + 'end'):
            prefix = '[[' + 'destination/' * 20 + '|'
            text = prefix + alias + ']]\nAfter'
            buffer = editor.get_buffer()
            buffer.set_text(text)
            hidden = buffer.create_tag(None, invisible=True)
            buffer.apply_tag(hidden, buffer.get_start_iter(),
                             buffer.get_iter_at_offset(len(prefix)))
            buffer.apply_tag(hidden, buffer.get_iter_at_offset(len(prefix + alias)),
                             buffer.get_iter_at_offset(len(prefix + alias) + 2))
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            last = buffer.get_iter_at_offset(len(prefix + alias) - 1)
            cursor, _ = editor.get_cursor_locations(last)
            bottom = cursor.y + cursor.height
            # A long hidden prefix makes GTK's character rectangle empty.
            # Paragraph height includes padding and cannot replace it safely.
            for y in range(bottom + 1, bottom + 20):
                assert editor._hit_y(y) < bottom, (alias, y, bottom)
                for x in (0, 100, 400):
                    found, iterator = editor.get_iter_at_location(x, y)
                    assert not found and iterator.get_line() == 0
                    found, iterator, _ = editor.get_iter_at_position(x, y)
                    assert not found and iterator.get_line() == 0
            # Visible text must still be hit at its original coordinates.
            y = cursor.y + cursor.height // 2
            assert editor._hit_y(y) == y
        print('Padding hit-testing passed: concealed prefix, Unicode, wrapping')
    finally:
        window.destroy()
    return 0


if __name__ == '__main__':
    if '--padding-probe' in sys.argv:
        raise SystemExit(padding_probe())
    if '--probe' in sys.argv:
        raise SystemExit(probe())
    unittest.main()
