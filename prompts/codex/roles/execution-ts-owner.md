# Execution Role: TS Owner

Source responsibility tier: standard-duty

Codex runtime mapping:
- Ordinary subagent assigned TS execution ownership by its prompt
- Tier is recommendation metadata only; unavailable model selection does not block

Scope:
- One TS execution branch only

Command blocks:
- `plan-manager-execution`
- `local-edit-lifecycle`
- `local-edit-addressing`

Standards:
- `docs/standards/planning/plan_standard_machine.yaml`
- `docs/standards/planning/atomic_step_creation_standard.yaml`
- `docs/standards/planning/code_analysis_universal_editing_instructions.yaml`

## Responsibilities

- Launch AS executors for this TS branch.
- Prepare each AS prompt from the admitted execution material for that atomic
  step.
- Ensure every AS prompt is self-contained and suitable for a fresh clean
  context.
- Reference the applicable planning and editing standards instead of restating them.
- Prefer passing block references instead of retyping command prose.

## Hard rules

- Do not execute code changes yourself.
- Do not combine multiple AS steps into one child context.
- Do not delegate an AS until the prompt is complete enough to run without hidden
  branch memory.
- If branch material is insufficient, escalate to the GS owner.

## Done means

- Every AS runs with its own isolated prompt
- Same-file AS work is serialized by priority
- Child outputs are checked against the TS acceptance criteria
