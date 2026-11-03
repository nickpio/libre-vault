"""Disposable SQLite note index and byte-bounded loaded-text cache."""
from collections import OrderedDict
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import weakref

import yaml

from .metadata import NoteMetadata


class TextCache:
    def __init__(self, max_bytes=4 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.bytes = 0
        self.entries = OrderedDict()

    def get(self, key):
        value = self.entries.get(key)
        if value is not None:
            self.entries.move_to_end(key)
            return value[0]

    def put(self, key, note):
        self.remove(key)
        size = sys.getsizeof(note.text) + sys.getsizeof(note) + sys.getsizeof(key) + 128
        if size > self.max_bytes:
            return
        while self.entries and self.bytes + size > self.max_bytes:
            self.remove(next(iter(self.entries)))
        self.entries[key] = (note, size)
        self.bytes += size

    def remove(self, key):
        old = self.entries.pop(key, None)
        if old is not None:
            self.bytes -= old[1]

    def clear(self):
        self.entries.clear()
        self.bytes = 0


class CorruptIndex(sqlite3.DatabaseError):
    pass


class NoteIndex:
    VERSION = 2

    def __init__(self, root, cache_dir=None):
        base = Path(cache_dir) if cache_dir is not None else Path(
            os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'libre-vault'
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = base / (hashlib.sha256(str(root).encode()).hexdigest() + '.sqlite3')
        self.connection = None
        try:
            self.open()
        except sqlite3.DatabaseError as error:
            # Only derived data is discarded. The Markdown files are untouched.
            if self.connection is not None:
                self.connection.close()
            if not isinstance(error, CorruptIndex) and getattr(error, 'sqlite_errorcode', None) not in (
                    sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                raise
            self.path.unlink(missing_ok=True)
            self.open()
        self._finalizer = weakref.finalize(self, self.connection.close)

    def open(self):
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.connection = sqlite3.connect(self.path)
        db = self.connection
        db.execute('PRAGMA cache_size=-2048')
        db.execute('PRAGMA mmap_size=0')
        db.execute('PRAGMA temp_store=FILE')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise CorruptIndex('Invalid derived index')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version != self.VERSION:
            db.executescript('DROP TABLE IF EXISTS search; DROP TABLE IF EXISTS notes;')
        db.execute('''CREATE TABLE IF NOT EXISTS notes (
            path TEXT PRIMARY KEY, revision TEXT NOT NULL, bom INTEGER NOT NULL,
            text TEXT NOT NULL, properties TEXT NOT NULL, tags TEXT NOT NULL,
            aliases TEXT NOT NULL, error TEXT, links TEXT NOT NULL)''')
        # Trigrams accelerate substring searches without changing word/phrase
        # semantics. Older SQLite builds can still stream the exact matcher.
        had_search = db.execute("SELECT 1 FROM sqlite_master WHERE name='search'").fetchone() is not None
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(path UNINDEXED, body, tokenize='trigram')")
            self.fts = True
        except sqlite3.OperationalError as error:
            if 'no such module: fts5' not in str(error) and 'no such tokenizer: trigram' not in str(error):
                raise
            self.fts = False
        if self.fts and not had_search:
            for rowid, path, text, aliases in db.execute('SELECT rowid,path,text,aliases FROM notes'):
                body = (path + '\n' + text + '\n' + '\n'.join(json.loads(aliases))).casefold().replace('\x00', '\n')
                db.execute('INSERT INTO search(rowid,path,body) VALUES (?,?,?)', (rowid, path, body))
        db.execute(f'PRAGMA user_version={self.VERSION}')
        db.commit()

    def store(self, note, metadata, links):
        db = self.connection
        row = db.execute('SELECT rowid,revision FROM notes WHERE path=?', (note.path,)).fetchone()
        if row is not None and row[1] == note.revision:
            return
        if self.fts and row is not None:
            db.execute('DELETE FROM search WHERE rowid=?', (row[0],))
        db.execute('INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?,?,?,?,?)', (
            note.path, note.revision, note.bom, note.text,
            yaml.safe_dump(metadata.properties), json.dumps(metadata.tags), json.dumps(metadata.aliases),
            metadata.error, json.dumps([asdict(link) for link in links])))
        if self.fts:
            rowid = db.execute('SELECT rowid FROM notes WHERE path=?', (note.path,)).fetchone()[0]
            db.execute('INSERT INTO search(rowid,path,body) VALUES (?,?,?)', (
                rowid, note.path, (note.path + '\n' + note.text + '\n' + '\n'.join(metadata.aliases)).casefold().replace('\x00', '\n')))

    def revision(self, path):
        row = self.connection.execute('SELECT revision FROM notes WHERE path=?', (path,)).fetchone()
        return row[0] if row else None

    def remove(self, path):
        if self.fts:
            self.connection.execute('DELETE FROM search WHERE rowid IN (SELECT rowid FROM notes WHERE path=?)', (path,))
        self.connection.execute('DELETE FROM notes WHERE path=?', (path,))

    def prune(self, paths):
        for (path,) in self.connection.execute('SELECT path FROM notes'):
            if path not in paths:
                self.remove(path)

    def text(self, path):
        row = self.connection.execute('SELECT text FROM notes WHERE path=?', (path,)).fetchone()
        return row[0] if row else None

    def snapshot(self, path):
        return self.connection.execute('SELECT text,revision,bom FROM notes WHERE path=?', (path,)).fetchone()

    def metadata(self, path):
        row = self.connection.execute('SELECT properties,tags,aliases,error FROM notes WHERE path=?', (path,)).fetchone()
        if row is None:
            return NoteMetadata({}, (), ())
        return NoteMetadata(yaml.safe_load(row[0]), tuple(json.loads(row[1])), tuple(json.loads(row[2])), row[3])

    def links(self, path):
        row = self.connection.execute('SELECT links FROM notes WHERE path=?', (path,)).fetchone()
        return json.loads(row[0]) if row else []

    def candidates(self, terms):
        # Restrict only on positive literal terms of at least three characters.
        # All other filters and Unicode/alias behavior use the exact matcher.
        literals = [value.casefold() for negative, field, value in terms
                    if not negative and field in ('', 'content') and len(value.casefold()) >= 3
                    and '\x00' not in value]
        if self.fts and literals and not getattr(terms, 'complex', False):
            match = ' AND '.join('"' + value.replace('"', '""') + '"' for value in literals)
            yield from (row[0] for row in self.connection.execute(
                'SELECT path FROM search WHERE search MATCH ? ORDER BY path', (match,)))
        else:
            yield from (row[0] for row in self.connection.execute('SELECT path FROM notes ORDER BY path'))

    def close(self):
        if self.connection is not None:
            self._finalizer()
            self.connection = None
