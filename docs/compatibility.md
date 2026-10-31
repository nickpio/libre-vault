# Compatibility target

The first version targets Linux desktop and vault compatibility with near-identical core features. This document separates that target from the smaller implemented prototype.

The compatibility specification starts with public documentation. These are documented targets, not observations collected from an installed Obsidian binary. No behavioral parity claim has been verified against Obsidian yet.

## Documented foundations

- [How Obsidian stores data](https://obsidian.md/help/Files%2Band%2Bfolders/How%2BObsidian%2Bstores%2Bdata) describes local Markdown files, the `.obsidian` configuration folder, and derived metadata caches.
- [Internal links](https://obsidian.md/help/links) describes wikilinks, Markdown links, display text, headings, and block references.
- [Embed files](https://obsidian.md/help/embeds) describes file and note embeds.
- [Obsidian Flavored Markdown](https://obsidian.md/help/obsidian-flavored-markdown) lists supported syntax extensions.
- [JSON Canvas](https://jsoncanvas.org/) specifies the open `.canvas` file format.

## Feature coverage

| First-version target | Prototype status |
| --- | --- |
| Open an existing vault in place | Implemented for visible UTF-8 Markdown files; hidden paths and symlinks excluded |
| Preserve arbitrary frontmatter and `.obsidian` settings | Frontmatter preserved as source; `.obsidian` left untouched |
| Source editor and reading view | One formatted Markdown editor; syntax reveals on the active line |
| Obsidian-style live-preview editing | Basic headings, emphasis, code, quotes, and link labels; advanced rendering planned |
| Wikilinks and local Markdown links | Implemented subset, including display text and relative Markdown paths |
| Heading and block links | Approximate navigation; exact anchor rules planned |
| Backlinks and outgoing links | Implemented; backlinks index saved files |
| Search, quick switcher, command palette | Plain-text saved-file search and Alt+P command palette with keybind cheatsheet implemented; quick switcher planned |
| Tabs and workspace layouts | Tabs implemented; persisted layouts and split workspaces planned |
| Expandable file tree, create, rename, delete | Expandable folder tree, note creation, and subfolder creation implemented; rename and delete planned |
| Atomic writes, external-change handling, recovery history | Atomic replacement and revision checks implemented; stronger concurrency handling and history planned |
| Properties, tags, aliases, unlinked mentions | Preservation only; structured behavior planned |
| Bulleted, ordered, and task lists | Rendered with nesting and clickable task checkboxes; Markdown preserved |
| Callouts, tables, footnotes, math, diagrams | Full rendering and interactions planned |
| Attachment display and note transclusion | Images retain source syntax; note embeds show link labels; rendering planned |
| Graph and local graph | Planned |
| Canvas files | Planned using JSON Canvas |
| Templates, daily notes, bookmarks, outline | Basic outline implemented; remaining workflows planned |
| Existing plugin and theme compatibility | Outside first-version scope |
| Linux desktop distribution | Runs from source; packaging planned |
| Mobile, hosted sync, publishing | Outside initial Linux release |

## Milestones

1. **Runnable storage prototype.** Open an example vault, edit notes, navigate links, search, and verify changes on disk. This is the current deliverable.
2. **Compatibility evidence.** Create small fixtures covering syntax, filename collisions, case sensitivity, Unicode, links, embeds, and external changes. Run the same actions in a pinned Obsidian version and this app. Record differences and keep the fixture results machine-readable.
3. **Daily editing.** Choose the final editor after testing live-preview interactions. Add link completion, attachments, structured properties, recovery history, rename with link updates, folders, and incremental indexing.
4. **Core feature parity.** Implement the remaining target features with fixture-based acceptance checks, including graph, canvas, templates, daily notes, and full search behavior.
5. **Linux release.** Package the app, inventory dependency licenses, document reproducible builds, measure large-vault behavior, and exercise recovery across interruptions and competing editors.

## First-version acceptance gates

Opening a vault must preserve its files. Unsupported syntax and properties must survive editing without an automatic format conversion. A cancelled close or failed save must retain the buffer. Rename operations must preserve link meaning and expose recoverable failures. Startup, search, and editing latency need explicit budgets and measurements on representative vaults.

Exact parity requirements remain open for optional core plugins and proprietary services. Plugin execution is explicitly excluded, but data formats produced by commonly used plugins may require preservation fixtures.
