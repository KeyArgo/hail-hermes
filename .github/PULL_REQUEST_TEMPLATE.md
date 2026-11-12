## Summary

What changed and why.

## Tests run

- [ ] `bash scripts/test.sh`
- [ ] `cd wear && ./gradlew :app:assembleDebug :app:testDebugUnitTest` (if the watch changed)
- [ ] `cd android && ./gradlew :app:assembleDebug :app:testDebugUnitTest` (if the phone client changed)

Include the exact summary lines, not just "tests pass".

## Checklist

- [ ] `protocol/PROTOCOL.md` is unchanged, or the protocol change is called out and both clients
      implement it.
- [ ] `desktop/plugin.js` stays byte-identical to `plugin/hail_hermes/desktop/plugin.js`.
- [ ] No credentials, tokens, personal paths or `BUILD_INFO.json` are committed.
- [ ] Docs are updated where behaviour changed.
