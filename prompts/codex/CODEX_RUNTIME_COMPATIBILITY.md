# Codex runtime compatibility

## Native collaboration lifecycle

Child duties are prompt-assigned. Model selection and model identity are unavailable
unless the current runtime proves otherwise. Use only these lifecycle calls:

```text
spawn_agent({task_name, fork_turns, message})
send_message({target, message})
followup_task({target, message})
wait_agent({timeout_ms})
list_agents({path_prefix?})
interrupt_agent({target})
```

Delegation and report YAML travel as plain text inside `message` or final child
payloads. Model, effort, role, permissions, and acceptance are not spawn arguments.
Only the root communicates with the user. A parent remains active until every direct
report is received, every descendant is terminal, `children.active=0`, verification
passes, and no escalation remains. A wait timeout is neutral. Interruption is not
success. If required delegation cannot be performed, return `SPAWN_UNAVAILABLE`.

## Slot-safe plan-authoring dispatch

The root itself is the HRS/MRS owner; do not consume a slot with a separate
HRS/MRS child. Plan-authoring ownership is direct: root/HRS-MRS -> G owner -> T
owner -> A author. Before admitting a sibling owner, reserve one slot for every
remaining descendant level that owner must directly spawn. Admit sibling
subtrees depth-first and serialize them when necessary. With four total slots,
the full live path is root, G, T, A.

All ancestors remain active until their descendant reports satisfy the completion
barrier. Only a terminal sibling subtree frees its slot for the next sibling.
Capacity pressure does not authorize an ancestor to bridge-spawn a descendant,
a parent to perform child work, or siblings to be mixed into one context. If the
direct owner cannot spawn after correct depth-first admission, return
`SPAWN_UNAVAILABLE`.

## Project and plan adapters

For project search, preview, and analysis, read
`prompts/tool-routing/CODEX_MCP_PROXY_ADAPTER.yaml`, resolve
`code-analysis-server-vvz`, and use project
`ff997eab-d809-4cb9-b805-9dff4df60c6d`. Use live downstream help when a prepared
card conflicts with the server. For plan operations, use the same proxy adapter
but resolve `planmgr`; live Plan Manager remains normative.

For project mutation and execution, read
`prompts/tool-routing/CODEX_NATIVE_TOOLS_ADAPTER.yaml`. Use local `apply_patch`,
`exec_command` with explicit workdir, and non-destructive `git status`/`git diff`.
Inspect immediately before edits and verify immediately after them. CAS, proxy,
and AI Editor mutation are forbidden. Do not poll an id that was not returned.
