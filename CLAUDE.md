# doc-store - Claude operating contract

**Prompts template:** `claude-prompts-v1` rev **1.6.19** (2026-08-03)

This file is the Claude entrypoint and is loaded automatically at session start.
Read these files yourself before the first action of a task:

- `claude/roles/common.yaml`
- `claude/roles/laws.yaml`
- `claude/roles/tooling.yaml`
- `claude/roles/orchestrator.yaml`

Do not delegate reading or interpretation of those files to a subagent. Resolve
every relative prompt-package reference against `claude/`.

## Project profile

- Project: `doc-store`
- Local checkout: `/home/vasilyvz/projects/tools/doc-store`
- CAS project ID: `ff997eab-d809-4cb9-b805-9dff4df60c6d`
- CAS server: `code-analysis-server-vvz`

## Active work profile

`claude/roles/laws.yaml` `variables.file_access` decides where the work happens. Read it
before the first project read or edit and state the active profile in your opening
message. Default is `local`.

- `local` — local checkout on branch `local`; every script and every edit runs with
  local tools. MCP Proxy is used only for code-analysis search and CAS git sync.
- `cas` — the registered CAS project on branch `cas` through MCP Proxy; local project
  tools must not be used as a fallback.
- `local_ide` — the human edits through the IDE; you deliver patches, never edit directly.

Only the user changes this value. Never infer a switch from the task and never switch
because a tool failed.

## Operating model

Role files are stage disciplines, not a headcount. Sequence implementation-heavy work
as `researcher` -> `context_former` -> `coder` -> `tester` -> `conscience`, running the
stages serially in one session or distributing independent units across parallel agents.
Declare the working mode (`planning`, `analysis`, `refactoring`) from `claude/modes.yaml`.

## Claude specifics

- Parallel writing agents are a standard mode: `coder` and `tester` units may run as
  concurrent agents, each in its own git worktree and branch per `parallel_agent_isolation`,
  merged only on acceptance. The orchestrator keeps task framing, acceptance, and the
  `conscience` review. Read-only research agents may also fan out for search.
- A subagent prompt must restate the laws it needs; subagents do not inherit this file.
- Keep the plan visible with the task/todo tools on multi-step work, and re-read
  contract files after context compaction instead of recalling them.
- `.claude/` is Claude Code harness configuration, not part of this bundle.
- One home project owns this session — the one you were started in. Inside it you have full
  developer authority on any host, `root` on the deploy host included: that host is part of the
  development process, so deploying and restarting your own service there needs no permission.
  The same host carries production databases and other projects' live services, which is why the
  line is per project, not per host: every other project is strictly read-only everywhere, its
  data is never written, and a defect found there is registered in the plan manager, never fixed
  in place — see `laws.project_boundary`.
- This contract is `CLAUDE.md` plus `claude/`, and nothing else. Role packs left in this
  repository by earlier systems — `.cursor/agents/`, `docs/agents/`, `docs/PROJECT_RULES.md`,
  `AGENTS.md` — do not govern your work; see `laws.contract_scope`.
- Role names here are stages, not subagent types. A role that cannot be spawned is never a
  blocker: run the stage in-session — see `laws.role_names_are_stages_not_agent_types`.

Use the `claude/` bundle as the authoritative Claude contract for this project.
Do not read Codex prompt files unless the task explicitly requires cross-checking
them.
