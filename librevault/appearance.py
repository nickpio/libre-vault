"""Theme colors and typography shared by GTK CSS and Markdown text tags."""
import re


PALETTES = {
    'dark': {
        'canvas': '#0c0c0c', 'panel': '#0c0c0c', 'hover': '#181818', 'selected': '#202020',
        'ink': '#d5d5d5', 'muted': '#999999', 'edge': '#252525', 'accent': '#cccccc',
        'raised': '#161616', 'button_bg': '#1a1a1a', 'border': '#333333',
        'focus_edge': '#505050', 'bright': '#eeeeee', 'soft': '#aaaaaa',
        'code_bg': '#1b1b1b', 'selection_bg': '#3a3a3a',
    },
    'light': {
        'canvas': '#fafafa', 'panel': '#f2f2f2', 'hover': '#eaeaea', 'selected': '#dddddd',
        'ink': '#292929', 'muted': '#686868', 'edge': '#dddddd', 'accent': '#484848',
        'raised': '#ffffff', 'button_bg': '#eeeeee', 'border': '#cccccc',
        'focus_edge': '#888888', 'bright': '#151515', 'soft': '#555555',
        'code_bg': '#e8e8e8', 'selection_bg': '#d0d0d0',
    },
}
FONTS = {'sans': '"Inter", "Cantarell", sans-serif', 'serif': 'serif', 'monospace': 'monospace'}


def stylesheet(source, preferences):
    # Replace authoritative color definitions, leaving selectors independent of theme.
    colors = PALETTES[preferences.theme]
    source = re.sub(r'@define-color (\w+) [^;]+;',
                    lambda match: f'@define-color {match[1]} {colors[match[1]]};', source)
    return source + f'''
window, dialog {{ font-size: {preferences.interface_size}pt; }}
textview.live-editor, .editor-preview {{
    font-family: {FONTS[preferences.editor_font]};
    font-size: {preferences.editor_size}pt;
}}
.list-marker {{ font-size: {preferences.editor_size}pt; }}
'''
