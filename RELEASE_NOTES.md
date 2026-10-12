Enable with `"title_bar_to_status_bar": true` in `settings.json` (macOS only; there is no Settings UI entry): the title bar contents move into the status bar and the empty row goes back to the editor ([upstream PR](https://github.com/zed-industries/zed/pull/65471)). Further opt-in settings (tab width bounds and chips, compact toolbar, panel rows and threads sidebar at the tab bar height, shadow toggles): `tabs.min_width`, `tabs.max_width`, `tab_bar.pinned_tab_chips`, `tab_bar.unpinned_tab_chips`, `toolbar.compact`, `panel_rows_tab_height`, `agent.threads_sidebar.compact`, `shadows.project_panel` and `shadows.minimap`. All of them default to stock Zed; see the [README](https://github.com/warelik/zed-titlebarless#also-in-this-build) for what each does. Without any setting this build also halves the threads sidebar minimum width (200 → 100 px), reserves one em instead of two after the text in single-line editors and bundles the Mono Light theme (not selected by default).

## Install

1. Open `Zed-aarch64.dmg` and drag Zed to Applications. It replaces the official Zed.app: same bundle id, same settings and data.
2. The build is ad-hoc signed and not notarized, so remove the quarantine flag once:

   ```sh
   xattr -dr com.apple.quarantine /Applications/Zed.app
   ```

Later versions arrive through Zed's auto-update from this repository's releases. The version number matches upstream, so SSH remoting keeps downloading the official remote server. A rebuild of the same upstream version is not offered as an update.

## Known limitations

- Not notarized, so universal links (`zed://` from a browser) may not work.
- `title_bar_to_status_bar`: a left dock narrower than about 69 px does not cover the traffic lights, so they spill onto the first pane's tab bar. A very narrow top-left pane squeezes its tabs out sooner. In a centred layout the traffic lights sit on the blank padding. A panel docked on the left keeps its own header below the traffic-light row, so it has two header rows. Native window tabs (`use_system_window_tabs`) are untested.
- The status bar keeps its stock height, which is about 2 px less than the tab bar; no setting changes it.
- `toolbar.compact`: banners taller than one row (for example the migration and Python banners) stay content-sized, so the toolbar is then not an exact multiple of the tab bar height.
- `agent.threads_sidebar.compact` does not change the threads archive view; its rows keep their stock height.
- Labels in threads sidebar headers can sit up to 0.5 px off the centre, as in stock Zed.
- Mono Light: the scrollbar thumb has the surface colour at rest and only shows under the pointer.
