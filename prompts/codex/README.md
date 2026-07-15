# Generic Codex prompt bundle — local variant

This installation is derived from the local native-role variant and resolved by
`PROJECT_PROFILE.yaml`; treat `AGENTS.md` as the orchestration entry point. Native roles live under `roles/`; reusable
command blocks under `command-blocks/`; lazy routing under `../tool-routing/`; planning
standards under `../../docs/standards/planning/`.

Model selection is unavailable and never required. Roles are prompt-assigned duties.
Only the root communicates with the user. Project search and analysis use the
incorporated CAS proxy cards; project editing remains local `apply_patch` only;
plan operations use Plan Manager through MCP Proxy.
