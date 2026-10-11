# zed-titlebarless

[Zed](https://github.com/zed-industries/zed) stable with the title bar moved into the status bar ([upstream PR](https://github.com/zed-industries/zed/pull/65471)). macOS, Apple Silicon.

Download: [latest release](https://github.com/warelik/zed-titlebarless/releases/latest).

## How it works

- `patches/` holds the changes on top of upstream stable.
- [`.github/workflows/release.yml`](.github/workflows/release.yml) checks for a new upstream stable release every 6 hours, applies the patches, builds the DMG on a GitHub macOS runner and publishes a release with `latest.json`.
- Builds read `latest.json` for auto-update. The remote server for SSH still comes from zed.dev.
- If a patch stops applying, the workflow fails. Regenerate the patches against the new tag and push.
