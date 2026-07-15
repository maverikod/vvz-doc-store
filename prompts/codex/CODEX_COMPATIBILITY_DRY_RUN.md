# Local Codex compatibility dry runs

- PASS — dispatch: put `codex.delegation/v1` in `spawn_agent({task_name, fork_turns, message})`; receive a terminal child report.
- PASS — upward question: child reports to its parent; parent uses `send_message({target, message})`; child never asks the user.
- PASS — completion: use `wait_agent({timeout_ms})` and `list_agents({path_prefix?})`; timeout is neutral and every report has `children.active=0`.
- PASS — discovery: resolve the registered CAS project and run CAS cross-search first.
- PASS — search: use CAS `search`, consume/poll pages, retain evidence, and close the search session.
- PASS — detailed view: use CAS `universal_file_preview` with explicit `full_text_max_lines`; locally reread mutation targets around `apply_patch`.
- PASS — structural analysis: select the narrowest CAS AST, usage, dependency, import, hierarchy, complexity, duplication, lint, type, or quality command.
- PASS — impact preflight: combine CAS structural evidence, detailed preview, and targeted local tests before mutation.
- PASS — edit: reread disk target, call `apply_patch`, inspect `git diff`, reread independently, and run targeted checks.
- PASS — file lifecycle: validate exact paths and impact; use scoped `mkdir`, `mv`, `cp`, or `rm` only with required authorization and backup.
- PASS — plan read: read HRS/MRS/GS/TS/AS from Plan Manager through MCP Proxy and treat binding HRS as human-owned.
- PASS — plan author: apply the top-down HRS -> MRS -> GS -> TS -> AS cascade through live Plan Manager commands and cascade discipline.
- PASS — plan verify: run Plan Manager `plan_validate`, require green, then run `plan_score` against the same revision.
- PASS — plan execute: use Plan Manager graph and prompt-chain surfaces, then perform AS project edits locally.
- PASS — project run: call `exec_command` with explicit workdir; use `write_stdin` only when a real session id is returned; inspect completion, exit code, and output.
- PASS — recovery: retain exact command/cwd/arguments/output; inspect the exposed schema for invalid arguments, retry once, and never invent a tool.
