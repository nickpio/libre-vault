"""Grouped source edits and selections for a GTK text buffer's undo history."""
from contextlib import contextmanager
from dataclasses import dataclass
import time
import sys
from functools import cached_property


@dataclass(frozen=True)
class Selection:
    cursor: int
    anchor: int


@dataclass(frozen=True)
class Change:
    start: int
    removed: str
    inserted: str


@dataclass(frozen=True)
class Action:
    changes: tuple[Change, ...]
    before: Selection
    after: Selection


    @cached_property
    def size(self):
        return (sys.getsizeof(self) + sys.getsizeof(self.changes) + 256
                + sum(change_size(change) for change in self.changes))


def change_size(change):
    return sys.getsizeof(change) + sys.getsizeof(change.removed) + sys.getsizeof(change.inserted) + 128


def action_kind(action):
    if len(action.changes) != 1 or action.before.cursor != action.before.anchor:
        return 'edit'
    change = action.changes[0]
    if not change.removed and len(change.inserted) == 1 and change.inserted.isprintable():
        return 'typing'
    if not change.inserted and len(change.removed) == 1 and change.removed.isprintable():
        if change.start == action.before.cursor - 1:
            return 'backspace'
        if change.start == action.before.cursor:
            return 'delete'
    return 'edit'


def merge_actions(previous, current, kind):
    if previous.after != current.before:
        return None
    old, new = previous.changes[0], current.changes[0]
    if kind == 'typing' and new.start == old.start + len(old.inserted) and not old.inserted[-1].isspace():
        change = Change(old.start, '', old.inserted + new.inserted)
    elif kind == 'backspace' and new.start + len(new.removed) == old.start and not old.removed[0].isspace():
        change = Change(new.start, new.removed + old.removed, '')
    elif kind == 'delete' and new.start == old.start and not old.removed[-1].isspace():
        change = Change(old.start, old.removed + new.removed, '')
    else:
        return None
    return Action((change,), previous.before, current.after)


class BufferHistory:
    """Record text mutations, ignoring formatting tags and history replay.

    GTK user-action boundaries group list continuation, task toggles, paste,
    formatting, and replacements. Adjacent typing/deletion is coalesced for
    up to one second. Store deltas, bounded to 500 actions, rather than copies
    of the entire document for every keystroke. An 8 MiB budget bounds retained
    actions too; edits larger than that establish a new undo checkpoint.
    """
    def __init__(self, buffer, max_bytes=8 * 1024 * 1024):
        self.buffer = buffer
        self.max_bytes = max_bytes
        self.pending_bytes = 0
        self.overflow = False
        self.undo_stack = []
        self.redo_stack = []
        self.depth = 0
        self.editing = 0
        self.suppressed = 0
        self.pending = []
        self.before = None
        self.last_time = 0
        self.last_kind = 'edit'
        self.signals = [
            buffer.connect('begin-user-action', self.begin),
            buffer.connect_after('end-user-action', self.end),
            buffer.connect('insert-text', self.insert),
            buffer.connect_after('insert-text', self.after_edit),
            buffer.connect('delete-range', self.delete),
            buffer.connect_after('delete-range', self.after_edit),
            buffer.connect('mark-set', self.mark_set),
        ]

    @property
    def can_undo(self):
        return bool(self.undo_stack)

    @property
    def can_redo(self):
        return bool(self.redo_stack)

    def selection(self):
        return Selection(self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset(),
                         self.buffer.get_iter_at_mark(self.buffer.get_selection_bound()).get_offset())

    def begin(self, *_):
        if not self.suppressed:
            if self.depth == 0:
                self.before = self.selection()
            self.depth += 1

    def end(self, *_):
        if not self.suppressed:
            self.depth -= 1
            if self.depth == 0:
                self.commit()

    def capture(self, change):
        if not self.suppressed:
            if self.before is None:
                self.before = self.selection()
            if not self.overflow:
                self.pending_bytes += change_size(change)
                if self.pending_bytes > self.max_bytes:
                    self.pending.clear()
                    self.overflow = True
                else:
                    self.pending.append(change)
            self.editing += 1

    def insert(self, _, position, text, length):
        self.capture(Change(position.get_offset(), '', text))

    def delete(self, _, start, end):
        self.capture(Change(start.get_offset(), self.buffer.get_text(start, end, True), ''))

    def after_edit(self, *_):
        if not self.suppressed:
            self.editing -= 1
            if self.depth == 0:
                self.commit()

    @property
    def retained_bytes(self):
        return sum(action.size for action in self.undo_stack + self.redo_stack)

    def commit(self):
        if self.overflow:
            # A partial compound action cannot be undone safely. An oversized
            # edit establishes a new checkpoint rather than breaking offsets.
            self.reset()
            return
        self.pending_bytes = 0
        if not self.pending:
            self.before = None
            return
        action = Action(tuple(self.pending), self.before, self.selection())
        self.pending = []
        self.before = None
        now, kind = time.monotonic(), action_kind(action)
        merged = None
        if self.undo_stack and kind != 'edit' and kind == self.last_kind and now - self.last_time <= 1:
            merged = merge_actions(self.undo_stack[-1], action, kind)
        if merged:
            self.undo_stack[-1] = merged
        else:
            self.undo_stack.append(action)
            del self.undo_stack[:-500]
        self.redo_stack.clear()
        retained = self.retained_bytes
        while self.undo_stack and retained > self.max_bytes:
            retained -= self.undo_stack.pop(0).size
        self.last_time, self.last_kind = now, kind

    def mark_set(self, _, position, mark):
        if (mark.get_name() in ('insert', 'selection_bound') and not self.suppressed
                and not self.depth and not self.editing):
            self.break_group()

    def break_group(self):
        self.last_time = 0
        self.last_kind = 'edit'

    @contextmanager
    def suspended(self):
        self.suppressed += 1
        try:
            yield
        finally:
            self.suppressed -= 1

    def replay(self, action, backwards):
        with self.suspended():
            self.buffer.begin_user_action()
            try:
                changes = reversed(action.changes) if backwards else action.changes
                for change in changes:
                    removed = change.inserted if backwards else change.removed
                    inserted = change.removed if backwards else change.inserted
                    start = self.buffer.get_iter_at_offset(change.start)
                    end = self.buffer.get_iter_at_offset(change.start + len(removed))
                    self.buffer.delete(start, end)
                    self.buffer.insert(start, inserted)
                selection = action.before if backwards else action.after
                self.buffer.select_range(self.buffer.get_iter_at_offset(selection.cursor),
                                         self.buffer.get_iter_at_offset(selection.anchor))
            finally:
                self.buffer.end_user_action()
        self.break_group()

    def undo(self):
        if not self.undo_stack:
            return False
        action = self.undo_stack.pop()
        self.replay(action, True)
        self.redo_stack.append(action)
        return True

    def redo(self):
        if not self.redo_stack:
            return False
        action = self.redo_stack.pop()
        self.replay(action, False)
        self.undo_stack.append(action)
        return True

    def reset(self):
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.pending.clear()
        self.pending_bytes = 0
        self.overflow = False
        self.before = None
        self.break_group()

    def close(self):
        for signal in self.signals:
            self.buffer.disconnect(signal)
        self.signals.clear()
        self.reset()
