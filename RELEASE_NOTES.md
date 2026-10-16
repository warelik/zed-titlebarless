Enable with `"title_bar_to_status_bar": true` in `settings.json` (macOS only; there is no Settings UI entry): the title bar contents move into the status bar and the empty row goes back to the editor ([upstream PR](https://github.com/zed-industries/zed/pull/65471)). Further opt-in settings (tab width bounds and chips, compact toolbar, panel rows and threads sidebar at the tab bar height, shadow toggles): `tabs.min_width`, `tabs.max_width`, `tab_bar.pinned_tab_chips`, `tab_bar.unpinned_tab_chips`, `toolbar.compact`, `panel_rows_tab_height`, `agent.threads_sidebar.compact`, `agent.threads_sidebar.in_agent_panel` (threads sidebar as two tab rows in the agent panel), `shadows.project_panel` and `shadows.minimap`. All of them default to stock Zed; see the [README](https://github.com/warelik/zed-titlebarless#also-in-this-build) for what each does.

Without any setting this build also:
- Halves the threads sidebar minimum width (200 → 100 px).
- Reserves one em instead of two after the text in single-line editors.
- Bundles the Mono Light theme (not selected by default).
- Routes application logs to `--user-data-dir` on macOS when specified.
- Keeps managed agent-panel terminal threads created or restored in persistent [shpool](https://github.com/shell-pool/shpool) sessions on the host (Mac or remote server), surviving editor restarts, sleep, and SSH disconnects. Process exit keeps only these persistent threads with a Restart bar; × closes the thread and its session. Ordinary terminal tabs dragged into the agent panel remain non-persistent and close on process exit. Initial PTY sizing is held until output arrives (or a 5-second timeout) to avoid truncation. Non-interactive remote shells discover the systemd user socket even when `XDG_RUNTIME_DIR` is not exported. Login-shell attachment avoids injecting shpool's prompt sentinel or a leading blank line without changing global shell configuration. Probe errors are surfaced rather than silently falling back to a plain shell; a probe that does not complete times out after 17 seconds.
- Improves remote reconnection resilience: retries transient failures up to 10 times with exponential backoff (~65 s total) before exhausting, and automatically restarts only exited persistent terminal threads upon reconnecting. It stops retrying after a password prompt is canceled or unavailable, or after an authentication failure is verified; a retry action remains available with the actual disconnect reason.
- Fixes thread tab stability in `agent.threads_sidebar.in_agent_panel`: draft threads are preserved during pending activation, and tab ordering remains stable and deterministic across updates.


## Install

1. Open `Zed.dmg` and drag Zed to Applications. It replaces the official Zed.app: same bundle id, same settings and data.
2. The build is ad-hoc signed and not notarized, so remove the quarantine flag once:

   ```sh
   xattr -dr com.apple.quarantine /Applications/Zed.app
   ```

Later versions arrive through Zed's auto-update from this repository's releases. The version number matches upstream, so SSH remoting keeps downloading the official remote server. A rebuild of the same upstream version is not offered as an update.

## Debug symbols

Release assets attach `zed-<version>-titlebarless-debug-symbols-macos-aarch64.tar.gz` containing `zed.dwarf`, `remote_server.dwarf`, and `UUIDS.txt`. Builds are compiled with `line-tables-only` debug info, restoring source file and line number resolution for crash reports.

`zed.dwarf` corresponds to the shipped `Zed.app` binary (`remote_server.dwarf` is built during packaging but not shipped in the DMG; SSH remoting downloads the official upstream server).

To symbolicate addresses from a crash report:

```sh
# Match UUID case-insensitively (.ips reports use lowercase, minidumps drop dashes):
grep -i <uuid-from-crash> UUIDS.txt

# Symbolicate address using atos (in .ips reports, convert decimal base with printf '0x%x'):
atos -o zed.dwarf -l <load-address> <crash-address> | rustfilt
```

Or inspect in LLDB: `lldb -o "add-dsym zed.dwarf" /Applications/Zed.app`.

## Known limitations

- Not notarized, so universal links (`zed://` from a browser) may not work.
- `title_bar_to_status_bar`: a left dock narrower than about 69 px does not cover the traffic lights, so they spill onto the first pane's tab bar. A very narrow top-left pane squeezes its tabs out sooner. In a centred layout the traffic lights sit on the blank padding. A panel docked on the left keeps its own header below the traffic-light row, so it has two header rows. Native window tabs (`use_system_window_tabs`) are untested.
- The status bar keeps its stock height, which is about 2 px less than the tab bar; no setting changes it.
- `toolbar.compact`: banners taller than one row (for example the migration and Python banners) stay content-sized, so the toolbar is then not an exact multiple of the tab bar height.
- `agent.threads_sidebar.compact` does not change the threads archive view; its rows keep their stock height.
- Labels in threads sidebar headers can sit up to 0.5 px off the centre, as in stock Zed.
- Mono Light: the scrollbar thumb has the surface colour at rest and only shows under the pointer.
