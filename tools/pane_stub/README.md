# pane_stub — STAND-IN, not real Hermes

A labelled browser stand-in so the Hail Hermes panel can be seen without the real Hermes host.

## Run

From the repo root:

```
python -m http.server 8799
```

Then open: http://127.0.0.1:8799/tools/pane_stub/index.html

## What you should see

- Banner: “STAND-IN, not real Hermes”.
- A status bar with the “⌚ Hail Hermes” chip (green/muted dot). Click it to open the
  popover; click **Open big** to render the full pane in the workspace area below
  (this calls `host.openWorkspace`, same shape the plugin uses).
- `alt+shift+h` toggles the popover too (the registered keybind).

## Notes

- No build step, no npm: plain ESM + importmap pointing react at esm.sh and
  `@hermes/plugin-sdk` at `./sdk-stub.mjs`.
- `sdk-stub.mjs` implements only what `plugin/hail_hermes/desktop/plugin.js` imports:
  Button, EmptyState, Popover/Content/Trigger, StatusDot, KEYBINDS_AREA,
  STATUSBAR_AREAS, usePluginI18n, host (notify/openSession/openWorkspace).
- `main.mjs` builds a fake ctx: `i18n.register`, `onDispose`, `register` (collected),
  in-memory `storage.get/set`, `setInterval`/`setTimeout` wrapping window's, and
  `rest(path)` returning canned data (`/status`, `/settings`, `/soul-templates`,
  `/soul`, `/sessions`, `/setup-check`, `/face`, …; anything else → `{ok:true}`).
- The plugin does not export `WristPane` directly, so the pane appears via the chip →
  **Open big** path, exactly as in the real panel.
