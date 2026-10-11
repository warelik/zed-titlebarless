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

Or Settings → Title Bar. Nothing is removed — it's re-composed:

- the title bar contents (project and branch pickers, collab, user and app menus) move into the status bar, and their popovers open upward;
- the freed row goes to the editor;
- the traffic lights get their own room in the top-left corner (threads sidebar, left dock or the first tab bar), so nothing overlaps;
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

- The threads sidebar minimum width is halved (200 → 100 px).
- Auto-update comes from this repository's releases. SSH remoting still downloads the official remote server, since the version matches upstream.

### Optional density settings

All off by default; each is a separate patch in [`patches/`](patches).

```json
{
  "tabs": { "min_width": 120, "max_width": 200 },
  "toolbar": { "compact": true },
  "agent": { "threads_sidebar": { "compact": true } },
  "project_panel": { "sticky_root_tab_height": true }
}
```

- `tabs.min_width` / `tabs.max_width` — clamp tab width in px; long titles are truncated in the middle.
- `toolbar.compact` — the breadcrumbs toolbar gets the height of the tab bar.
- `agent.threads_sidebar.compact` — each thread is one row at the tab bar height, metadata on the title line.
- `project_panel.sticky_root_tab_height` — the topmost sticky folder gets the height of the tab bar.

Other changes will come later, if upstream rejects them.

## Install

1. Open `Zed-aarch64.dmg` and drag Zed to Applications. It replaces the official Zed.app: same bundle id, same settings and data.
2. The build is ad-hoc signed and not notarized, so remove the quarantine flag once:

   ```sh
   xattr -dr com.apple.quarantine /Applications/Zed.app
   ```

## How it's built

- [`patches/`](patches) holds the changes on top of upstream stable.
- [`.github/workflows/release.yml`](.github/workflows/release.yml) checks for a new upstream stable release every 6 hours, applies the patches, builds the DMG on a GitHub macOS runner and publishes a release with `latest.json` for auto-update.
- If a patch stops applying, the workflow fails. Regenerate the patches against the new tag and push.
