# Local detailed view

Read a known file with a native read or bounded shell output. Use exact path and bounded ranges; do not invent node identifiers.

## Exact native workflow

- Use CAS preview for ordinary project inspection.
- Use this local preview block only for exact mutation-target rereads before and after apply_patch.
- Before mutation, reread current disk content and inspect `git status --short`.
- Mutate content only with `apply_patch`; file lifecycle uses scoped native shell operations.
- After mutation, perform an independent full reread and inspect `git diff`.
- Run targeted project-native lint, type, compile, build, and tests with `exec_command`.
- Use `write_stdin` only when `exec_command` returned a real session id.
- Success requires completion, checked exit code, inspected output, and no unexplained dirty paths.
- Invalid arguments use the exposed schema and one corrected retry; unavailable tools are reported.
- Destructive actions require explicit current authorization, preview, and backup when feasible.
