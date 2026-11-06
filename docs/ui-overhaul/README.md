# UI Overhaul

Libre Vault uses a warm writing surface framed by sage navigation. The light and dark palettes are designed separately; Lights Out keeps its pure-black surfaces. A serif document title, smaller section headings, clear selected states, and quiet toolbars give notes a stronger hierarchy without changing their Markdown.

The overhaul also fixes the file tree sliding outside its panel when the window shrinks. Reading mode uses normal bullets and numbered lists, including nested lists and non-default starting numbers, instead of boxing every item.

## Screenshots

These are real GTK widget captures from a disposable vault, not mockups. The capture script opens the welcome screen, a note in all three themes, reading mode, commands, settings, and a compact window. Review images are generated into `artifacts/ui-overhaul`.

```sh
./check.sh --script scripts/capture_ui.py
```

The usual runner uses a background Hyprland workspace or a private Xvfb display. On a host with a Broadway-enabled GTK build, `./check.sh --broadway --script scripts/capture_ui.py` uses an isolated headless GTK daemon. It binds its HTTP interface to a private Unix socket, uses a loopback display connection, and stops the daemon after the probe. No browser or active desktop is used. Stock macOS Homebrew GTK does not include this backend; the review was captured with a temporary GTK 3.24.52 Broadway build.

New workspaces default to 12pt note text in a 680px writing column; stored preferences are preserved. Typography still respects the 11pt minimum and custom editor fonts. The default sans preset uses an editorial serif title in live preview. Body text and section headings retain the selected editor font.

## Validation

The first validation pass ran 524 tests through the private Broadway display: 515 passed, one skipped for unavailable language fonts, and eight failed or errored. All eight also fail on the unchanged base revision in the same environment: three filesystem-monitor checks, the trusted-plugin opt-in cancel check, the launcher interpreter/dependency check, the narrow table dialog check, case-sensitive attachment resolution on this case-insensitive filesystem, and the custom-font chooser check. The base additionally fails two compact-panel checks that this overhaul fixes.

The focused layout, command, preference, reading, memory, navigation, and test-runner checks pass. The GUI smoke test passes. Native macOS and Linux X11 presentation still need platform validation; these captures validate actual GTK layout and painting on the headless Broadway backend.

## Council review

Round 1: GPT Sol 8.2/10, Gemini 4 Argon 8.1/10, Grok 4.7 7.5/10. The reviewers asked for a compact details selector, clearer typography and text measure, consistent callouts, more useful command results, and preservation of custom fonts and dock controls. Those changes are implemented; fresh review is pending.

## Gallery

![Light workspace](workspace-light.png)
![Dark workspace](workspace-dark.png)
![Reading mode](reading-light.png)
![Compact window](compact-light.png)
![Commands](commands-light.png)
![Settings](settings-light.png)
![Welcome](welcome-dark.png)
![Lights Out](workspace-lights-out.png)
