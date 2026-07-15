# Planning standards compatibility entry

The reference methodology is installed under `docs/standards/planning/`.
Operational plan truth for this repository is not stored in this checkout: it
is the registered `doc-store` plan in Plan Manager through MCP Proxy.

Use the local standards only as methodology. Obtain HRS, MRS, GS, TS, AS,
statuses, dependencies, cascade state, validation, scoring, context blocks, and
prompt chains from live Plan Manager. The obsolete local plan cascade was
removed. Do not recreate it unless the user explicitly authorizes a separate
synchronization task.

Recommended authoring order for doc-store:

1. Create every G from HRS/MRS without creating T or A.
2. Verify HRS/MRS coverage, duplicate ownership, responsibility boundaries,
   dependencies, execution order, and parallel-development opportunities.
3. Snapshot the G level.
4. Select the next ready G branch by dependency graph.
5. Create every T for that G and verify that the T set fully reproduces the G.
6. Immediately decompose each T to A.
7. Verify that each A has exactly one target file, concrete inputs and outputs,
   dependencies, a test or completion criterion, and no architectural decision.
8. Snapshot the completed G branch before moving to the next ready G.

Formula: horizontal across G, then vertical T -> A inside each ready G branch.
