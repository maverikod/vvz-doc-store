# Canonical file structure

```text
doc-store/
  AGENTS.md
  .claude/CLAUDE.md
  .codex/instructions.md
  docs/standards/originals/        # uploaded YAML standards
  docs/architecture/file_structure.md
  docs/plans/doc-store/source_spec.md
  docs/plans/doc-store/spec.yaml
  docs/plans/doc-store/G-NNN-<slug>/README.yaml
  docs/plans/doc-store/G-NNN-<slug>/T-NNN-<slug>/README.yaml
  src/doc_store_client/
  src/doc_store_filewatcher/
  src/doc_store_server/{api,core,db,filters,ingestion,query,vectorization}/
  migrations/
  systemd/
  tests/{client,server,filewatcher,integration}/
```
