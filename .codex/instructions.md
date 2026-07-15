# Codex instructions for doc-store

Read `AGENTS.md`, then follow the referenced project profile, project context,
native-role prompts, lazy tool-routing manifest, and orchestration contract.

The critical routing split is mandatory:

- Code Analysis Server through MCP Proxy first for project search, preview, AST,
  usage, dependency, and quality analysis;
- local `apply_patch` only for project content editing;
- local Git and `exec_command` with explicit workdir for tests, builds, and
  project execution;
- Plan Manager through MCP Proxy for all normative plan operations;
- never Code Analysis Server, MCP Proxy, AI Editor, or a remote editor for
  project mutation;
- treat exact `plan_export` copies under `docs/plans` as non-normative reference
  files; never reconstruct or edit them.
