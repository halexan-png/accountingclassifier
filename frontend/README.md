# frontend/ — G&A Classifier UI (no-build vanilla JS)

Plain ES modules, no bundler/transpiler, no npm. Served as static files by
`gna_server` (or any static file server for standalone preview).

```
index.html        single page shell
app.js            view store + render dispatch + event delegation + adapter wiring
mock-adapter.js    Wave 1B's fake backend (scripted SSE run) — swapped for a
                   real adapter in Wave 3; every screen is written against
                   the SAME interface (see mock-adapter.js's header comment),
                   so that swap should not touch any screen file
styles.css         design tokens + components, lifted from tempUI/Odyssey.dc.html
screens/*.js       one module per screen/modal (icons.js/markdown.js are shared helpers)
fonts/             Archivo variable woff2 (bundled, no CDN) + OFL.txt license
gnl-logo-front.png, gnl-logo-stacked.png   logo assets used by the hero/header
```

## Running the app

Normal use is via the repo-root launcher (`launch_ui.ps1`), which starts
`gna_server` on loopback and opens the browser. `app.js` imports
`./real-adapter.js`, so the served app talks to the real backend.

### Offline preview (mock data, no backend)

`mock-adapter.js` is kept as the adapter-contract reference and an offline-dev
path, but it is **no longer the default** — a plain static serve would load
`real-adapter.js` and hang without a server. To preview against the scripted
mock data, first repoint the one import in `app.js`:

```
// app.js
import { adapter } from './mock-adapter.js';   // was: './real-adapter.js'
```

then serve statically and open http://127.0.0.1:8000/ :

```
cd frontend
python -m http.server 8000
```

Every screen is then reachable and interactive with no `gna_server`, `.env`,
or real workbook. Revert the import before shipping.

## Design system

Source of truth is `tempUI/Odyssey.dc.html` (the operator's hand-built
design comp) — colors, spacing, radii, and `@keyframes` in `styles.css` are
lifted from it, not reinvented. See that file's `<style>` block and the v2
UI handoff (`handoff/HANDOFF_2026-07-17_ui_build_orchestrator_v2.md`) §3 for
the full token rationale.
