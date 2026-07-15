# Local edit addressing

Inspect the target immediately before editing. Use apply_patch with stable surrounding context and re-read after changes.

## Exact native workflow

- Use CAS search, preview, and structural analysis to locate and assess the target.
- Reread the exact local target immediately before applying the patch.
- Before mutation, reread current disk content and inspect `git status --short`.
- Mutate content only with `apply_patch`; file lifecycle uses scoped native shell operations.
- After mutation, perform an independent full reread and inspect `git diff`.
- Run targeted project-native lint, type, compile, build, and tests with `exec_command`.
- Use `write_stdin` only when `exec_command` returned a real session id.
- Success requires completion, checked exit code, inspected output, and no unexplained dirty paths.
- Invalid arguments use the exposed schema and one corrected retry; unavailable tools are reported.
- Destructive actions require explicit current authorization, preview, and backup when feasible.
