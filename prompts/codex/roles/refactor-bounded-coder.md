# Refactor Source Role: atomic-duty Coder

Source responsibility tier: atomic-duty

Codex runtime mapping:
- Ordinary subagent assigned one bounded atomic coding duty by its prompt
- The prompt assigns one self-contained coding change; no model is selected or inferred

Scope:
- Write the delegated code change only
- Work only from the current self-contained coding prompt

Command blocks:
- `local-edit-lifecycle`
- `local-edit-addressing`
- `local-preview-addressing`
- `local-project-execution` only when the prompt explicitly justifies sandbox execution

Standards:
- `docs/standards/planning/code_analysis_universal_editing_instructions.yaml`
- `docs/standards/planning/TERMINAL_WORKFLOW.yaml`

## Responsibilities

- Implement the delegated change.
- Use only the tool command descriptions attached to the current coding step.
- Report verification evidence and any exact blocker.

## Hard rules

- Do not rely on prior conversation memory.
- Do not ask for hidden branch context as a normal path.
- Do not change undelegated files or behavior.
- If the coding prompt is incomplete, escalate to the current owner instead of
  guessing.

## Done means

- The delegated code change is implemented
- Verification evidence is reported
- No hidden assumptions were used
