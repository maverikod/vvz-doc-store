# Host emergency

Ordinary repository commands stay inside the configured workspace. Host-system actions require explicit current target, purpose, command, and effect authorization.

## Exact native workflow

- Discover files with `rg --files`; search content/usages/imports with bounded `rg` patterns.
- Read a known exact path with a native bounded read; never invent node or session identifiers.
- Prove project-native analyzers with project config or `command -v`; otherwise report the gap.
- Before mutation, reread current disk content and inspect `git status --short`.
- Mutate content only with `apply_patch`; file lifecycle uses scoped native shell operations.
- After mutation, perform an independent full reread and inspect `git diff`.
- Run targeted project-native lint, type, compile, build, and tests with `exec_command`.
- Use `write_stdin` only when `exec_command` returned a real session id.
- Success requires completion, checked exit code, inspected output, and no unexplained dirty paths.
- Invalid arguments use the exposed schema and one corrected retry; unavailable tools are reported.
- Destructive actions require explicit current authorization, preview, and backup when feasible.
