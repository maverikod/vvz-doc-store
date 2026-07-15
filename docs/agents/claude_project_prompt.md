# Claude project context: doc-store

Normative plan truth is the registered `doc-store` plan in Plan Manager through
MCP Proxy. Local `plan_export` files, when present, are unchanged reference
copies only; do not use them as current plan truth.

Project search and analysis use Code Analysis Server first; project mutation
uses local `apply_patch` only. Consult the Claude-specific entry instructions
for its runtime orchestration rules and preserve `PROJECT_PROFILE.yaml`.
