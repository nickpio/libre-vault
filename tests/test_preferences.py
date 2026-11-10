"""Preference parsing and atomic storage require no GTK or note vault."""
from dataclasses import replace
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from librevault.appearance import PALETTES, font_family_css, font_size_pixels, stylesheet
from librevault.preferences import BOUNDS, MIN_FONT_SIZE, Preferences, PreferencesStore


class PreferenceTests(unittest.TestCase):
    def test_font_sizes_have_an_eleven_point_minimum(self):
        self.assertGreaterEqual(Preferences().interface_size, 11)
        self.assertGreaterEqual(Preferences().editor_size, 11)
        for field in ('interface_size', 'editor_size'):
            self.assertEqual(BOUNDS[field][0], 11)
            for old_size in (8, 9, 10):
                with self.subTest(field=field, old_size=old_size):
                    self.assertEqual(getattr(Preferences.from_dict({field: old_size}), field), 11)
            self.assertEqual(getattr(Preferences.from_dict({field: 16}), field), 16)
        source = (Path(__file__).resolve().parents[1] / 'librevault' / 'style.css').read_text()
        rendered = stylesheet(source, replace(Preferences(), interface_size=8, editor_size=8))
        sizes = re.findall(r'font-size:\s*([\d.]+)pt', rendered)
        self.assertTrue(sizes)
        self.assertTrue(all(float(size) >= MIN_FONT_SIZE for size in sizes))

    def test_theme_text_and_active_states_have_readable_contrast(self):
        def luminance(color):
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                      for value in channels]
            return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
        for theme, colors in PALETTES.items():
            for foreground, background in (('ink', 'canvas'), ('muted', 'panel'),
                                           ('soft', 'panel'), ('accent', 'selected')):
                with self.subTest(theme=theme, foreground=foreground, background=background):
                    low, high = sorted((luminance(colors[foreground]), luminance(colors[background])))
                    self.assertGreaterEqual((high + 0.05) / (low + 0.05), 4.5)

    def test_reading_titles_respect_custom_editor_fonts(self):
        source = (Path(__file__).resolve().parents[1] / 'librevault' / 'style.css').read_text()
        custom = stylesheet(source, replace(Preferences(), editor_font='monospace'))
        self.assertIn('.reading-view .reading-title { font-family: monospace; }', custom)

    def test_cairo_font_sizes_respect_the_minimum_at_screen_resolution(self):
        widget = Mock()
        for dpi in (-1, 72, 96, 144):
            with self.subTest(dpi=dpi):
                widget.get_screen.return_value.get_resolution.return_value = dpi
                self.assertAlmostEqual(font_size_pixels(widget, 8), 11 * (dpi if dpi > 0 else 96) / 72)

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
            with patch('librevault.durable.replace_file', side_effect=OSError('Disk full')):
                with self.assertRaises(OSError):
                    store.save(Preferences())
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(list(store.path.parent.glob('.settings-*')), [])

    def test_configuration_uses_xdg_directory(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_CONFIG_HOME': directory}):
            self.assertEqual(PreferencesStore().path, Path(directory) / 'libre-vault' / 'settings.json')

    def test_custom_editor_font_is_kept_and_unsafe_names_fall_back(self):
        custom = replace(Preferences(), editor_font='IBM Plex Sans')
        self.assertEqual(Preferences.from_dict({'editor_font': '  IBM Plex Sans  '}), custom)
        self.assertEqual(Preferences.from_dict({'editor_font': '文泉驿等宽正黑'}).editor_font, '文泉驿等宽正黑')
        self.assertEqual(Preferences.from_dict({'editor_font': '  monospace  '}).editor_font, 'monospace')
        for bad in ('url(evil)', 'foo;bar', '"Inter"', 'a\nb', 'x' * 121, '---', '../Font', 12, None):
            self.assertEqual(Preferences.from_dict({'editor_font': bad}).editor_font, 'sans', bad)
        css = font_family_css('IBM Plex Sans')
        self.assertTrue(css.startswith('"IBM Plex Sans", "Inter", "Cantarell", "Noto Sans", '))
        self.assertIn('"Noto Sans CJK JP"', css)
        self.assertIn('"Noto Sans Arabic"', css)
        self.assertIn('"Noto Sans Hebrew"', css)
        self.assertNotIn('sans-serif', css)
        arabic = font_family_css('Noto Sans Arabic')
        self.assertTrue(arabic.startswith('"Noto Sans Arabic", '))
        self.assertEqual(arabic.count('"Noto Sans Arabic"'), 1)
        serif = font_family_css('serif')
        self.assertTrue(serif.startswith('"Noto Serif", '))
        self.assertIn('"Noto Serif CJK JP"', serif)
        self.assertIn('"Noto Naskh Arabic"', serif)
        self.assertTrue(serif.endswith('"Liberation Serif"'))
        self.assertFalse(any(part.strip() == 'serif' for part in serif.split(',')))
        self.assertEqual(font_family_css('monospace'), 'monospace')
        self.assertEqual(font_family_css('"); color: red'), font_family_css('sans'))
        self.assertNotIn('sans-serif', font_family_css('sans'))

    def test_all_themes_define_all_stylesheet_colors(self):
        source = (Path(__file__).resolve().parents[1] / 'librevault' / 'style.css').read_text()
        for theme in PALETTES:
            rendered = stylesheet(source, replace(Preferences(), theme=theme, editor_font='serif', editor_size=16))
            self.assertIn('.reading-view { font-family: "Noto Serif", ', rendered)
            self.assertNotIn('font-family: serif;', rendered)
            self.assertIn('font-size: 16pt', rendered)
            self.assertIn(f"@define-color canvas {PALETTES[theme]['canvas']};", rendered)


if __name__ == '__main__':
    unittest.main()
