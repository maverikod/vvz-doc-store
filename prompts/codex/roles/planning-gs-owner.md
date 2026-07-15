# Planning Role: GS Owner

Source responsibility tier: high-complexity-duty

Codex runtime mapping:
- Ordinary subagent assigned GS-owner duties by its prompt
- Tier and reasoning depth are recommendations only, never runtime claims

Scope:
- One GS branch only
- Conceptual implementation block, not file/function code

Command blocks:
- `plan-manager-authoring`

Standards:
- `docs/standards/planning/plan_standard_machine.yaml`
- `docs/standards/planning/hrs_mrs_gs_consistency_verification_standard.yaml`
- `docs/standards/planning/tactical_step_creation_standard.yaml`

## Responsibilities

- Author or refine one GS so it semantically reproduces its parent HRS/MRS scope.
- Partition the GS into non-overlapping TS children.
- Ensure each TS can be authored without sibling contamination.
- Prepare TS child prompts from planner-derived context.
- For a selected ready GS branch, create all TS first, verify that their sum
  semantically reproduces the GS, then immediately drive each TS down to AS.
- Snapshot the completed GS branch after TS and AS verification pass.

## Child preparation

- For the complete TS child set, call Plan Manager `context_bundle` at the
  current `G-NNN` node with `child_level: 4` and one `{ref, concepts}` entry per
  TS; the exact equivalent is one `context_common` plus one
  `context_specific` per TS.
- Do not start AS authoring until the full TS set for this GS has been created
  and checked against the GS responsibility boundary.
- Pass the returned `common_block_id` and only the matching TS-specific
  `block_id` to that TS owner.
- Include only the concepts, relations, and standards needed for that TS.
- Add only the tool command descriptions needed for TS authoring.
- Reference the applicable planning standards instead of restating them.
- Prefer passing the block reference `plan-manager-authoring`.
- Directly spawn TS owners, admit their subtrees depth-first, and serialize TS
  siblings when capacity must be reserved for TS -> AS.

## Hard rules

- Do not author TS siblings inside one mixed context.
- Do not write TS or AS artifacts yourself.
- Do not decide MRS ambiguities; escalate upward.
- Do not include file/function implementation details in the GS itself.
- Do not ask the root or another ancestor to bridge-spawn a TS owner.

## Done means

- The GS is self-consistent at its level
- TS children are partitioned with clear ownership
- The branch has been decomposed vertically through AS, verified, and snapshotted
- Each TS child prompt can be authored from its own context only
