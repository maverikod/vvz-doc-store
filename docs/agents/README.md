# Agent entry points

This directory contains project-specific context for coding agents.

- `claude_project_prompt.md` remains the Claude-specific entry point.

Codex starts from root `AGENTS.md` and the `codex/` prompt package.
`PROJECT_PROFILE.yaml` is retained unchanged because the Claude configuration
depends on it; it is not part of the Codex bootstrap.
