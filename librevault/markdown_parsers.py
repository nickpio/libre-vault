"""The MarkdownIt parsers, and which GitHub extensions each one reads.

Every parser starts from the commonmark preset with raw HTML off. Tables and
strikethrough are GFM extensions that the preset leaves off, so each consumer
says which it wants:

- ``vault.MARKDOWN`` (index, link extraction, organization) reads tables, so
  a table row splits at ``|`` the same way the reading view splits it. A
  wikilink that the reader cannot see, such as ``[[Note|Alias]]`` in a table
  cell, is therefore not indexed as a link. It reads no strikethrough, which
  does not change which links are found.
- ``formatting.MARKDOWN`` (live editor layout) reads neither. The editor finds
  tables with ``tables.find_tables`` and styles ``~~strike~~`` itself, so it
  needs source offsets rather than the parser's table or strikethrough tokens.
- ``anchors._parser`` (heading and block ids) reads tables, so that a heading
  or block id inside a table is seen the same way the reading view shows it.
- ``reading.MARKDOWN`` (reading view and preview) reads both, because it
  renders tables and strikethrough directly.

Rules that need other modules, such as the wikilink rule and safe inline HTML,
are added by the module that owns the parser. This file imports nothing from
the rest of the package, so that every module can use it without a cycle.
"""
from markdown_it import MarkdownIt


def new_parser(*, tables=False, strikethrough=False):
    """Return a commonmark parser with the requested GFM extensions and no raw HTML."""
    parser = MarkdownIt('commonmark', {'html': False})
    if tables:
        parser.enable('table')
    if strikethrough:
        parser.enable('strikethrough')
    return parser
