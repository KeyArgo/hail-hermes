# Pane browser check

Run `./tools/pane_e2e/run.sh` with `hermes` on PATH and Node Playwright plus its Chromium browser installed. The script creates a temporary HOME and HERMES_HOME, links the current plugin as `hail-hermes`, configures the webapp on port 19120, opens its one-use link in muted headless Chromium with fake media, and stops the isolated gateway on exit. It never uses the real `~/.hermes` configuration or port 9120. Screenshots are written to `tools/pane_e2e/out/` as `chip.png`, `popover.png`, and `big-tab.png`.

The installed Hermes command and its webapp build must be available; port 19120 must be free. A running hub on 8767 is optional. The webapp log is retained in `out/` and can contain a short-lived sign-in link.
