# Plan Manager execution through MCP Proxy

Require a committed Plan Manager revision and green `plan_validate` result for
the same scope. Use `graph_order` or `graph_parallel_map` for dependency order,
`plan_prompt_chain` for the deterministic execution corpus, and `branch_prompt`
for one branch. Dispatch by plan level, serialize same-file AS work, and keep
runtime reports distinct from authoritative lifecycle status.

AS research uses Code Analysis Server first. AS implementation uses local
`apply_patch` only; do not use Code Analysis Server or any proxy editor to
mutate project files.
