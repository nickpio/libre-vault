"""Small shared GTK factories used by the window and editor components."""
from gi.repository import Gtk


def scrolled(widget, width=None):
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.add(widget)
    if width:
        scroll.set_size_request(width, -1)
    return scroll


def text_buffer(text):
    buffer = Gtk.TextBuffer()
    buffer.set_text(text)
    return buffer


def count_summary(text, lines):
    """Words, characters, and lines for the active note."""
    words = len(text.split())
    characters = len(text)

    def unit(count, singular, plural):
        return f'{count} {singular if count == 1 else plural}'

    return (f'{unit(words, "word", "words")} · '
            f'{unit(characters, "character", "characters")} · '
            f'{unit(lines, "line", "lines")}')

