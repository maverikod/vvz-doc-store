# Codex project prompt - doc-store

Read actual standards moved from planning first:
1. docs/standards/originals/planning_README.md
2. docs/standards/originals/terminal_workflow.yaml
3. docs/standards/originals/atomic_step_creation_standard.yaml
4. docs/standards/originals/code_analysis_fs_instructions.yaml
5. docs/standards/originals/code_analysis_search_instructions.yaml
6. docs/standards/originals/code_analysis_universal_editing_instructions.yaml
7. docs/standards/originals/editor_ca_workflow_prompt.yaml
8. docs/standards/originals/hrs_mrs_gs_consistency_verification_standard.yaml
9. docs/standards/originals/metadatastd.yaml
10. docs/standards/originals/plan_standard_machine.yaml
11. docs/standards/originals/tactical_step_creation_standard.yaml

Then read project structure and plan cascade:
1. docs/architecture/file_structure.md
2. docs/plans/doc-store/source_spec.md
3. docs/plans/doc-store/spec.yaml
4. docs/plans/doc-store/G-*/README.yaml
5. docs/plans/doc-store/G-*/T-*/README.yaml

OpenAI model analogs:
- frontier reasoning: GPT-5.5
- balanced coding: GPT-5.4
- fast checks: GPT-5.4 mini or GPT-5.4 nano
- embedding models are used only inside vectorizer provider layer.

Rules: source_spec is HRS; spec.yaml is MRS; G-* is GS; T-* is TS. Use zero-trust reread. Do not bypass cascade order. AS touches exactly one code file.
