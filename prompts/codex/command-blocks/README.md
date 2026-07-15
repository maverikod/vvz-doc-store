# Command Blocks

Attach only blocks required by one child. Native filesystem and Git state are
authoritative for project work. Plan Manager through MCP Proxy is authoritative
for normative plan work.

## Hybrid workflow

- Use CAS cross-search for project discovery and CAS structural commands for
  usages, imports, dependencies, and analysis.
- Use CAS universal preview for known-file research context.
- Before mutation, reread current disk content and inspect `git status --short`.
- Mutate content only with `apply_patch`; file lifecycle uses scoped native shell operations.
- After mutation, perform an independent full reread and inspect `git diff`.
- Run targeted local compile, build, and tests with `exec_command`; use CAS-first
  lint, type, and quality analysis when those checks are requested.
- Use `write_stdin` only when `exec_command` returned a real session id.
- Success requires completion, checked exit code, inspected output, and no unexplained dirty paths.
- Invalid arguments use the exposed schema and one corrected retry; unavailable tools are reported.
- Destructive actions require explicit current authorization, preview, and backup when feasible.
