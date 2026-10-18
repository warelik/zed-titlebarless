# zed-titlebarless

[Zed](https://github.com/zed-industries/zed) stable for macOS (Apple Silicon) with one goal: every row of the window does useful work.

Zed without the title bar: titlebarless, title-less, no titlebar, hidden or hide title bar, compact title bar — the title bar contents live in the status bar instead.

This fork keeps a small set of requested layout changes and fixes for terminal persistence and remote reconnection. It follows upstream rather than developing a separate editor.

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

- The threads sidebar minimum width is halved (200 → 100 px). The clamp also applies to `agent.threads_sidebar.default_width`, so its valid range now starts at 100. This is intentional with `compact` and `in_agent_panel` disabled too; those switches control layout, not this independent width change.
- Single-line editors (such as the threads sidebar search) reserve one em instead of two after the text.
- A flat light theme, Mono Light, is bundled. It only appears in the theme picker (`"theme": "Mono Light"`); the default theme is unchanged.
- Auto-update uses this repository's immutable releases. Same-version fixes use the native GitHub `release.id` without changing Zed's upstream version; SSH remoting still downloads the official remote server. Already-installed clients learn this comparison on a one-time install or their next ordinary higher-upstream-version update.
- Managed terminal threads created or restored in the agent panel keep their shell in a [shpool](https://github.com/shell-pool/shpool) session when `shpool` is on the host's `PATH`, so they survive restarts and dropped SSH connections; closing one with its × button removes its session. Ordinary terminal tabs dragged into the agent panel remain non-persistent and close when their process exits. Without shpool, managed threads run a plain shell as before. Probe errors are surfaced instead of silently falling back to a plain shell; a probe that does not complete times out after 17 seconds.
- Manual and automatic SSH reconnect stop retrying after a password prompt is canceled or unavailable, or after an authentication failure is verified. A retry action remains available with the actual disconnect reason; transient failures continue retrying with backoff.
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

## Install

1. Open `Zed.dmg` and drag Zed to Applications. It replaces the official Zed.app: same bundle id, same settings and data.
2. The build is ad-hoc signed and not notarized, so remove the quarantine flag once:

   ```sh
   xattr -dr com.apple.quarantine /Applications/Zed.app
   ```

## How it's built

- [`patches/`](patches) holds the minimal ordered patch series on one supported upstream snapshot. The workflow selects an exact stable source SHA, applies the patches in filename order, and records the resulting patch-set digest.
- [`.github/workflows/release.yml`](.github/workflows/release.yml) first obtains two independent exact-source Codex reviews: every new or modified patch since the last shipped configuration, and the complete assembled project. Each review must return its complete finding list. Both reports are collected before one batched repair; unresolved findings carry forward. No tests, compilation or builds run during this preparation.
- After the complete repair batch and both fresh reviews are accepted, the candidate is frozen. One final sequence runs the preserved policy tests, all Rust regressions, then the macOS Apple Silicon build, artifact inspection and checksum/provenance validation for one immutable five-asset release. An authenticated receipt prevents callbacks or schedules from rebuilding that candidate. A failed final sequence blocks publication and automatic expensive retries, preserving diagnostics and the last working release.
- Compiler recipes come from the exact selected upstream SHA. Regression binaries use upstream's default test profile and parent CI Cargo configuration; the application uses its unmodified `script/bundle-mac` and release profile. No downstream LTO, codegen-unit, debug-info or debug-assertion overrides are added. The checked controller compares recipes during Linux preparation; proposed policy ports first pass the sandbox and then their checked controller compares recipes on Linux before activation and macOS. An obsolete controller cannot block its own reviewed replacement. The comparison runs again before compilation. All selected regressions compile in one profile, including selector-dependent UI cases. After compilation, selected runtime cases use an isolated process group and the selected upstream ordinary-case timeout (60 seconds); a timed-out group is killed and the remaining cases continue. Their failures are reported together; any missing, ignored or failed required case blocks the bundle and publication.
- Receipts identify both application and configuration. A corrected policy can proceed with unchanged application code only when native workflow metadata across every run attempt proves all prior macOS jobs were skipped. Once that job starts, changing configuration or consolidating a repair cannot grant another heavy attempt. Repeated callbacks of the same candidate do not rerun its failed lightweight validation. An explicit additional resource grant is an owner-authored `zed-final-retry` comment on the existing source-review PR, bound to the exact application, configuration and latest completed failed/cancelled run. Agents record it only after the owner grants that attempt; ordinary callbacks cannot grant themselves a retry.
- Proposed policy runs without credentials before adoption. Adoption must preserve the exact reviewed file blobs and may only remove repair context and extend the regression manifest. The same final run then tests and builds the application; adopting a repair does not trigger a second test/build cycle. Policy-only maintenance of an already published application validates the policy without rebuilding that application.
- Privileged preparation loads the checked controller pinned by the native repository variable `ZED_TRUSTED_POLICY`, using the candidate checkout only as data. The final credential-free policy check advances that pin before the candidate code can receive write credentials. Initial setup or controller recovery uses this workflow's `policy_bootstrap` mode with the exact policy commit and both completed review PR numbers; agents invoke it after the full finding batch is resolved. The bootstrap checks the candidate controller against an independently pinned regression baseline and the proposed suite without owner secrets, then continues the same final sequence. The baseline preserves the reviewed contracts for paired reviews, candidate-bound releases and complete repair batches. A missing trusted pin blocks ordinary privileged repair; it adds no intermediate application build or tests.
- Keep two permanent branches: `main` is an unmodified upstream mirror; `downstream` is the default branch holding patches, workflow and documentation. Native Codex repair PRs use temporary branches and are reviewed and selectively adopted by the workflow.
- Keep one active candidate, its two review PRs and at most one coding task. Superseded review PRs are closed and their temporary heads retired only after their exact commits remain available through GitHub pull refs. Release identity includes the version, upstream, patch-set digest and exact application commit.
- The preserved real-shpool regression uses the pinned official Apple Silicon shpool fixture in an isolated daemon, home and runtime directory. CI enables that case explicitly and fails if its fixture or activation is missing; it cannot count an early return as verified daemon behavior.
- The five release assets are `Zed.dmg`, `latest.json`, `provenance.json`, `SHA256SUMS`, and the versioned debug-symbol archive. Same-version fixes advance the native GitHub release ID while preserving the official Zed version and SSH remote-server compatibility.
- The release boundary rechecks both reviews, branch identity and verified artifacts immediately before and after publication. A failed post-publication check returns the candidate to draft through at most three PATCH/read-back attempts (each API call is bounded to 60 seconds), preserving its diagnostics and the previous working release. GitHub does not offer a transaction covering release, branch and review state; a concurrent invalidation can therefore cause brief visibility before withdrawal. This limitation is not an atomic-publication guarantee.
- Publication is not live installation. This workflow does not restart an editor or remote session; installation and validation of a running version are separate steps.
- [AGENTS.md](AGENTS.md) defines the maintenance, verification, recovery, and cleanup contract. The presence of a workflow does not by itself prove that the entire cycle is automated.

### Debug symbols

Each release attaches `zed-<version>-titlebarless-debug-symbols-macos-aarch64.tar.gz` containing flat DWARF symbols (`zed.dwarf`, `remote_server.dwarf`) and `UUIDS.txt`. Builds inherit the selected upstream release debug-information settings without a downstream override. The final artifact inspection requires compilation-unit debug information and matches each symbol UUID to its binary.

Only `zed.dwarf` corresponds to the shipped `Zed.app` binary. `remote_server.dwarf` is produced during packaging for the macOS remote server, but `remote_server` is not distributed in the DMG (and SSH remoting downloads upstream's official remote server), so it does not match deployed remote binaries.

To symbolicate addresses from a crash report:

```sh
# Match binary UUID from crash report against UUIDS.txt:
# (.ips reports use lowercase UUIDs with dashes; minidump IDs drop dashes.
# UUIDS.txt uses uppercase with dashes, so match case-insensitively.)
grep -i <uuid-from-crash> UUIDS.txt

# Symbolicate address using atos:
# <load-address> is the hex base address of the zed image from the crash report.
# In macOS .ips reports, "base" is decimal; convert to hex with printf '0x%x\n' <base>.
# <crash-address> is the hex instruction pointer address (base + imageOffset).
# atos outputs v0-mangled Rust names; pipe through rustfilt to demangle:
atos -o zed.dwarf -l <load-address> <crash-address> | rustfilt
```

Symbols can also be loaded into LLDB, which demangles names automatically:
```sh
lldb -o "add-dsym zed.dwarf" /Applications/Zed.app
```
