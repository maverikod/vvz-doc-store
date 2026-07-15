# Code Analysis Server detailed preview

Inspect a known project-relative file with `universal_file_preview` through
`code-analysis-server-vvz`. Always pass `full_text_max_lines` explicitly. Use
the returned positive integer `node_ref` for drill-down and refresh it after a
structural change. Use `get_file_lines` only for exact bounded ranges or invalid
source.

This is a read-only research route. Local target reread immediately before and
after `apply_patch` remains part of the local editing lifecycle.
