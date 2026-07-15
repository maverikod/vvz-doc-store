# Agent entry points

This directory contains project-specific context for coding agents.

- `codex_project_prompt.md` defines the current Codex authority split and
  doc-store architecture.
- `claude_project_prompt.md` remains the Claude-specific entry point.

For Codex, `AGENTS.md` and `PROJECT_PROFILE.yaml` are authoritative. Normative
plan truth lives in Plan Manager through MCP Proxy. Project search and analysis
use Code Analysis Server first; project edits remain local `apply_patch` only.
Only the two unchanged Plan Manager export files may exist under `docs/plans`;
they are non-authoritative references.
