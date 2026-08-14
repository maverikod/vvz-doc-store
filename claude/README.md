# doc-store Claude prompt package

`../CLAUDE.md` is the entrypoint. This directory contains the project-bound
Claude contract bundle.

Package version: `v1.6.19`

## Layout

- `modes.yaml`: mode router.
- `roles/common.yaml`, `roles/laws.yaml`, `roles/tooling.yaml`, `roles/orchestrator.yaml`: mandatory core read.
- `roles/*.yaml`: stage contracts.
- `ops/*.yaml`: lazily loaded operating cards.
- `VERSION`: bundle version marker.

## Project bindings

- Project: `doc-store`
- Local checkout: `/home/vasilyvz/projects/tools/doc-store`
- CAS project ID: `ff997eab-d809-4cb9-b805-9dff4df60c6d`
- CAS server: `code-analysis-server-vvz`

## Notes

- This bundle is Claude-only.
- Codex prompt files remain outside this directory and are not modified by it.
- Relative bundle references resolve from `claude/`.
