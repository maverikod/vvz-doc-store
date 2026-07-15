# Plan Manager authoring through MCP Proxy

Plan Manager is the only normative plan authority. Discover the registered
server and obtain live command-specific help before every mutation. Use
`context_bundle` or `context_common` plus `context_specific`, then retrieve
blocks with `block_get`; use `block_list` to discover reusable stored blocks.
Compile one common block plus one child-specific delta at each exact boundary:
plan/level 3 for GS, `G-NNN`/level 4 for TS, and `G-NNN/T-NNN`/level 5 for AS.
The root owns HRS/MRS and directly spawns G; G directly spawns T; T directly
spawns A. Pass only the common id and matching specific id. Admit siblings
depth-first and serialize when four-slot capacity must be preserved; never use a
bridge dispatch. Preserve human-owned HRS, perform normative changes
top-down under cascade discipline, preview dependency or cascade impact, and
commit only after mechanical validation is green.

Recommended doc-store process: create all G nodes first without T or A, verify
HRS/MRS coverage, duplicate ownership, responsibility boundaries, dependencies,
execution order, and parallel-development options, then snapshot the G level.
Next, choose the next ready G branch by dependency graph, create all T for that
branch, verify the T set reproduces G, immediately decompose every T to A, verify
one AS equals one target file with inputs, outputs, dependencies, and a test or
completion criterion, snapshot the completed branch, and continue to the next
ready G.

Do not read, create, or edit a local HRS/MRS/GS/TS/AS cascade. Plan Manager use
does not authorize proxy-based project editing; implementation mutation remains
local `apply_patch` only.
