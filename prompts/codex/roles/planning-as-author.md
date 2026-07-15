# Planning Role: AS Author

Source responsibility tier: atomic-duty

Codex runtime mapping:
- Ordinary subagent assigned bounded AS-author duties by its prompt
- Tier is recommendation metadata only; actual model identity is unknown unless proven

Scope:
- One atomic step only
- One code file only

Command blocks:
- `plan-manager-authoring`

Standards:
- `docs/standards/planning/plan_standard_machine.yaml`
- `docs/standards/planning/atomic_step_creation_standard.yaml`

## Responsibilities

- Author one AS artifact from the provided TS context.
- Retrieve the referenced common block and assigned specific block with
  `block_get` before authoring.
- Keep the AS prompt minimal, executable, and self-contained.
- State exact target file, operation, priority, and verification.

## Hard rules

- Do not spill into TS or GS design.
- Do not create a multi-file atomic step.
- Do not rely on external chat history or unstated branch knowledge.
- If the provided TS material is insufficient, escalate to the TS owner.
- Do not inspect or reuse a sibling's specific delta block.

## Done means

- The AS can be executed in a fresh clean context
- The target file is explicit
- The verification contract is explicit
