Enable with `"title_bar_to_status_bar": true` or in Settings → Title Bar: the title bar contents move into the status bar and the empty row goes back to the editor ([upstream PR](https://github.com/zed-industries/zed/pull/65471)). This build also halves the threads sidebar minimum width.

## Install

1. Open `Zed-aarch64.dmg` and drag Zed to Applications. It replaces the official Zed.app: same bundle id, same settings and data.
2. The build is ad-hoc signed and not notarized, so remove the quarantine flag once:

   ```sh
   xattr -dr com.apple.quarantine /Applications/Zed.app
   ```

Later versions arrive through Zed's auto-update from this repository's releases. The version number matches upstream, so SSH remoting keeps downloading the official remote server.

## Known limitations

- Not notarized, so universal links (`zed://` from a browser) may not work.
- Very narrow top-left pane: the room reserved for the traffic lights squeezes its tabs out sooner.
