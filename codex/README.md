# Codex project prompt template

This archive provides a thin-core, lazy-loaded orchestration contract for Codex.
Place `AGENTS.md` at the project root and keep `modes.yaml`, `roles/`, `servers/`,
and `ops/` together under `codex/`.

Codex has no prompt-file import directive. The root reads `roles/common.yaml`,
`roles/laws.yaml`, and `roles/orchestrator.yaml` explicitly. Every child reads
the common files plus its own role file before acting.

## Required substitutions

- `doc-store`: project name
- `ff997eab-d809-4cb9-b805-9dff4df60c6d`: registered CAS project UUID
- `/home/vasilyvz/projects/tools/doc-store`: local checkout path
- `code-analysis-server-vvz`, `ai-editor-server-vvz`, `mcp-terminal-vvz`,
  `planmgr`: live MCP Proxy registrations
- `the user-authorized deployment target verified for the current task`: authorized deployment target
- `the canonical build/release entrypoint discovered from current project configuration`: canonical build/release entrypoint
- `the canonical real-server acceptance pipeline discovered from current project configuration`: the single real-server acceptance pipeline
- `the authoritative version source discovered from current project configuration`: authoritative version file; add further lockstep files
  to `ops/delivery-release.yaml` when required
- `this project's own live doc-store database on the deployment target`: the part
  of the deployment target a bugfix-cycle deploy (`ops/delivery-release.yaml`
  `deploy_target_excluded_scope`) must never touch even though the rest of the
  host is a test target

## Model selection

For every child task, select the cheapest configured tier demonstrably capable
of the assessed complexity, context breadth, ambiguity, impact, recovery risk,
tool demands, and verification burden. Record the choice in the canonical
`requested_model: {model, reasoning_effort}` mapping and record a
`constraints.must` item beginning `model capability rationale:`.

Use `gpt-5.6-terra` with medium reasoning for bounded atomic work, `gpt-5.6-terra`
with xhigh reasoning for medium ownership and verification, `gpt-5.6-sol` with
max reasoning for strong promotion, and `gpt-5.6-sol` with ultra reasoning for
root ownership, architectural conscience, or exceptional risk. Role-file
`capability_reference` annotations are selection
inputs, not fixed defaults. Never silently substitute an explicitly requested
model or choose an underpowered tier without capability evidence. Every
non-leaf parent owns context formation, child dispatch, upward escalation, and
the complete descendant barrier.

## Acceptance laws

Use exactly one mode per branch: `plan_authoring`, `plan_execution`, or
`refactor_repair`. Treat child reports as untrusted claims. Verify artifacts,
tests, live behavior, and authoritative server state independently.

Long operations may validly enter a queue. Configure adapter clients explicitly
for synchronous poll-and-unwrap or asynchronous/message handling. Queue handoff
alone is not a defect.

Keep one real-server pipeline and extend it with regression scenarios. Build and
verify from the active working branch. Merge into transfer-only `main` only after
production acceptance. The agent reports the ready commit and waits for the user
to push before synchronizing the opposite site. Delivery mechanics may be
delegated to `codex/roles/deliverer.yaml` under an explicit orchestrator delivery
decision — the deliverer never decides whether or where to deploy/repair, it only
executes the mandated procedure.

**Build execution locus (HARD RULE):** whatever the discovered build/release
entrypoint is (`ops/delivery-release.yaml` `build`), it always runs on the LOCAL
host from the LOCAL checkout via local shell — never through MCP Proxy or the MCP
Terminal sandbox/host-exec path. Only the deploy step touches the discovered
deployment target. See `codex/roles/laws.yaml` `local_mode` / `host_execution` and
`codex/ops/delivery-release.yaml` `build_execution`.

## Validation

Parse all YAML, verify every referenced package file exists, and ensure no
template substitution token remains. Confirm live server IDs and command schemas
through MCP Proxy before first use.
