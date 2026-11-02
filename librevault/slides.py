"""Split a note into slides and present them with the reading view."""
import re

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, Gtk

from .metadata import frontmatter_range
from .reading import ReadingView


def split_slides(text):
    """Split on a line that is only ---, after YAML frontmatter and outside fences."""
    bounds = frontmatter_range(text)
    body = text[bounds[2]:] if bounds else text
    slides, current, fence = [], [], None
    for line in body.splitlines(keepends=True):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            current.append(line)
            continue
        if fence is None and re.fullmatch(r'[ ]{0,3}---[ ]*', line.rstrip('\r\n')):
            slides.append(''.join(current).strip('\r\n'))
            current = []
            continue
        current.append(line)
    slides.append(''.join(current).strip('\r\n'))
    while len(slides) > 1 and slides[0] == '':
        slides.pop(0)
    while len(slides) > 1 and slides[-1] == '':
        slides.pop()
    return slides or ['']


class _SlideNote:
    def __init__(self, path):
        self.path = path


class SlideDocument:
    """The small surface ReadingView needs: window, note path, and current text."""

    def __init__(self, window, path, source):
        self.window = window
        self.note = _SlideNote(path)
        self.source = source

    def text(self):
        return self.source


class SlidesWindow(Gtk.Window):
    def __init__(self, owner, path, text):
        super().__init__(title='Slides', transient_for=owner)
        self.owner = owner
        self.path = path
        self.fullscreen_on = False
        self.slides = split_slides(text)
        self.index = 0
        self.set_default_size(1100, 760)
        self.set_destroy_with_parent(True)
        self.get_style_context().add_class('slides-window')
        self.document = SlideDocument(owner, path, '')
        self.reading = ReadingView(self.document)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        root.pack_start(self.reading, True, True, 0)
        bar = Gtk.Box(spacing=12)
        bar.set_border_width(10)
        bar.get_style_context().add_class('slides-bar')
        self.previous = Gtk.Button(label='Previous')
        self.previous.get_accessible().set_name('Previous slide')
        self.previous.set_tooltip_text('Previous slide · Left or Page Up')
        self.counter = Gtk.Label()
        self.counter.set_hexpand(True)
        self.counter.get_style_context().add_class('slides-counter')
        self.next = Gtk.Button(label='Next')
        self.next.get_accessible().set_name('Next slide')
        self.next.set_tooltip_text('Next slide · Right, Space, or Page Down')
        bar.pack_start(self.previous, False, False, 0)
        bar.pack_start(self.counter, True, True, 0)
        bar.pack_end(self.next, False, False, 0)
        root.pack_end(bar, False, False, 0)
        self.previous.connect('clicked', lambda *_: self.nudge(-1))
        self.next.connect('clicked', lambda *_: self.nudge(1))
        self.connect('destroy', self._closed)
        self.show_slide()
        self.show_all()
        self.grab_focus()

    def _closed(self, *_):
        if getattr(self.owner, 'slides_window', None) is self:
            self.owner.slides_window = None

    def show_slide(self):
        count = len(self.slides)
        self.index = max(0, min(count - 1, self.index))
        self.document.source = self.slides[self.index]
        self.reading.refresh(force=True)
        label = f'Slide {self.index + 1} of {count}'
        self.counter.set_text(label)
        self.counter.get_accessible().set_name(label)
        self.previous.set_sensitive(self.index > 0)
        self.next.set_sensitive(self.index < count - 1)
        stem = self.path.rsplit('/', 1)[-1]
        if stem.lower().endswith('.md'):
            stem = stem[:-3]
        self.set_title(f'Slides · {stem}')

    def nudge(self, delta):
        target = max(0, min(len(self.slides) - 1, self.index + delta))
        if target == self.index:
            return
        self.index = target
        self.show_slide()

    def go(self, index):
        self.index = index
        self.show_slide()

    def toggle_fullscreen(self):
        self.fullscreen_on = not self.fullscreen_on
        if self.fullscreen_on:
            self.fullscreen()
        else:
            self.unfullscreen()

    def handle_key(self, event):
        key = event.keyval
        if key in (Gdk.KEY_Right, Gdk.KEY_KP_Right, Gdk.KEY_space, Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Down):
            self.nudge(1)
            return True
        if key in (Gdk.KEY_Left, Gdk.KEY_KP_Left, Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up):
            self.nudge(-1)
            return True
        if key in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            self.go(0)
            return True
        if key in (Gdk.KEY_End, Gdk.KEY_KP_End):
            self.go(len(self.slides) - 1)
            return True
        if key == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True
        if key == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def do_key_press_event(self, event):
        if self.handle_key(event):
            return True
        return Gtk.Window.do_key_press_event(self, event)
