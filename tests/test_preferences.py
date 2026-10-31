"""Preference parsing and atomic storage require no GTK or note vault."""
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.appearance import PALETTES, stylesheet
from librevault.preferences import Preferences, PreferencesStore


class PreferenceTests(unittest.TestCase):
    def test_missing_configuration_uses_defaults_without_creating_files(self):
        with TemporaryDirectory() as directory:
            store = PreferencesStore(Path(directory) / 'config' / 'settings.json')
            self.assertEqual(store.load(), Preferences())
            self.assertFalse(store.path.parent.exists())

    def test_invalid_fields_fall_back_individually(self):
        self.assertEqual(Preferences.from_dict({
            'theme': 'unknown', 'editor_size': True, 'interface_size': 1000,
            'line_width': -1, 'editor_font': 'url(evil)', 'wrap_lines': 'false',
            'readable_length': False, 'continue_lists': False,
        }), replace(Preferences(), readable_length=False, continue_lists=False))

    def test_partial_and_non_object_preferences(self):
        self.assertEqual(Preferences.from_dict({'theme': 'light', 'editor_size': 14, 'unknown': 1}),
                         replace(Preferences(), theme='light', editor_size=14))
        self.assertEqual(Preferences.from_dict([]), Preferences())

    def test_malformed_json_and_non_utf8_use_defaults(self):
        with TemporaryDirectory() as directory:
            store = PreferencesStore(Path(directory) / 'settings.json')
            for raw in (b'{broken', b'\xff'):
                store.path.write_bytes(raw)
                self.assertEqual(store.load(), Preferences())
                self.assertEqual(store.path.read_bytes(), raw)

    def test_atomic_save_round_trip_and_failed_replace_preserves_previous_file(self):
        with TemporaryDirectory() as directory:
            store = PreferencesStore(Path(directory) / 'config' / 'settings.json')
            expected = replace(Preferences(), theme='light', editor_size=15)
            store.save(expected)
            self.assertEqual(PreferencesStore(store.path).load(), expected)
            before = store.path.read_bytes()
            with patch('librevault.preferences.os.replace', side_effect=OSError('Disk full')):
                with self.assertRaises(OSError):
                    store.save(Preferences())
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(list(store.path.parent.glob('.settings-*')), [])

    def test_configuration_uses_xdg_directory(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_CONFIG_HOME': directory}):
            self.assertEqual(PreferencesStore().path, Path(directory) / 'libre-vault' / 'settings.json')

    def test_both_themes_define_all_stylesheet_colors(self):
        source = (Path(__file__).resolve().parents[1] / 'librevault' / 'style.css').read_text()
        for theme in PALETTES:
            rendered = stylesheet(source, replace(Preferences(), theme=theme, editor_font='serif', editor_size=16))
            self.assertIn('font-family: serif', rendered)
            self.assertIn('font-size: 16pt', rendered)
            self.assertIn(f"@define-color canvas {PALETTES[theme]['canvas']};", rendered)


if __name__ == '__main__':
    unittest.main()
