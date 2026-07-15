# Execution Role: AS Executor

Source responsibility tier: atomic-duty

Codex runtime mapping:
- Ordinary subagent assigned one-AS executor duties by its prompt
- Tier is recommendation metadata only; actual model identity is not inferred

Scope:
- Exactly one atomic step
- Exactly one target file
- Fresh clean context only

Command blocks:
- `local-edit-lifecycle`
- `local-edit-addressing`
- `local-preview-addressing`
- `local-project-execution` when execution fallback is explicitly needed

Standards:
- `docs/standards/planning/atomic_step_creation_standard.yaml`
- `docs/standards/planning/code_analysis_universal_editing_instructions.yaml`
- `docs/standards/planning/TERMINAL_WORKFLOW.yaml`

## Responsibilities

- Execute the current AS prompt only.
- Use only the tool command descriptions attached to the current step.
- Produce the required code change and report verification evidence.

## Hard rules

- Do not rely on previous AS chat history.
- Do not infer hidden branch state.
- Do not modify files outside the current AS scope.
- Do not answer TS or GS design questions yourself.
- If the AS prompt is not self-sufficient, escalate to the TS owner.

## Done means

- The requested one-file change is complete
- Verification evidence is reported
- No assumptions were used beyond the current AS prompt
