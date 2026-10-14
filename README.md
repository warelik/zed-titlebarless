# zed-titlebarless

[Zed](https://github.com/zed-industries/zed) stable for macOS (Apple Silicon) with one goal: every row of the window does useful work.

Zed without the title bar: titlebarless, title-less, no titlebar, hidden or hide title bar, compact title bar — the title bar contents live in the status bar instead.

I work from a single MacBook — small display, many desktops, yabai. Cursor looks clean and compact but is slow; Zed is fast but spends space on rows that are mostly empty. This fork closes that gap with small, opt-in changes. It starts with the title bar; whatever comes next depends on what turns out to waste space.

Everything is offered upstream first. The fork carries only what upstream hasn't taken (yet).

**Download:** [latest release](https://github.com/warelik/zed-titlebarless/releases/latest)

## Title bar → status bar

```json
"title_bar_to_status_bar": true
```

Add it to `settings.json`; there is no Settings UI entry. macOS only: Windows and Linux draw their window controls in the title bar, so they ignore it (this fork releases for macOS only). Nothing is removed — it's re-composed:

- the title bar contents (project and branch pickers, collab, user and app menus) move into the status bar, and their popovers open upward;
- the freed row goes to the editor;
- the traffic lights get their own room in the top-left corner (threads sidebar, left dock or the first tab bar; a top-left pane without a tab bar and zoomed panes and panels get a row of the tab bar height), so nothing overlaps;
- that room and the empty part of tab bars drag the window.

Off by default; off means stock Zed. Upstream PR: [zed-industries/zed#65471](https://github.com/zed-industries/zed/pull/65471).

Left: stock. Right: `title_bar_to_status_bar: true`.

Threads sidebar on the left:
![Threads sidebar on the left](screenshots/1-sidebar-left.png)

Left dock closed, tabs start after the traffic lights:
![Left dock closed](screenshots/2-dock-closed.png)

Left dock open:
![Left dock open](screenshots/3-left-dock.png)

Popovers open upward from the status bar:
![Branch picker from the status bar](screenshots/4-popover.png)

## Also in this build

Without any setting:

- The threads sidebar minimum width is halved (200 → 100 px). The clamp also applies to `agent.threads_sidebar.default_width`, so its valid range now starts at 100.
- Single-line editors (such as the threads sidebar search) reserve one em instead of two after the text.
- A flat light theme, Mono Light, is bundled. It only appears in the theme picker (`"theme": "Mono Light"`); the default theme is unchanged.
- Auto-update comes from this repository's releases. SSH remoting still downloads the official remote server, since the version matches upstream.
- Agent panel terminal threads keep their shell in a [shpool](https://github.com/shell-pool/shpool) session when `shpool` is on the host's `PATH`, so they survive restarts and dropped SSH connections; a thread is closed only with its × button. Without shpool the terminal runs a plain shell as before.
- On macOS `--user-data-dir` also holds the logs, as documented, so a second instance no longer writes into the default instance's `Zed.log`.

### Optional density settings

Every setting below defaults to stock Zed. None of them has a Settings UI entry: edit `settings.json`. They are part of the patches in [`patches/`](patches).

```json
{
  "title_bar_to_status_bar": true,
  "tabs": { "min_width": 120, "max_width": 200 },
  "tab_bar": { "pinned_tab_chips": true, "unpinned_tab_chips": true },
  "toolbar": { "compact": true },
  "panel_rows_tab_height": true,
  "agent": { "threads_sidebar": { "compact": true, "in_agent_panel": true } },
  "shadows": { "project_panel": false, "minimap": false }
}
```

| Setting | Default | Effect |
| --- | --- | --- |
| `title_bar_to_status_bar` | `false` | See above. macOS only. |
| `tabs.min_width` / `tabs.max_width` | `null` | Clamp tab width in px; `null` keeps tabs as wide as their title. With a maximum, editor and terminal titles are truncated in the middle. |
| `tab_bar.pinned_tab_chips` / `tab_bar.unpinned_tab_chips` | `false` | Render the pinned or unpinned tabs as borderless chips that hug their content, as tall as the toolbar's default buttons. |
| `toolbar.compact` | `false` | The pane toolbar gets the height of the tab bar: no vertical padding, no gap above secondary rows, and search rows, errors, hints and banners are sized to the tab height, so a single-row toolbar equals the tab bar. The search bar above an agent panel terminal is compacted too. |
| `panel_rows_tab_height` | `false` | Every row in the project, git and outline panels takes the tab bar height. |
| `agent.threads_sidebar.compact` | `false` | Each thread is one row at the tab bar height; hovering a thread shows its metadata and actions instead of the title. Project group headers and the "No threads yet" row take the same height. |
| `agent.threads_sidebar.in_agent_panel` | `false` | The threads sidebar is not shown; its contents become two tab rows at the top of the agent panel, in place of the thread title: the window's projects, then the threads and terminals of the active project. The rows use the editor's tabs and tab settings (chips, width bounds, close button). Click activates, double-click renames, middle-click or × archives a thread or closes a terminal, right-click opens the sidebar's menu. Thread history is in the agent panel's ⋯ menu. |
| `shadows.project_panel` | `true` | `false` removes the shadow under the sticky folder rows of the project panel. |
| `shadows.minimap` | `true` | `false` removes the shadow around the minimap. |

See [Known limitations](RELEASE_NOTES.md#known-limitations).

Other changes will come later, if upstream rejects them.

## Install

1. Open `Zed-aarch64.dmg` and drag Zed to Applications. It replaces the official Zed.app: same bundle id, same settings and data.
2. The build is ad-hoc signed and not notarized, so remove the quarantine flag once:

   ```sh
   xattr -dr com.apple.quarantine /Applications/Zed.app
   ```

## How it's built

- [`patches/`](patches) holds the changes on top of upstream stable: one patch per commit of the fork, eleven in total, written against `v1.23.2` and applied in file name order.
- [`.github/workflows/release.yml`](.github/workflows/release.yml) checks for a new upstream stable release every 6 hours, applies the patches, builds the DMG on a GitHub macOS runner and publishes a release with `latest.json` for auto-update.
- If a patch stops applying, the workflow fails. Regenerate the patches against the new tag and push.
