# Local project execution

Use exec_command in the repository workspace. A process is successful only after completion, exit code, and output are checked; poll only a returned session id.

## Exact native workflow

- Use CAS first for discovery and analysis that precedes execution.
- Use local bounded reads only for execution inputs already identified exactly.
- Before mutation, reread current disk content and inspect `git status --short`.
- Mutate content only with `apply_patch`; file lifecycle uses scoped native shell operations.
- After mutation, perform an independent full reread and inspect `git diff`.
- Run targeted project-native lint, type, compile, build, and tests with `exec_command`.
- Use `write_stdin` only when `exec_command` returned a real session id.
- Success requires completion, checked exit code, inspected output, and no unexplained dirty paths.
- Invalid arguments use the exposed schema and one corrected retry; unavailable tools are reported.
- Destructive actions require explicit current authorization, preview, and backup when feasible.
