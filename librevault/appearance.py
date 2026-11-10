"""Theme colors and typography shared by GTK CSS and Markdown text tags."""
import re

from .preferences import MIN_FONT_SIZE, normalize_editor_font


PALETTES = {
    'dark': {
        'canvas': '#222725', 'panel': '#1a1f1d', 'hover': '#2b332f', 'selected': '#33483e',
        'ink': '#d7dfda', 'muted': '#9eafa5', 'edge': '#343e38', 'accent': '#a8d5bb',
        'raised': '#29312c', 'button_bg': '#2b352e', 'border': '#435047',
        'focus_edge': '#8ab69a', 'bright': '#f1f4ed', 'soft': '#b5c5ba', 'link': '#a8d5bb',
        'code_bg': '#1b211e', 'selection_bg': '#3f5d4c', 'panel_border': '#a8c8b4',
        'highlight_bg': '#5c4b16', 'highlight_fg': '#fff4cc',
        'mark_bg': '#6b5420', 'mark_fg': '#f1f4ed',
        'code_keyword': '#d2a8ff', 'code_string': '#b5cf96', 'code_comment': '#9eafa5',
        'code_number': '#e0b77d', 'code_builtin': '#92cbd0',
    },
    'light': {
        'canvas': '#fcfbf7', 'panel': '#f0f1ea', 'hover': '#e5e9df', 'selected': '#dce9dc',
        'ink': '#35443b', 'muted': '#617066', 'edge': '#dce2d6', 'accent': '#32634b',
        'raised': '#f1f3eb', 'button_bg': '#e9ede3', 'border': '#c5d0c1',
        'focus_edge': '#63896d', 'bright': '#24382b', 'soft': '#526859', 'link': '#32634b',
        'code_bg': '#edf0e7', 'selection_bg': '#c9decd', 'panel_border': '#365e45',
        'highlight_bg': '#ffe58a', 'highlight_fg': '#241c04',
        'mark_bg': '#ffe08a', 'mark_fg': '#1a1400',
        'code_keyword': '#754599', 'code_string': '#30734e', 'code_comment': '#68776c',
        'code_number': '#925b17', 'code_builtin': '#206b7c',
    },
}
PALETTES['lights_out'] = {
    'canvas': '#000000', 'panel': '#000000', 'raised': '#000000',
    'button_bg': '#000000', 'code_bg': '#000000',
    'hover': '#111111', 'selected': '#1a1a1a', 'selection_bg': '#333333',
    'ink': '#ffffff', 'bright': '#ffffff', 'accent': '#ffffff',
    'soft': '#dedede', 'muted': '#bdbdbd', 'edge': '#303030', 'link': '#9fd3ff',
    'border': '#555555', 'focus_edge': '#aaaaaa', 'panel_border': '#ffffff',
    'highlight_bg': '#3d3410', 'highlight_fg': '#fff3c4',
    'mark_bg': '#6b5420', 'mark_fg': '#ffffff',
    'code_keyword': '#d2a8ff', 'code_string': '#9ece6a', 'code_comment': '#9a9a9a',
    'code_number': '#e0af68', 'code_builtin': '#7dcfff',
}

_LATIN_SANS = ('Inter', 'Cantarell')
# A generic family in this list is glued onto the family name. Fontconfig then
# misses the selected face and draws Han text with a bitmap font. Names that
# are not installed are skipped.
_SCRIPT_SANS = (
    'Noto Sans',
    'Noto Sans CJK JP', 'Noto Sans CJK KR', 'Noto Sans CJK SC', 'Noto Sans CJK TC', 'Noto Sans CJK HK',
    'Noto Sans Arabic', 'Noto Sans Hebrew',
    'Noto Sans Devanagari', 'Noto Sans Bengali', 'Noto Sans Tamil', 'Noto Sans Telugu',
    'Noto Sans Thai', 'Noto Sans Gujarati', 'Noto Sans Gurmukhi', 'Noto Sans Kannada',
    'Noto Sans Malayalam', 'Noto Sans Oriya', 'Noto Sans Sinhala',
    'Noto Sans Khmer', 'Noto Sans Lao', 'Noto Sans Myanmar',
    'Noto Sans Armenian', 'Noto Sans Georgian', 'Noto Sans Ethiopic',
)
_SCRIPT_SERIF = (
    'Noto Serif',
    'Noto Serif CJK JP', 'Noto Serif CJK KR', 'Noto Serif CJK SC', 'Noto Serif CJK TC', 'Noto Serif CJK HK',
    'Noto Naskh Arabic', 'Noto Serif Hebrew',
    'Noto Serif Devanagari', 'Noto Serif Bengali', 'Noto Serif Tamil', 'Noto Serif Telugu',
    'Noto Serif Thai', 'Noto Serif Gujarati', 'Noto Serif Gurmukhi', 'Noto Serif Kannada',
    'Noto Serif Malayalam', 'Noto Serif Oriya', 'Noto Serif Sinhala',
    'Noto Serif Khmer', 'Noto Serif Lao', 'Noto Serif Myanmar',
    'Noto Serif Armenian', 'Noto Serif Georgian', 'Noto Serif Ethiopic',
    'Liberation Serif',
)


TITLE_FONTS = ('Georgia', *_SCRIPT_SERIF)

def _quoted_families(names):
    unique = []
    for name in names:
        if name not in unique:
            unique.append(name)
    return ', '.join(f'"{name}"' for name in unique)


def font_family_css(name):
    """CSS font-family value for a stored editor font, falling back to the sans preset."""
    if name == 'monospace':
        return 'monospace'
    if name == 'serif':
        return _quoted_families(_SCRIPT_SERIF)
    if name != 'sans' and normalize_editor_font(name) == name:
        return _quoted_families((name, *_LATIN_SANS, *_SCRIPT_SANS))
    return _quoted_families((*_LATIN_SANS, *_SCRIPT_SANS))


def stylesheet(source, preferences):
    # Replace authoritative color definitions, leaving selectors independent of theme.
    colors = PALETTES[preferences.theme]
    source = re.sub(r'@define-color (\w+) [^;]+;',
                    lambda match: f'@define-color {match[1]} {colors[match[1]]};', source)
    family = font_family_css(preferences.editor_font)
    title_family = _quoted_families(TITLE_FONTS) if preferences.editor_font == 'sans' else family
    return source + f'''
window, dialog {{ font-size: {max(MIN_FONT_SIZE, preferences.interface_size)}pt; }}
textview.live-editor, .editor-preview {{
    font-family: {family};
    font-size: {max(MIN_FONT_SIZE, preferences.editor_size)}pt;
}}
.reading-view {{ font-family: {family}; font-size: {max(MIN_FONT_SIZE, preferences.editor_size)}pt; }}
.reading-view .reading-title {{ font-family: {title_family}; }}
.welcome-title {{ font-family: {_quoted_families(TITLE_FONTS)}; }}
.list-marker {{ font-family: {family}; font-size: {max(MIN_FONT_SIZE, preferences.editor_size)}pt; }}
'''


def font_size_pixels(widget, points=MIN_FONT_SIZE):
    """Convert the minimum readable point size for Cairo's pixel-based text."""
    dpi = widget.get_screen().get_resolution()
    return max(MIN_FONT_SIZE, points) * (dpi if dpi > 0 else 96) / 72
