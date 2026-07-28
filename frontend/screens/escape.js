// escape.js — the single-source HTML escaper for interpolating any value that
// did NOT originate as a trusted in-code literal into an innerHTML template
// string: server-echoed or client-side filenames, LLM/pipeline-authored row
// text, operator-typed doctrine/context, error messages. Route every such
// value through escapeHtml.
//
// Why this matters even in a single-operator loopback app: (1) robustness — a
// legitimate `&` or `<` in a filename or a doctrine would otherwise corrupt
// the render; (2) portability — on Linux/macOS a filename can legally contain
// `<script>`, so an unescaped filename sink is a real DOM-injection path once
// the repo is cloned off Windows. One escaper, used everywhere, so no screen
// re-invents a partial version.
export function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
