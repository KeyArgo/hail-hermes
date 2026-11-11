# Panel check without a browser

Runs the real plugin.js in jsdom with the stand-in SDK from tools/pane_stub, clicks through every tab,
and exits 1 on any failure. It proves rendering and clicks. It does not prove the plugin loads in real
Hermes.

The harness renders both status-bar items: the `wrist-status` chip and the `version` chip. The version
chip only appears once `/v1/version` answers with a version, so a harness that skipped it would never
exercise that code — which is exactly how an undefined token in it survived every test for a week.

Dependencies are declared in `package.json` beside this file; the harness must run from the directory
that holds `node_modules`, so copy it into a scratch directory:

    mkdir pane-rt && cd pane-rt
    cp <repo>/tools/pane_jsdom/package.json .
    cp <repo>/tools/pane_jsdom/run.mjs .
    npm i
    node run.mjs <repo>
