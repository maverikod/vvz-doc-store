# doc-store Codex entry point

Read these files in order before project work:

1. `.codex/instructions.md`
2. `PROJECT_PROFILE.yaml`
3. `docs/agents/codex_project_prompt.md`
4. `prompts/codex/ORCHESTRATION_CONTRACT.md`

The contract is based on the local native-role bundle. This repository applies a
hybrid authority profile:

- Project search, known-file preview, structural inspection, dependency and
  usage analysis, and quality analysis use the registered Code Analysis Server
  through MCP Proxy first.
- Project content editing uses the local checkout and `apply_patch` only. Do not
  use MCP Proxy, Code Analysis Server mutation, AI Editor, or any server-side
  editor to change project files.
- Git, builds, tests, and project execution use local native commands with an
  explicit repository workdir.
- Normative plan truth is the registered `doc-store` plan in Plan Manager,
  accessed through MCP Proxy. Plan reading, authoring, validation, scoring,
  cascade handling, context compilation, prompt assembly, dependency ordering,
  and execution-state reporting use Plan Manager only.
- Plan authoring uses stored Plan Manager common and child-specific context
  blocks. The root is the HRS/MRS owner and directly dispatches a GS owner; each
  GS owner directly dispatches its TS owner, and each TS owner directly
  dispatches its AS author. Admit this chain depth-first and serialize sibling
  subtrees when the four-slot runtime requires it; bridge dispatch is forbidden.
- Recommended doc-store authoring order is horizontal across all G first, with
  HRS/MRS coverage, duplication, boundary, dependency, order, and parallelism
  checks plus a G-level snapshot; then choose the next ready G by dependency
  graph and decompose that branch vertically T -> A, verify atomic A quality, and
  snapshot the completed branch before moving to the next G.
- Local plan files, when present, may only be the two unchanged files produced
  by Plan Manager `plan_export`, preserving the returned bare filenames under
  the configured `docs/plans` export directory. They are reference exports,
  never normative truth. Do not reconstruct, merge, or edit them.

When this project overlay conflicts with a generic local-plan statement in the
base bundle, this overlay and `PROJECT_PROFILE.yaml` take precedence.

Claude entry instructions remain in `.claude/CLAUDE.md` and are outside this
Codex installation task.
