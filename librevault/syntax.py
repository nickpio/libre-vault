"""Lightweight source highlighters for fenced code blocks.

Token offsets are character indexes into the code string the caller passes.
They never rewrite that string. Unknown languages still get comments, strings,
and numbers; recognized languages add keywords and builtins.
"""
from dataclasses import dataclass
import re


TOKEN_STYLES = (
    'code_keyword', 'code_string', 'code_comment', 'code_number', 'code_builtin',
)

_ALIASES = {
    'py': 'python', 'js': 'javascript', 'ts': 'typescript', 'yml': 'yaml',
    'sh': 'shell', 'bash': 'shell', 'shell': 'shell', 'c++': 'cpp', 'cc': 'cpp',
    'hpp': 'cpp', 'h': 'c', 'md': 'markdown',
}


@dataclass(frozen=True)
class Token:
    start: int
    end: int
    style: str


def _words(words, ignore_case=False):
    ordered = sorted(set(words), key=len, reverse=True)
    body = r'\b(?:' + '|'.join(ordered) + r')\b'
    return '(?i:' + body + ')' if ignore_case else body


def _lexer(parts):
    groups, styles = [], []
    for index, (style, pattern) in enumerate(parts):
        groups.append(f'(?P<g{index}>{pattern})')
        styles.append(style)
    return re.compile('|'.join(groups), re.MULTILINE), tuple(styles)


def _scan(lexer, source):
    regex, styles = lexer
    tokens = []
    for match in regex.finditer(source):
        start, end = match.start(), match.end()
        if end <= start:
            continue
        tokens.append(Token(start, end, styles[match.lastindex - 1]))
    return tokens


_NUMBER = (
    r'(?<![A-Za-z0-9_])(?:0[xX][0-9A-Fa-f_]+|0[bB][01_]+|0[oO][0-7_]+|'
    r'\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)(?![A-Za-z0-9_])'
)
_QUOTED = (
    r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\''
)
_PY_STRING = (
    r'(?<![A-Za-z0-9_])(?:[rRuUbBfF]{1,3})?'
    r'(?:\'\'\'[\s\S]*?\'\'\'|"""[\s\S]*?"""|(?<!\')\'(?!\')(?:\\.|[^\'\\\n])*\'|"(?:\\.|[^"\\\n])*")'
)
_BLOCK_COMMENT = r'/\*[\s\S]*?\*/'
_LINE_COMMENT = r'(?<!:)//[^\n]*'
_HASH_COMMENT = r'#[^\n]*'

_PY_KEYWORDS = (
    'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await', 'break',
    'class', 'continue', 'def', 'del', 'elif', 'else', 'except', 'finally', 'for',
    'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or',
    'pass', 'raise', 'return', 'try', 'while', 'with', 'yield',
)
_PY_BUILTINS = (
    'abs', 'all', 'any', 'bool', 'dict', 'enumerate', 'filter', 'float', 'hex',
    'input', 'int', 'isinstance', 'len', 'list', 'map', 'max', 'min', 'object',
    'open', 'ord', 'pow', 'print', 'range', 'repr', 'reversed', 'round', 'set',
    'sorted', 'str', 'sum', 'super', 'tuple', 'type', 'zip',
)
_JS_KEYWORDS = (
    'async', 'await', 'break', 'case', 'catch', 'class', 'const', 'continue',
    'debugger', 'default', 'delete', 'do', 'else', 'export', 'extends', 'finally',
    'for', 'from', 'function', 'if', 'import', 'in', 'instanceof', 'let', 'new',
    'of', 'return', 'static', 'super', 'switch', 'this', 'throw', 'try', 'typeof',
    'var', 'void', 'while', 'with', 'yield',
)
_TS_KEYWORDS = _JS_KEYWORDS + (
    'abstract', 'any', 'as', 'asserts', 'boolean', 'declare', 'enum', 'implements',
    'infer', 'interface', 'is', 'keyof', 'namespace', 'never', 'number', 'private',
    'protected', 'public', 'readonly', 'satisfies', 'string', 'type', 'unknown',
)
_JS_BUILTINS = (
    'Array', 'Boolean', 'Date', 'Error', 'Infinity', 'JSON', 'Map', 'Math', 'NaN',
    'Number', 'Object', 'Promise', 'RegExp', 'Set', 'String', 'console', 'document',
    'undefined', 'window',
)
_SHELL_KEYWORDS = (
    'case', 'do', 'done', 'elif', 'else', 'esac', 'fi', 'for', 'function', 'if',
    'in', 'select', 'then', 'until', 'while',
)
_SHELL_BUILTINS = (
    'alias', 'break', 'cd', 'continue', 'echo', 'eval', 'exec', 'exit', 'export',
    'local', 'printf', 'pwd', 'read', 'return', 'set', 'shift', 'source', 'test',
    'trap', 'type', 'umask', 'unset', 'wait',
)
_SQL_KEYWORDS = (
    'add', 'all', 'alter', 'and', 'as', 'asc', 'begin', 'between', 'by', 'case',
    'commit', 'constraint', 'create', 'delete', 'desc', 'distinct', 'drop', 'else',
    'end', 'exists', 'foreign', 'from', 'group', 'having', 'in', 'index', 'inner',
    'insert', 'into', 'is', 'join', 'key', 'left', 'like', 'limit', 'not', 'null',
    'on', 'or', 'order', 'outer', 'primary', 'references', 'right', 'rollback',
    'select', 'set', 'table', 'then', 'union', 'update', 'values', 'when', 'where',
)
_SQL_BUILTINS = ('avg', 'count', 'max', 'min', 'sum')
_RUST_KEYWORDS = (
    'as', 'async', 'await', 'break', 'const', 'continue', 'crate', 'dyn', 'else',
    'enum', 'extern', 'false', 'fn', 'for', 'if', 'impl', 'in', 'let', 'loop',
    'match', 'mod', 'move', 'mut', 'pub', 'ref', 'return', 'self', 'static',
    'struct', 'super', 'trait', 'true', 'type', 'unsafe', 'use', 'where', 'while',
)
_RUST_BUILTINS = (
    'Some', 'None', 'Ok', 'Err', 'Option', 'Result', 'String', 'Vec', 'Box', 'bool',
    'char', 'f32', 'f64', 'i32', 'i64', 'isize', 'str', 'u32', 'u64', 'usize',
    'println', 'vec',
)
_GO_KEYWORDS = (
    'break', 'case', 'chan', 'const', 'continue', 'default', 'defer', 'else',
    'fallthrough', 'for', 'func', 'go', 'goto', 'if', 'import', 'interface', 'map',
    'package', 'range', 'return', 'select', 'struct', 'switch', 'type', 'var',
)
_GO_BUILTINS = (
    'append', 'bool', 'byte', 'cap', 'close', 'complex', 'copy', 'delete', 'error',
    'false', 'imag', 'int', 'len', 'make', 'new', 'nil', 'panic', 'print', 'println',
    'real', 'recover', 'rune', 'string', 'true', 'uint',
)
_C_KEYWORDS = (
    'auto', 'break', 'case', 'char', 'const', 'continue', 'default', 'do', 'double',
    'else', 'enum', 'extern', 'float', 'for', 'goto', 'if', 'inline', 'int', 'long',
    'register', 'restrict', 'return', 'short', 'signed', 'sizeof', 'static',
    'struct', 'switch', 'typedef', 'union', 'unsigned', 'void', 'volatile', 'while',
)
_CPP_KEYWORDS = _C_KEYWORDS + (
    'bool', 'class', 'constexpr', 'delete', 'explicit', 'false', 'friend', 'mutable',
    'namespace', 'new', 'noexcept', 'nullptr', 'operator', 'override', 'private',
    'protected', 'public', 'template', 'this', 'throw', 'true', 'try', 'typename',
    'using', 'virtual', 'wchar_t',
)
_C_BUILTINS = (
    'NULL', 'EOF', 'free', 'int32_t', 'malloc', 'printf', 'scanf', 'size_t',
    'sizeof', 'stderr', 'stdin', 'stdout', 'uint32_t',
)
_CPP_BUILTINS = _C_BUILTINS + (
    'cout', 'cin', 'endl', 'string', 'vector', 'map', 'std',
)
_CSS_PROPERTIES = (
    'align-items', 'animation', 'background', 'background-color', 'border',
    'border-radius', 'bottom', 'box-shadow', 'color', 'content', 'cursor',
    'display', 'flex', 'flex-direction', 'float', 'font', 'font-family',
    'font-size', 'font-weight', 'gap', 'grid', 'grid-template-columns', 'height',
    'justify-content', 'left', 'line-height', 'list-style', 'margin',
    'margin-bottom', 'margin-left', 'margin-right', 'margin-top', 'max-height',
    'max-width', 'min-height', 'min-width', 'opacity', 'outline', 'overflow',
    'padding', 'padding-bottom', 'padding-left', 'padding-right', 'padding-top',
    'position', 'right', 'text-align', 'text-decoration', 'top', 'transform',
    'transition', 'vertical-align', 'white-space', 'width', 'z-index',
)
_CSS_VALUES = (
    'absolute', 'auto', 'block', 'bold', 'center', 'fixed', 'flex', 'grid',
    'hidden', 'inherit', 'initial', 'inline', 'none', 'normal', 'pointer',
    'relative', 'solid', 'sticky', 'unset',
)


def _c_family(keywords, builtins):
    return _lexer((
        ('code_comment', _BLOCK_COMMENT + '|' + _LINE_COMMENT),
        ('code_string', _QUOTED),
        ('code_keyword', r'^\s*#\s*(?:include|define|ifdef|ifndef|if|else|elif|endif|pragma|undef|error|line)\b[^\n]*'),
        ('code_number', _NUMBER),
        ('code_keyword', _words(keywords)),
        ('code_builtin', _words(builtins)),
    ))


_LEXERS = {
    'python': _lexer((
        ('code_comment', _HASH_COMMENT),
        ('code_string', _PY_STRING),
        ('code_number', _NUMBER),
        ('code_keyword', _words(_PY_KEYWORDS)),
        ('code_builtin', _words(_PY_BUILTINS)),
    )),
    'javascript': _lexer((
        ('code_comment', _BLOCK_COMMENT + '|' + _LINE_COMMENT),
        ('code_string', r'`(?:\\.|[^`])*`|' + _QUOTED),
        ('code_number', _NUMBER),
        ('code_keyword', _words(_JS_KEYWORDS)),
        ('code_builtin', _words(_JS_BUILTINS)),
    )),
    'typescript': _lexer((
        ('code_comment', _BLOCK_COMMENT + '|' + _LINE_COMMENT),
        ('code_string', r'`(?:\\.|[^`])*`|' + _QUOTED),
        ('code_number', _NUMBER),
        ('code_keyword', _words(_TS_KEYWORDS)),
        ('code_builtin', _words(_JS_BUILTINS)),
    )),
    'json': _lexer((
        ('code_string', r'"(?:\\.|[^"\\])*"'),
        ('code_number', _NUMBER),
        ('code_keyword', _words(('true', 'false', 'null'))),
    )),
    'yaml': _lexer((
        ('code_comment', _HASH_COMMENT),
        ('code_string', _QUOTED),
        ('code_builtin', r'^[ \t]*(?:-[ \t]*)?[A-Za-z_][\w.-]*(?=[ \t]*:)'),
        ('code_number', _NUMBER),
        ('code_keyword', _words(('true', 'false', 'yes', 'no', 'null', 'on', 'off'), True)),
    )),
    'shell': _lexer((
        ('code_comment', r'(?<!\$)#[^\n]*'),
        ('code_string', _QUOTED),
        ('code_builtin', r'\$\{[^}\n]+\}|\$(?:[A-Za-z_][\w]*|[#@?$!*0-9-])'),
        ('code_number', _NUMBER),
        ('code_keyword', _words(_SHELL_KEYWORDS)),
        ('code_builtin', _words(_SHELL_BUILTINS)),
    )),
    'html': _lexer((
        ('code_comment', r'<!--[\s\S]*?-->'),
        ('code_string', _QUOTED),
        ('code_keyword', r'(?i:</?[A-Za-z][\w:-]*)'),
        ('code_builtin', r'(?<![\w<])[A-Za-z_:][\w:.-]*(?=[ \t]*=)'),
    )),
    'css': _lexer((
        ('code_comment', _BLOCK_COMMENT),
        ('code_string', _QUOTED),
        ('code_keyword', r'@[A-Za-z-]+|!important'),
        ('code_number', r'#[0-9A-Fa-f]{3,8}\b|(?<![A-Za-z0-9_#])\d[\d_]*(?:\.\d+)?(?:px|em|rem|%|vh|vw|ch|ex|pt|s|ms)?(?![A-Za-z0-9_])'),
        ('code_builtin', _words(_CSS_PROPERTIES)),
        ('code_keyword', _words(_CSS_VALUES)),
    )),
    'sql': _lexer((
        ('code_comment', _BLOCK_COMMENT + r'|--[^\n]*'),
        ('code_string', r"'(?:''|[^'])*'|" + r'"(?:\\.|[^"\\])*"'),
        ('code_number', _NUMBER),
        ('code_keyword', _words(_SQL_KEYWORDS, True)),
        ('code_builtin', _words(_SQL_BUILTINS, True)),
    )),
    'rust': _lexer((
        ('code_comment', _BLOCK_COMMENT + '|' + _LINE_COMMENT),
        ('code_string', r'r#*"[\s\S]*?"#*|' + _QUOTED),
        ('code_number', _NUMBER),
        ('code_keyword', _words(_RUST_KEYWORDS)),
        ('code_builtin', _words(_RUST_BUILTINS)),
    )),
    'go': _lexer((
        ('code_comment', _BLOCK_COMMENT + '|' + _LINE_COMMENT),
        ('code_string', r'`[^`]*`|' + _QUOTED),
        ('code_number', _NUMBER),
        ('code_keyword', _words(_GO_KEYWORDS)),
        ('code_builtin', _words(_GO_BUILTINS)),
    )),
    'c': _c_family(_C_KEYWORDS, _C_BUILTINS),
    'cpp': _c_family(_CPP_KEYWORDS, _CPP_BUILTINS),
    'markdown': _lexer((
        ('code_comment', r'<!--[\s\S]*?-->'),
        ('code_keyword', r'^[ ]{0,3}#{1,6}(?=\s|$)'),
        ('code_string', r'^[ ]{0,3}(?:```+|~~~+).*|`+[^`\n]*`+'),
        ('code_builtin', r'!\[[^\]]*\]\([^)\n]+\)|\[[^\]]+\]\([^)\n]+\)'),
    )),
}

_GENERIC = _lexer((
    ('code_comment', _BLOCK_COMMENT + '|' + _LINE_COMMENT + '|' + _HASH_COMMENT),
    ('code_string', _PY_STRING),
    ('code_number', _NUMBER),
))


def canonical_language(language):
    """Return the highlighter name for a fence info string."""
    raw = (language or '').strip().lower()
    if not raw:
        return ''
    name = raw.split(None, 1)[0].split(',', 1)[0]
    return _ALIASES.get(name, name)


def highlight_code(language, source):
    """Tokenize ``source`` for ``language``. Offsets are into ``source``."""
    if not source:
        return []
    name = canonical_language(language)
    return _scan(_LEXERS.get(name, _GENERIC), source)


def _xml_escape(text):
    return (text.replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def code_markup(source, language, colors):
    """Pango markup for ``source`` using palette entries named like token styles."""
    parts = []
    cursor = 0
    for token in highlight_code(language, source):
        if token.start < cursor or token.end > len(source) or token.style not in colors:
            continue
        parts.append(_xml_escape(source[cursor:token.start]))
        parts.append(f'<span foreground="{_xml_escape(colors[token.style])}">')
        parts.append(_xml_escape(source[token.start:token.end]))
        parts.append('</span>')
        cursor = token.end
    parts.append(_xml_escape(source[cursor:]))
    return ''.join(parts)
