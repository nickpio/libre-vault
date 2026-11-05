"""Theme colors and typography shared by GTK CSS and Markdown text tags."""
import re

from .preferences import normalize_editor_font


PALETTES = {
    'dark': {
        'canvas': '#0c0c0c', 'panel': '#0c0c0c', 'hover': '#181818', 'selected': '#202020',
        'ink': '#d5d5d5', 'muted': '#999999', 'edge': '#252525', 'accent': '#cccccc',
        'raised': '#161616', 'button_bg': '#1a1a1a', 'border': '#333333',
        'focus_edge': '#505050', 'bright': '#eeeeee', 'soft': '#aaaaaa',
        'code_bg': '#1b1b1b', 'selection_bg': '#3a3a3a', 'panel_border': '#ffffff',
        'highlight_bg': '#5c4b16', 'highlight_fg': '#fff4cc',
        'code_keyword': '#d2a8ff', 'code_string': '#9ece6a', 'code_comment': '#8b8b8b',
        'code_number': '#e0af68', 'code_builtin': '#7dcfff',
    },
}
# Preserve the dark theme's contrast and surface hierarchy in light mode.
PALETTES['light'] = {
    name: f'#{0xFFFFFF - int(color[1:], 16):06x}'
    for name, color in PALETTES['dark'].items()
}
# Inversion does not keep yellow highlights or syntax colors readable.
PALETTES['light'].update({
    'highlight_bg': '#ffe58a', 'highlight_fg': '#241c04',
    'code_keyword': '#6f42c1', 'code_string': '#18794e', 'code_comment': '#6e6e6e',
    'code_number': '#a15c07', 'code_builtin': '#0b6e99',
})
PALETTES['lights_out'] = {
    'canvas': '#000000', 'panel': '#000000', 'raised': '#000000',
    'button_bg': '#000000', 'code_bg': '#000000',
    'hover': '#111111', 'selected': '#1a1a1a', 'selection_bg': '#333333',
    'ink': '#ffffff', 'bright': '#ffffff', 'accent': '#ffffff',
    'soft': '#dedede', 'muted': '#bdbdbd', 'edge': '#303030',
    'border': '#555555', 'focus_edge': '#aaaaaa', 'panel_border': '#ffffff',
    'highlight_bg': '#3d3410', 'highlight_fg': '#fff3c4',
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
    return source + f'''
window, dialog {{ font-size: {preferences.interface_size}pt; }}
textview.live-editor, .editor-preview {{
    font-family: {family};
    font-size: {preferences.editor_size}pt;
}}
.reading-view {{ font-family: {family}; }}
.list-marker {{ font-family: {family}; font-size: {preferences.editor_size}pt; }}
'''
