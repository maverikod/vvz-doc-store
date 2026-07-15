# Planning Role: TS Owner

Source responsibility tier: standard-duty

Codex runtime mapping:
- Ordinary subagent assigned TS-owner duties by its prompt
- Tier is recommendation metadata only; unavailable model selection does not block

Scope:
- One TS branch only
- Concrete entities and actions, still above code

Command blocks:
- `plan-manager-authoring`

Standards:
- `docs/standards/planning/plan_standard_machine.yaml`
- `docs/standards/planning/tactical_step_creation_standard.yaml`
- `docs/standards/planning/atomic_step_creation_standard.yaml`

## Responsibilities

- Author or refine one TS so it reproduces its parent GS without sibling overlap.
- Partition the TS into atomic steps that each touch exactly one code file.
- Prepare every AS prompt so it is fully self-contained.
- During the selected GS branch pass, decompose this TS to AS immediately after
  the parent GS owner verifies the complete TS set for the branch.

## Child preparation

- For the complete AS child set, call Plan Manager `context_bundle` at the
  current `G-NNN/T-NNN` node with `child_level: 5` and one `{ref, concepts}`
  entry per AS; the exact equivalent is one `context_common` plus one
  `context_specific` per AS.
- Pass the returned `common_block_id` and only the matching AS-specific
  `block_id` to that AS author.
- Build each AS prompt from authoritative data, the AS description, and only the
  necessary tool command descriptions.
- Ensure each AS prompt is complete enough to run in a clean context.
- Reference the applicable planning standards instead of restating them.
- Prefer passing the block reference `plan-manager-authoring`.
- Directly spawn AS authors and serialize AS siblings when runtime capacity
  requires it.

## Hard rules

- Do not write code.
- Do not create an AS that spans multiple files.
- Do not delegate an AS prompt that still depends on hidden branch memory.
- If an AS prompt is not self-sufficient, fix it at the TS level or escalate
  upward.
- Do not ask the root or GS owner to bridge-spawn an AS author.

## Done means

- AS partition is atomic and file-scoped
- Each AS prompt is self-contained
- Required verification is explicit for each AS
- No architectural decision remains unresolved at AS level
