# Roadmap

Libre Vault is a working prototype. Implemented features are tracked in the [compatibility matrix](compatibility.md#feature-coverage); they do not establish verified behavioral parity with Obsidian.

## Implemented foundations

- Open local vaults, edit Markdown, navigate links, search, and save changes to disk.
- Daily editing tools: live preview, table editing, folding, slash commands, multi-cursor editing, optional Vim mode, properties, saved-version history, and incremental indexing.
- Core tools: canvas, templates, daily notes, bases, slides, format conversion, section and block embeds, anchors, supported Obsidian Flavored Markdown, Mermaid diagrams, and math environments.
- Build standalone executables and Linux AppImages from source.

## Compatibility evidence

- [ ] Run shared fixtures against a pinned Obsidian version and record machine-readable differences.
- [ ] Cover syntax preservation, filename collisions, case sensitivity, Unicode, links, embeds, and external changes.
- [ ] Verify the [first-version acceptance gates](compatibility.md#first-version-acceptance-gates), including cancelled closes, failed saves, and recoverable rename failures.

## Linux release

- [ ] Publish an AppImage as the main download.
- [ ] Validate packaged builds across supported distributions.
- [ ] Complete the dependency license inventory and document reproducible builds.
- [ ] Set and measure startup, search, and editing latency budgets on representative large vaults.
- [ ] Exercise recovery across interruptions and competing editors.

## Other desktop platforms

- [ ] Validate native Windows and macOS GUI behavior and packaged artifacts. See [Desktop platforms](platforms.md) for setup and current limitations.

Existing Obsidian plugins and themes, hosted sync, publishing, and mobile support remain outside the initial release scope.
