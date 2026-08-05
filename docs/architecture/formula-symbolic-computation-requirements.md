# Formula and Symbolic Computation Requirements

Status: accepted requirements awaiting incorporation into the live Plan Manager
HRS/MRS and the derived GS/TS/AS hierarchy.

Recorded at: 2026-07-22T23:36:44+03:00.

This file is a human-readable recovery and review artifact. It is not normative
plan truth and must not replace the live `doc-store` plan in Plan Manager. The
normative plan must be updated after the blocking Plan Manager defect is fixed
and verified.

## 1. Purpose

The document store must treat mathematical formulas as first-class domain
objects. A formula is neither merely text inside an imported chunk nor merely a
rendering inside generated prose. It has its own identity, source provenance,
versions, semantic structure, symbol bindings, derivation history, search
capabilities, and verification evidence.

The system must also support symbolic checking through a replaceable engine.
SymPy is the first implementation adapter, not the persisted domain model and
not an irreversible public contract.

These additions must preserve the already accepted separation between:

1. imported source text and source fragments;
2. `SemanticChunk` history;
3. normalized theory-graph entities;
4. formulas and formula versions;
5. generated `TheoryTextBlock` and prose history;
6. symbolic verification runs and their evidence.

No one of these histories may silently overwrite or masquerade as another.

## 2. Formula domain model

### 2.1. Required entities

The target model must include at least the following UUID-addressable entities:

- `Formula`: stable conceptual identity of a mathematical expression.
- `FormulaOccurrence`: one exact occurrence in an imported source.
- `FormulaVersion`: a temporal version of normalized formula content.
- `FormulaExpression`: validated internal expression tree or AST.
- `FormulaSymbol`: a symbol used by one or more formulas.
- `SymbolBinding`: meaning of a symbol in a defined scope.
- `FormulaGroup`: an ordered or typed collection, such as a system of equations.
- `FormulaRelation`: typed relation between formulas.
- `DerivationStep`: one independently addressable step in a derivation.
- `AssumptionSet`: explicit assumptions used for interpretation or checking.
- `SymbolicCheckRun`: immutable record of one verification attempt.
- `SymbolicCounterexample`: persisted counterexample produced by a check.

Every entity and every version must have a UUID primary identifier registered in
the global object registry. A caller that knows only this UUID must be able to
resolve the owning storage table and retrieve the object through the common
object API.

### 2.2. Lossless source provenance

`FormulaOccurrence` must preserve the imported representation exactly. Depending
on the source, this may include:

- raw LaTeX, MathML, AsciiMath, or Unicode text;
- source image or image-region reference;
- OCR input and OCR output without destructive replacement;
- source document/version/fragment UUIDs;
- character offsets, page number, and bounding box;
- import hash and importer metadata;
- project membership and owner linkage;
- timestamps and temporal validity.

The original occurrence is immutable evidence. Cleanup, normalization, parsing,
repair, or conversion creates additional linked objects or versions; it never
destroys or rewrites the imported payload. The system must restore both the
original state and the effective state at a requested date.

### 2.3. Formula versions and temporal rules

`FormulaVersion` is a full entity with its own UUID and integrity constraints.
Every version has timestamps and temporal validity sufficient for an as-of
query. The wider bitemporal design should be used where transaction time and
domain-effective time differ.

Versioned APIs must support insertion and explicit deletion of an erroneous
version. Soft deletion applies to any entity and may later support hierarchical
propagation to descendants. Hard deletion operates over a list of global object
UUIDs, checks dependencies first, and must reject deletion when a non-deleted
object still references a target. Formula versions are subject to the same rules
as every other globally registered object.

Ordinary object and list APIs select the one effective version for an optional
`act_date`, defaulting to `NOW()`. They must not return all versions unless the
caller explicitly requests version or link history. PostgreSQL selection will
normally use a window query or an equivalent deterministic temporal query.

### 2.4. Expression structure and symbols

`FormulaExpression` must be a validated engine-neutral AST. It is the canonical
normalized structure used for structural search and symbolic-engine adapters.
It must not be a pickled SymPy object or another engine-specific serialization.

The model must represent explicitly:

- free and bound symbols;
- symbol scope and owner;
- aliases and notation variants;
- declared meaning and links to theory concepts;
- mathematical domain and assumptions;
- units and physical dimensions;
- indices, tensors, vectors, operators, functions, and constants;
- ambiguity or unresolved parsing state.

Normalization must not erase distinctions that matter in the source theory.
Different source occurrences may point to the same conceptual formula while
retaining their exact notation and provenance.

### 2.5. Derivations and evidence

A derivation is an ordered graph of `DerivationStep` entities rather than one
opaque text field. Each step records input formula-version UUIDs, output
formula-version UUIDs, operation or justification, assumptions, source evidence,
and optional symbolic-check evidence.

Formula and derivation entities participate in the theory graph. A supported
fact may be backed simultaneously by text evidence, formula evidence, and
derivation evidence. Every theory node must support traversal back to all exact
source fragments and formula occurrences from which it was formed.

### 2.6. How formulas enter the system

Formula ingestion is a traceable pipeline, not an implicit side effect of text
chunking. The system must introduce these additional audit entities:

- `FormulaDetectionRun`: one immutable execution of an extractor set over an
  exact source version;
- `FormulaCandidate`: a proposed formula span or image region before canonical
  acceptance;
- `FormulaParseAttempt`: one parser result, including errors and confidence;
- `FormulaResolution`: the decision that links a candidate to a new or existing
  `Formula`, exact `FormulaVersion`, and glossary bindings;
- `FormulaReview`: a human or policy review decision when automatic resolution
  is unsafe or ambiguous.

There are exactly two user-visible formula creation modes:

1. **Dialogue mode.** A user and a model formulate, discuss, correct, or derive a
   formula interactively. The immutable dialogue turns and attachments are the
   primary source. The model proposes a structured formula candidate, explains
   its interpretation and symbol bindings, and presents it for acceptance or
   further correction. An accepted state creates a new `Formula` or a new
   `FormulaVersion` of an existing formula.
2. **Imported-file mode.** A file is imported and preserved first. The model then
   processes exact source fragments and, with parser/OCR/layout tools where
   needed, proposes formula candidates, boundaries, normalized expressions,
   glossary bindings, and theory-graph links. Acceptance may be interactive or
   policy-driven, but the model-produced interpretation remains traceable to the
   exact file version and fragments.

The model participates in both modes. Native equation parsers, OCR, layout
analysis, and symbolic computation are tools used by the model-facing extraction
workflow; they do not independently create canonical formula truth. Conversely,
the model may not bypass deterministic parsing, schema validation, glossary
resolution, provenance recording, or configured review policy.

Dialogue source and imported-file source are both primary, immutable source
entities. Model proposals, normalized formulas, generated explanations, and
verification results are derived entities. The system must preserve the exact
user message, model proposal, acceptance/correction event, file fragment, and
tool evidence needed to reconstruct how each formula version appeared.

The required processing sequence is:

1. Import and freeze the exact source document/version and source fragments.
2. Detect formula candidates and preserve exact text offsets, page coordinates,
   bounding boxes, numbering, surrounding text, and source hashes.
3. Create `FormulaOccurrence` from the untouched source representation.
4. Parse the candidate into an engine-neutral `FormulaExpression` AST without
   changing the occurrence.
5. Extract symbols and resolve them against the effective notation glossary.
6. Compare the normalized structure and context with existing formulas to link
   an occurrence or create a new conceptual `Formula`.
7. Apply dimensional, syntactic, semantic, and optional symbolic checks.
8. Publish the formula into the theory graph only when the configured acceptance
   policy is satisfied; otherwise retain it as a reviewable candidate.

In imported-file mode, the model-facing extraction workflow uses deterministic,
source-aware evidence in this priority order:

1. Native structured markup such as LaTeX, MathML, OMML, or document equation
   objects is authoritative for the occurrence payload.
2. Layout-aware PDF/document parsers identify text spans and display-equation
   regions when native markup is unavailable.
3. Formula OCR adapters process image-only regions and retain both image evidence
   and every OCR hypothesis.
4. The model combines this evidence to propose boundaries, repairs, notation
   meanings, normalized expressions, and links. Its output is always a candidate
   carrying model/version, confidence, prompt/policy provenance, and review
   status until accepted under policy.

No OCR, parser, or model may silently replace imported source content. Conflicting
extractor results remain independently addressable and comparable. Re-running an
extractor creates a new `FormulaDetectionRun`; it does not rewrite an earlier run.

Extraction is idempotent for the tuple of source-version UUID, source location,
source hash, extractor identity/version, and extraction policy. It must correctly
handle inline formulas, display formulas, numbered equations, multi-line
expressions, equation systems, aligned derivations, tables containing formulas,
and formulas split by page or layout boundaries.

`doc-store` owns pipeline orchestration, persistence, review state, publication,
and theory-graph linkage. Source-format parsers and OCR/model integrations are
replaceable extraction adapters. `chunk-metadata-adapter` owns portable candidate,
occurrence, and formula-reference metadata needed by chunks and `ChunkQuery`; it
does not own formula truth or the review workflow.

### 2.6.1. Version creation rules for both modes

`Formula` is the stable conceptual identity. `FormulaVersion` is the immutable
state selected by temporal queries. Every accepted creation or semantic change
produces a new version UUID and timestamp; no accepted formula state is updated
in place.

A new version is required when any of these change:

- normalized expression or expression AST;
- equation side, operator, index, bound variable, or grouping structure;
- symbol-to-concept binding;
- assumptions, domain, unit, or physical dimension that affect meaning;
- formula type, relation, or derivation role;
- canonical glossary rendering when it changes the normalized representation.

Pure display rendering may remain a separate rendering artifact when meaning and
normalized structure are unchanged. A correction made during dialogue creates a
new candidate revision; once accepted, it creates a new `FormulaVersion`. File
reprocessing never overwrites an earlier extracted version: it creates a new
`FormulaDetectionRun`, resolution decision, and, if accepted content differs, a
new formula version.

Each formula version records its creation mode (`dialogue` or `imported_file`),
source UUIDs, source-version UUIDs, dialogue turn or file-fragment references,
model identity/version, extraction policy, acceptance actor/policy, predecessor
version, transaction timestamp, effective interval, and reason for change. An
as-of query returns the one effective version while explicit history APIs return
the complete ordered version chain.

### 2.7. Project notation glossary

The system requires a versioned notation glossary so that concepts use one
controlled system of symbols in normalized formulas and generated theory text.
The glossary must not erase the notation used by an imported source.

Required entities include:

- `NotationGlossary`: a project-owned, versioned glossary with a UUID;
- `NotationEntry`: one concept-to-notation rule in a declared scope;
- `NotationAlias`: an accepted source or compatibility notation;
- `SymbolOccurrence`: one exact use of a symbol in source or generated content;
- `SymbolResolution`: a versioned link from an occurrence to a glossary entry;
- `NotationConflict`: an unresolved collision or policy violation;
- `NotationMigration`: an explicit plan for changing canonical notation without
  rewriting historical source evidence.

Each notation entry must support:

- a concept UUID and optional theory-graph node UUID;
- canonical LaTeX and Unicode renderings;
- permitted aliases and prohibited or deprecated aliases;
- semantic type, mathematical domain, tensor/vector/scalar rank, and index rules;
- unit and physical-dimension declarations where applicable;
- textual name, definition, language variants, and disambiguation notes;
- owner/project, scope, temporal validity, provenance, and review status.

Glossaries follow project ownership and membership. A project may inherit a
glossary from its owner project, attach shared glossary entities belonging to
several projects, and add a local overlay. Resolution precedence must be explicit
and deterministic: formula-local binding, section/theory scope, current project
overlay, owner-project ancestors, then shared defaults. A nearer scope may
override a symbol only through an explicit binding; it may not silently change
the meaning of historical occurrences.

The same written symbol may represent different concepts in different scopes,
and one concept may have source aliases. Therefore symbol text alone is never a
global identity. Canonical generated text uses the effective glossary notation,
while citations and source views retain exact original notation. A notation
change creates a new glossary version and can trigger regeneration or validation;
it never mutates source occurrences.

Formula extraction must resolve every symbol to a glossary entry, create a
formula-local binding, or mark it unresolved. Publication policy may reject or
quarantine formulas with unresolved or conflicting symbols. Search expands
canonical symbols and aliases through the glossary while returning the exact
notation actually present in each source occurrence.

### 2.8. Formula and glossary API

The API is exposed through the existing MCP server and client architecture; no
separate REST/FastAPI surface is introduced. All object-level CRUD uses global
UUID identity, text field names, the common object registry, referential
integrity, project authorization, soft deletion, dependency-checked hard
deletion, and optional `act_date` temporal selection.

The formula API must provide commands equivalent to:

- `formula_add_from_dialog(conversation_id, turn_ids, proposal, policy)` for
  interactive user/model creation and correction;
- `formula_extract_from_file(source_id, source_version_id, scope, policy,
  async_mode)` for model processing of an imported file;
- `formula_detection_run_get(run_id)` and paginated run listing;
- `formula_candidate_get/list/review` with source, status, confidence, extractor,
  project, and time filters;
- `formula_create/get/list/update/soft_delete`;
- `formula_version_insert/list/delete` including explicit hard deletion of an
  erroneous version through the global registry deletion procedure;
- `formula_version_get(formula_id, act_date=NOW())` and explicit ordered history;
- `formula_occurrence_get/list` and `formula_source_context`;
- `formula_parse`, `formula_render`, and `formula_compare`;
- `formula_relation_create/get/list/delete`;
- `derivation_create/get/list`, step insertion/deletion/reordering, and
  derivation validation;
- `formula_verify` plus `symbolic_check_get/list/replay/cancel`;
- `formula_symbols` and `formula_unresolved_symbols`;
- unified search through formula-aware `ChunkQuery`.

The glossary API must provide commands equivalent to:

- `glossary_create/get/list/update/soft_delete`;
- `glossary_version_insert/list/delete`;
- `notation_entry_create/get/list/update/soft_delete`;
- `notation_alias_add/remove/list`;
- `notation_resolve` for one symbol or a batch;
- `notation_validate` for a formula, theory node, document, or project;
- `notation_conflict_get/list/resolve`;
- `notation_migration_preview/apply` for generated and normalized artifacts only.

Names above define required capabilities, not a frozen command catalog. During
plan decomposition the live registry must be audited to reuse existing generic
object/version/list commands where they already satisfy the contract and to avoid
duplicate public operations.

Mutation commands must accept an idempotency key. Long-running extraction,
project-wide validation, migration preview, and symbolic verification must use
the established synchronous-emulation or asynchronous job semantics. List
commands must have bounded pagination and deterministic ordering. Every response
must include enough UUID, version, project, temporal, provenance, and review
metadata to retrieve the exact underlying objects.

## 3. Generated theory text

The system must be able to emit theory text blocks from which a model can create
a human-readable chapter, paragraph, or complete theory, depending on the
requested scope.

A `TheoryTextBlock` must reference exact `FormulaVersion` UUIDs and include
rendering and explanation instructions. Generated prose may choose presentation
style but must not silently change a formula, its assumptions, variable meaning,
or derivation status.

Source chunks, generated blocks, and generated prose remain distinct. A generated
text block can cite source fragments and formulas, but it is not another source
chunk version. Its own version history and provenance must be independently
queryable and restorable by date.

## 4. Formula search

Formula-aware retrieval must support:

- exact source-representation search;
- exact normalized-expression search;
- structural AST and subexpression search;
- variable-renamed or alpha-equivalent search;
- semantic and hybrid search;
- symbol, scope, unit, and dimension filters;
- graph-neighborhood and derivation traversal;
- provenance and source-fragment filters;
- verification-status filters;
- project membership and owner-project hierarchy;
- effective-state selection at an optional date.

`ChunkQuery` remains the sole public search contract. It must be extended with an
explicit corpus or entity-domain selector plus formula modes and filters. A
second unrelated public formula-search API must not be introduced. Existing
source-chunk queries must retain their current default behavior.

Every result must identify its corpus/entity type and exact source, formula, and
version UUIDs where applicable. List methods require bounded pagination,
deterministic ordering, and transparent current-version selection. Link/history
search is the explicit exception that may return multiple versions.

## 5. Symbolic computation abstraction

### 5.1. Engine contract

Define a replaceable `SymbolicComputationEngine` interface. The first adapter is
`SymPyAdapter`. Future engines must be addable without redesigning the formula
schema, theory graph, persisted verification records, or public object API.

The contract must cover at least:

- parsing or conversion from the validated internal AST;
- simplification under an explicit policy;
- algebraic equivalence checks;
- equation and system solving where supported;
- substitution and solution checking;
- dimensional and unit consistency checks;
- step-by-step derivation checks;
- numerical evaluation and counterexample search;
- capability discovery and unsupported-operation reporting.

### 5.2. Verification policy and outcomes

Every run uses an explicit `VerificationPolicy` and `AssumptionSet`. A check must
return one of these non-overlapping outcomes:

- `proved`;
- `disproved`;
- `inconclusive`;
- `unsupported`;
- `timeout`.

Heuristic simplification is not an unqualified proof. An engine must report
which methods were attempted and why the final outcome was selected.

Recommended checks include:

1. AST and symbol consistency;
2. domain and assumption consistency;
3. unit and physical-dimension consistency;
4. algebraic equivalence under named transformations;
5. substitution and `checksol`-style validation;
6. numerical counterexample generation;
7. verification of each derivation step rather than only the final expression.

### 5.3. Reproducible verification records

`SymbolicCheckRun` must persist:

- engine name and exact version;
- input and output formula-version UUIDs;
- assumption-set snapshot or immutable reference;
- verification policy;
- methods attempted;
- result status;
- residual expression when relevant;
- counterexample references;
- warnings, resource-limit events, and diagnostics;
- start/end timestamps and provenance.

The persisted record must be sufficient to reproduce or audit the run. It is
verification evidence linked to formulas and theory facts; it never overwrites
source content or formula history.

### 5.4. Security and resource limits

Untrusted imported strings must never be passed directly to `sympify`,
`parse_expr`, `eval`, or equivalent dynamic evaluators. The adapter builds SymPy
objects only from the validated internal AST and an allowlisted operator and
symbol vocabulary.

Checks must run in an isolated worker with:

- no network access;
- CPU, memory, wall-time, and recursion limits;
- AST-size and expression-growth limits;
- controlled temporary storage;
- cancellation and timeout handling;
- deterministic, version-aware caching.

Cache keys must include formula hashes, assumptions, policy, and engine version.

## 6. `chunk-metadata-adapter` ownership

Any required extension of `SemanticChunk` or `ChunkQuery` belongs to the
`chunk-metadata-adapter` project, CAS project UUID
`7cacbad8-2e06-4c2f-a42c-a5dee8e903e6`.

Required adapter work includes:

- backward-compatible formula references and provenance metadata;
- formula corpus/mode/filter fields in `ChunkQuery`;
- stable serialization and deserialization templates;
- unknown-field and schema-version compatibility;
- deterministic pagination and temporal selection;
- server/client schema and help/info synchronization;
- tests proving that formula and chunk histories are not conflated.

## 7. Plan integration after Plan Manager recovery

Plan authoring is paused because Plan Manager defect
`26fa21a5-5487-4cf7-9b41-64a350a7074c` was observed corrupting plan blocks from
structured TS inputs/outputs. Planning agents must remain stopped until the fix
is independently verified against the live server.

After recovery, the plan-authoring owner must:

1. re-read live HRS, MRS, the open cascade, all TODOs, bugs, comments, and current
   CAS-backed implementation evidence;
2. incorporate every accepted requirement in this file into human-readable HRS;
3. project the requirements into typed, non-prose MRS concepts and relations;
4. revise or expand GS coverage radically where needed;
5. decompose dependency-ready branches into TS and one-file AS nodes;
6. assign cross-project work explicitly to `doc-store` or
   `chunk-metadata-adapter`;
7. include migrations, backward compatibility, security, testing, client
   contracts, deployment, and live MCP Proxy acceptance;
8. run mechanical validation before semantic scoring;
9. obtain independent architectural review before freezing changed artifacts;
10. use the live Plan Manager cascade discipline and generate any exports only
    through `plan_export`.

The revised HRS/MRS must explicitly assign ownership for the extraction pipeline,
extractor adapters, review workflow, formula/glossary API, notation inheritance,
symbol resolution, and generated-text notation enforcement. These requirements
must not be left implicit inside the storage schema or symbolic-engine work.

The expanded plan must maintain one-to-one traceability from accepted HRS
requirements through MRS and implementation ownership, without duplicate
concrete work ownership.

## 8. TODO registration state at the time of this record

An attempted four-item Plan Manager write produced a partial result:

1. Created successfully and project anchoring confirmed:
   `Add first-class formulas, provenance, and derivations to the theory graph`.
   The compact response did not retain the created UUID; locate it by its exact
   title when Plan Manager is stable.
2. Not created because `planmgr_1` became unavailable:
   `Add a pluggable symbolic verification engine with a hardened SymPy adapter`.
3. Not created because `planmgr_1` was no longer registered:
   `Extend chunk-metadata-adapter for formula-aware metadata and unified ChunkQuery`.
4. Not created because `planmgr_1` was no longer registered:
   `After Plan Manager repair, extend doc-store HRS/MRS and full implementation
   cascade for formulas`.

The fourth item must be created as blocked by defect
`26fa21a5-5487-4cf7-9b41-64a350a7074c` and anchored to plan UUID
`b847fc0b-7180-4430-a1a3-820d93d8261c`. The first two domain/server tasks belong
to doc-store project UUID `ff997eab-d809-4cb9-b805-9dff4df60c6d`; the adapter
task belongs to `chunk-metadata-adapter` UUID
`7cacbad8-2e06-4c2f-a42c-a5dee8e903e6`.

Before retrying, list matching TODOs by exact title to avoid duplicates. Do not
blindly replay the four-item batch because `todo_create` is not idempotent.

Two additional TODOs are required and have not yet been sent to Plan Manager:

5. `Add an auditable formula extraction pipeline and Formula API`, anchored to
   doc-store. It must expose exactly two creation modes: user/model dialogue and
   model processing of an imported file. It must cover immutable dialogue/file
   sources, detection runs, candidates, native markup, layout extraction,
   OCR/parser tools, model provenance, parsing, review, idempotency, publication,
   formula-version creation, temporal CRUD, derivations, verification commands,
   job semantics, authorization, pagination, and live server/client contracts.
6. `Add a versioned project notation glossary and symbol-resolution API`,
   anchored to doc-store. It must cover glossary inheritance and overlays,
   shared multi-project entries, canonical notation and aliases, scopes,
   dimensions, conflicts, migrations, source preservation, generated-text
   enforcement, search expansion, CRUD/version commands, and tests.

Before creating these TODOs, also check whether the first successfully created
formula TODO can be linked as their parent or dependency without altering its
accepted description. Do not collapse the extraction and glossary scopes into an
opaque implementation note: each requires independent acceptance criteria and
traceability into HRS/MRS.

## 9. Acceptance summary

The addition is complete only when all of the following are true:

- exact source formula content is losslessly restorable;
- formula candidates are extracted through immutable, reproducible detection
  runs with explicit adapter and review provenance;
- every formula enters through either user/model dialogue or model processing of
  an imported file, with immutable primary-source and model provenance;
- formulas, versions, derivation steps, and verification runs are globally
  addressable by UUID and protected by referential integrity;
- every accepted semantic correction creates a new immutable `FormulaVersion`
  that is selectable through current and as-of APIs;
- formula and glossary capabilities are available through the existing MCP
  server/client API with idempotent mutations and bounded list operations;
- every normalized symbol is resolved through a versioned project glossary or
  remains explicitly unresolved/conflicting;
- generated theory text uses canonical effective notation while source views
  preserve the notation actually imported;
- ordinary APIs and lists provide deterministic as-of views;
- a theory node can return all original text and formula evidence;
- generated chapter/paragraph/theory material references exact formula versions;
- formula search is available through the unified `ChunkQuery` contract;
- symbolic checks are reproducible, resource-bounded, and safe for untrusted
  imports;
- SymPy is replaceable behind the engine interface;
- all histories remain distinct and linked through explicit relations;
- live HRS/MRS and the complete implementation cascade cover the accepted model.
