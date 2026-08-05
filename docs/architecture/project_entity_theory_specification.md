# Project, Entity, and Theory Model — Technical Specification

## 1. Purpose, status, and scope

**Status: proposed, reviewable safety-copy.** This is the exact HRS import candidate derived only from the user's explicit authorization in this task; it remains reviewable here and is not a claim that Plan Manager, code, or migrations have changed. A live Plan Manager dry-run and mechanical validation are mandatory before any normative replacement, and this Markdown candidate is not proof that import is possible.

The specification introduces a bitemporal, project-scoped entity and theory model while preserving the canonical `Document -> Chapter -> Paragraph -> SemanticChunk` hierarchy, PostgreSQL canonical storage, pgvector indexing, atomic ingestion, adapter-owned transport, and the current server/client boundaries. It distinguishes canonical evidence from derived projections and generated prose.

## 2. Terminology

- **act_date:** effective-time instant at which the represented world is requested.
- **known_at:** transaction-time instant at which the system's knowledge is requested.
- **assertion:** append-only statement that a typed payload was valid for an effective interval and was known at a transaction time.
- **supersession:** append-only statement that a prior assertion is no longer accepted from a transaction-time point; it never rewrites the prior assertion.
- **PrimarySource / SourceState:** immutable original source material and a lossless, time-addressable restoration state respectively.
- **TheoryGraph / GraphSnapshot:** a resolved graph and its immutable publication identity.
- **TheoryTextBlock / CompiledContext / GeneratedProse:** canonical compiler block, selected compiler input, and noncanonical derived prose respectively.

## 3. Binding requirements

### Identity and registry

1. **{ID01} UUID identity.** Every independently referenceable persistent object shall have a PostgreSQL `uuid` UUIDv4 primary key.
2. **{ID02} Universal lookup.** ObjectRegistry shall locate a registered object by UUID alone without a caller-supplied type.
3. **{ID03} Concrete locator.** Each ObjectRegistry row shall contain an allowlisted object kind and concrete typed-table locator.
4. **{ID04} Duplicate rejection.** Registry allocation shall reject a UUID that is already registered and shall never upsert it.
5. **{ID05} Registry foreign keys.** Cross-family object references shall use registry UUID foreign keys and typed-table constraints where a concrete kind is required.
6. **{ID06} Registered families.** Entities, Entity types, temporal assertions, PrimarySources, SourceStates, ProcessingRuns, Projects, relations, independently referenceable endpoints, memberships, attachments, snapshots, graph members, text blocks, contexts, SufficiencyReports, prose, previews, successful-delete tombstones, and rejected-attempt audits shall be registered objects.
7. **{ID07} Unregistered rows.** Pure field-value rows, implementation join rows without independent references, current/index materializations, pgvector index rows, cursor payloads, and Redis keys shall not receive registry identities.
8. **{ID08} One version strategy.** The implementation shall use one registered temporal-assertion identity with family-specific payload tables keyed by that assertion UUID and shall not introduce a duplicate generic `object_version` plus parallel family-version identities.

### Bitemporal evidence and restoration

9. **{BT01} Effective assertion.** Each temporal assertion shall state `effective_from` and nullable `effective_until` as a half-open effective interval.
10. **{BT02} Transaction assertion.** Each temporal assertion shall state immutable `recorded_at` transaction time and its acceptance provenance.
11. **{BT03} Known-time supersession.** Corrections shall append a supersession assertion with a transaction timestamp rather than updating or deleting an accepted assertion.
12. **{BT04} Bitemporal resolution.** Resolution shall select assertions valid at both `act_date` and `known_at` before applying authorization or business filters.
13. **{BT05} Current defaults.** When omitted, `act_date` and `known_at` shall each resolve to PostgreSQL `NOW()` once at request start and that pair shall be returned or cursor-pinned.
14. **{BT06} Historical insertion.** Inserting historical information shall append assertions and interval-boundary assertions without mutating earlier payload evidence.
15. **{BT07} Historical removal.** Removing information from an effective interval shall append a deletion or interval-closure assertion and shall not erase visibility for earlier effective or known times.
16. **{BT08} Restoration.** Restoration shall append a new assertion whose payload hash and lossless source-state reference reproduce the selected historical state exactly.
17. **{BT09} Version ordering.** Each logical object shall have a monotonic revision order determined by accepted assertion order, not by mutable current state.
18. **{BT10} Temporal uniqueness.** Resolution shall reject ambiguous simultaneously accepted assertions for the same logical object and effective instant.

### Sources, entities, projects, and relations

19. **{SO01} Immutable source.** PrimarySource shall preserve immutable original bytes, content hash, media type, source locator, and provenance.
20. **{SO02} Exact source state.** SourceState shall preserve a lossless manifest or reconstruction locator and hash sufficient for exact original and any as-of state restoration.
21. **{SO03} Processing provenance.** ProcessingRun shall record input objects, deterministic configuration hash, actor, status, timestamps, and output objects.
22. **{EN01} Entity identity.** Entity shall have UUID, `description varchar(100)`, optional owner Entity UUID, and Entity type.
23. **{EN02} Typed fields.** Entity type schema shall define stable textual field codes, value types, cardinality, requiredness, and relation constraints.
24. **{EN03} Field validation.** Create and update shall validate field codes and values against the selected Entity type schema.
25. **{EN04} Generic lifecycle.** Generic get, list, create, update, references, and owner-tree operations shall reuse one lifecycle surface and stable field codes.
26. **{EN05} Typed relation.** A binary relation shall have a declared relation type and two typed Entity endpoints.
27. **{EN06} Hyperrelation.** A hyperrelation shall have one relation object and ordered role-bearing endpoint objects.
28. **{PR01} Project identity.** Project shall have UUID and an owner Entity UUID.
29. **{PR02} Many-to-many membership.** An Entity may have independently referenceable temporal memberships in more than one Project.
30. **{PR03} Hierarchy.** Project parent/child hierarchy shall reject cycles.
31. **{PR04} Parent attachment.** A child Project may attach to a parent graph node or parent snapshot.
32. **{PR05} Pinned inheritance.** A pinned attachment shall retain the explicitly selected parent snapshot.
33. **{PR06} Tracking inheritance.** A tracking attachment shall resolve its parent-node target at the child's requested bitemporal point.

### Access and deletion

34. **{LC01} Object access.** `get_some_object(obj_id, act_date='2026-01-01T04:03:32Z', known_at?)` shall resolve one bitemporal assertion before filtering.
35. **{LC02} Temporal list access.** List, context, traverse, and compile shall accept optional `act_date` and `known_at` and default both as specified in {BT05}.
36. **{LC03} Stable cursor.** A pagination cursor shall store the resolved `act_date`, `known_at`, immutable sort key, and integrity hash.
37. **{LC04} Snapshot precedence.** Explicit `graph_snapshot_id` shall take precedence over `act_date` and `known_at` for graph membership resolution.
38. **{LC05} Snapshot disclosure.** A response resolved from a snapshot shall return that snapshot identity and its recorded bitemporal resolution.
39. **{DL01} Soft delete input.** Soft delete shall accept an exact UUID list and an optional descendant-closure request.
40. **{DL02} Soft delete evidence.** Soft delete shall append deletion assertions and preserve prior bitemporal visibility.
41. **{DL03} Hard-delete preview.** `registry_hard_delete_preview` shall receive an exact UUID list, report dependencies and any required closure, and produce a canonical preview hash.
42. **{DL04} No silent hard-delete expansion.** Hard-delete execution shall reject a partial input whenever preview identified required closure until the caller explicitly resubmits the complete exact UUID list.
43. **{DL05} Hard-delete blockers.** Hard delete shall reject surviving inbound references and unresolved dependency cycles.
44. **{DL06} Hard-delete ordering.** Hard delete shall run atomically in reverse topological order with ObjectRegistry rows deleted last and all database foreign keys `ON DELETE RESTRICT`.
45. **{DL07} Cycle policy.** A strongly connected deletion set shall be rejected unless the caller supplies the entire cycle and a previewed, procedure-defined constraint-safe order exists.
46. **{DL08} Tombstone evidence.** An AuditTombstone shall persist the deleted UUID values, preview hash, actor, time, and result as scalar audit data without foreign keys to deleted rows.

### Provenance, compilation, cache, and compatibility

47. **{PV01} Evidence chain.** Provenance shall connect PrimarySource and SourceState through ProcessingRun to Document, Paragraph, SemanticChunkVersion, GraphMember, SupportedFact, TheoryTextBlock, CompiledContext, and GeneratedProse.
48. **{PV02} Evidence identifiers.** Every provenance link shall retain source object UUID, temporal assertion UUID where applicable, and content hash or immutable locator.
49. **{PV03} Canonical prose boundary.** GeneratedProse shall be a registered provenance artifact but shall never become canonical theory truth.
50. **{GT01} Deterministic compiler.** The graph-to-text compiler shall produce ordered TheoryTextBlocks deterministically from a resolved snapshot and task selector.
51. **{GT02} Selector contract.** A compilation task selector shall declare `paragraph`, `chapter`, or `whole_theory`, target identity, requested language, and size/token budget.
52. **{GT03} Evidence closure.** A sufficiency report shall identify the evidence closure used for every emitted block and every permitted prose claim.
53. **{GT04} Contradiction policy.** The compiler shall emit contradictory assertions as explicitly labeled alternatives and shall not silently choose one unsupported assertion.
54. **{GT05} Uncertainty policy.** The compiler shall preserve uncertainty, confidence, and missing-evidence markers from supported inputs.
55. **{GT06} Omission policy.** A compiler result constrained by selector or budget shall declare omitted supported facts and the reason for each omission.
56. **{GT07} Budget policy.** If the required evidence closure exceeds the size or token budget, compilation shall return a deterministic insufficiency result rather than truncate evidence silently.
57. **{GT08} Support rule.** GeneratedProse shall reject a factual claim unless its CompiledContext contains a SupportedFact path to canonical evidence.
58. **{CA01} PostgreSQL canonicality.** PostgreSQL shall remain canonical for history, provenance, registry, and compiled artifacts.
59. **{CA02} Redis limitation.** Redis shall cache only the current accepted compiled projection with graph snapshot and PostgreSQL watermark.
60. **{CA03} Cache fallback.** On cache miss, stale watermark, hash mismatch, or Redis failure, the service shall rebuild from or fall back to PostgreSQL.
61. **{IN01} Ingestion preservation.** Ingestion shall remain atomic, versioned, and idempotent, and incomplete document versions shall remain invisible.
62. **{IN02} Chunk separation.** SemanticChunk and SemanticChunkVersion history shall remain separate from TheoryTextBlock history.
63. **{IN03} Existing clients.** SvoChunkerClient and EmbeddingClient shall remain the chunking and embedding integrations.
64. **{IN04} Metadata evidence gate.** `chunk-metadata-adapter` shall not change without concrete observed adapter metadata evidence.
65. **{SC01} Search boundary.** ChunkQuery shall remain the sole public full-text, semantic, and hybrid search contract.
66. **{SC02} Transport boundary.** mcp-proxy-adapter shall remain the exclusive owner of transport, JSON-RPC, OpenAPI, authorization, TLS/mTLS, queues, WebSockets, and proxy registration.
67. **{SC03} Command registry.** ServerManager shall retain the explicit command manifest and identical main/worker registration, with live help and info synchronized to it.
68. **{RU01} Registry reuse.** Existing EntityUuidRegistry behavior shall be extended in place or replaced only where its upsert behavior is unsafe; no parallel registry is permitted.
69. **{RU02} Lifecycle reuse.** Existing generic lifecycle commands shall be extended in place; no parallel CRUD surface is permitted.
70. **{RU03} Version reuse.** Existing chunk version handling shall remain the chunk concern and shall not become a second generic theory-version system.
71. **{RU04} Reconstruction boundary.** Existing current-chunk reconstruction shall remain separate from graph-to-text compilation; no parallel reconstruction meaning is permitted.
72. **{RU05} Migration safety.** Migration `0005` registry allocation behavior shall replace `ON CONFLICT UPDATE` with duplicate rejection after a legacy-duplicate preflight.
73. **{ID09} Assertion identity link.** Each TemporalAssertion shall directly identify the RegisteredObject whose typed payload it asserts.
74. **{SO04} Run output links.** ProcessingRun shall retain typed provenance links to every output family it produces.
75. **{EN07} Relation endpoint links.** Binary relation endpoints and HyperRelationEndpoint records shall directly identify their Entity endpoints.
76. **{PR07} Hierarchy role links.** ProjectHierarchy shall directly identify both its parent Project and its child Project.
77. **{PR08} Attachment role links.** InheritanceAttachment shall directly identify its child Project and its parent graph node or GraphSnapshot target.
78. **{IN05} Chunk version link.** SemanticChunk shall directly identify its SemanticChunkVersion records while retaining chunk ownership separate from theory versioning.
79. **{PV04} Graph-member provenance.** GraphMember shall retain direct resolved RegisteredObject and TemporalAssertion references.
80. **{PV05} Fact evidence provenance.** SupportedFact shall retain direct canonical evidence references in addition to its GraphMember reference.
81. **{PV06} Context provenance.** CompiledContext shall directly retain GraphSnapshot and SufficiencyReport identities.
82. **{BT11} Partial supersession.** A partial-interval correction shall globally supersede the original assertion at correction `recorded_at` and append non-overlapping left/original-copy, corrected-overlap, and right/original-copy payload assertions as applicable, omitting empty segments without rewriting prior assertions.
83. **{GT09} Mechanical evidence set.** CompilerPolicy shall mechanically compute the required-evidence set from selector closure, declared traversal depth, relation allowlist, Project inheritance, source precedence, and versioned task-policy exclusions.
84. **{DL09} Rejection audit.** After a rejected hard-delete attempt rolls back, the system shall write a separate rejection audit record in an independent transaction containing scalar UUID values, preview hash, and reason without live foreign keys.
85. **{DL10} Restricted-cycle rejection.** A strongly connected foreign-key set shall be rejected whenever `ON DELETE RESTRICT` leaves no provably valid deletion order, even when the caller supplied the entire cycle.
86. **{RU06} Chunk provenance extension.** `chunk_version_commands.py` shall be extended minimally for assertion and provenance references while retaining its independent chunk ownership and without parallel versioning.
87. **{PM01} Normative validation gate.** Live Plan Manager dry-run and mechanical validation shall succeed before this candidate can replace normative plan content.
88. **{PR09} Exclusive inheritance target.** InheritanceAttachment shall contain exactly one of `parent_node_uuid` or `graph_snapshot_uuid` through a target-union constraint.
89. **{PR10} Pinned target constraint.** Pinned inheritance shall require `graph_snapshot_uuid` and shall reject `parent_node_uuid`.
90. **{PR11} Tracking target constraint.** Tracking inheritance shall require `parent_node_uuid`, shall reject `graph_snapshot_uuid`, and shall reject tracking-snapshot semantics in this model.
91. **{IN06} Chunk-version identity.** SemanticChunkVersion public/version UUID shall equal `temporal_assertion.assertion_uuid` and shall be its only version identity.

## 4. Bitemporal model

There is no mutable generic `object_version` table. `temporal_assertion` is the single registered version identity: `assertion_uuid`, `object_uuid`, `effective_from`, `effective_until`, `recorded_at`, `revision_order`, payload hash, and ProcessingRun provenance. Typed family payload tables (`entity_assertion`, `source_state_assertion`, `relation_assertion`, and equivalent typed payloads) use `assertion_uuid` as both primary key and registry foreign key. Immutable families may have one assertion; they still use the same strategy when a time-addressable statement is necessary.

`assertion_supersession` is append-only: it names prior assertion UUID, superseding assertion UUID or correction event UUID, `recorded_at`, and reason. A partial-interval correction has exactly one model: at correction `recorded_at` it globally supersedes the original assertion, then appends up to three non-overlapping payload assertions—left replacement copying the original payload and provenance for the unaffected left interval, corrected overlap assertion, and right replacement copying the original payload and provenance for the unaffected right interval; empty segments are omitted. Before that `known_at`, resolution sees the original assertion; at or after it, resolution sees only applicable replacements. A replacement is a TemporalAssertion payload role, not an independent boundary-metadata candidate, and no prior row is rewritten. A deletion is a registered `deletion_assertion`, not a destructive update; it declares the object and effective interval removed as known at its recorded time. The old assertion remains queryable when `known_at` predates the deletion or when `act_date` is outside the removed overlap.

For a request, capture `resolved_act_date` and `resolved_known_at` once. A candidate is effective when its interval contains `resolved_act_date`; it is transaction-visible when `recorded_at <= resolved_known_at` and no supersession/deletion assertion visible at `resolved_known_at` disqualifies that effective interval. Select the greatest accepted revision order only after those predicates, and fail on more than one remaining assertion. `current` means `(act_date=NOW(), known_at=NOW())`, not a mutable materialized row. Historical insertion appends a payload assertion and any required interval-boundary assertions; historical removal appends a deletion/closure assertion; restoration appends a new assertion referring to the exact chosen SourceState payload hash.

## 5. Registry, schema, and object boundaries

`object_registry(object_uuid uuid primary key, object_kind, concrete_table, created_at)` is a universal directory with a checked kind/table allowlist. `registry_allocate` inserts exactly once and raises `duplicate_object_uuid`; no `ON CONFLICT UPDATE` is allowed. Global references use `object_uuid` to registry and typed constraints where needed. Every cross-family delete-relevant foreign key is `ON DELETE RESTRICT`. SufficiencyReport and DeletionRejectionAudit are registered objects; AuditTombstone is only successful-delete evidence, while DeletionRejectionAudit is only rejected-attempt evidence, and their lifecycles do not overlap.

### 5.1 Registered objects

The following independently referenceable rows are registry objects: PrimarySource; SourceState; ProcessingRun; Document, Chapter, and Paragraph where independently addressable; SemanticChunk and SemanticChunkVersion; Entity; EntityType and EntityTypeSchema; temporal assertion and deletion assertion; RelationType; EntityRelation; HyperRelation; HyperRelationEndpoint when exposed or cited; Project; ProjectEntityMembership; InheritanceAttachment; TheoryGraph; GraphSnapshot; GraphMember; SupportedFact; TheoryTextBlock; CompiledContext; SufficiencyReport; GeneratedProse; HardDeletePreview; successful-delete AuditTombstone; and rejected-attempt DeletionRejectionAudit.

### 5.2 Non-registered rows

The following are not objects: `entity_field_value` entries, typed schema property rows, endpoint-role indexes when no endpoint object exists, `compiled_context_block` ordering joins, graph lookup indexes, current SemanticChunk materializations, vector/pgvector index rows, cursor encodings, cache invalidation rows, and Redis keys. They cannot be addressed by UUID alone and cannot be foreign-key targets outside their owning typed table.

### 5.3 Logical typed tables

| Family | Canonical typed storage |
|---|---|
| Sources | `primary_source`, `source_state`, `temporal_assertion`, `source_state_assertion`, and provenance links. |
| Entity model | `entity`, `entity_type`, `entity_type_schema`, `entity_assertion`, and non-object `entity_field_value`. |
| Relations | `relation_type`, `entity_relation`, `hyper_relation`, and registered `hyper_relation_endpoint` where individually cited. |
| Projects | `project`, `project_hierarchy`, registered `project_entity_membership`, and registered `inheritance_attachment`. |
| Theory | `theory_graph`, `graph_snapshot`, registered `graph_member`, `supported_fact`, `theory_text_block`, `compiled_context`, and `generated_prose`. |
| Deletion | `hard_delete_preview`, non-object preview graph edges, and `audit_tombstone`. |

## 6. Domain and lifecycle API

Entity has UUID, Entity type, `description varchar(100)`, and owner Entity UUID. A schema validates textual field codes before any generic create/update. Binary relations are typed directed records. Hyperrelations have a relation identity plus endpoint ordinal, role code, and Entity UUID; endpoint order is unique within the relation. Project membership is temporal and many-to-many. Project hierarchy is acyclic. InheritanceAttachment has an exclusive target union: exactly one `parent_node_uuid` or `graph_snapshot_uuid`; `pinned` requires the GraphSnapshot alternative, while `tracking` requires the parent-node alternative and rejects tracking-snapshot semantics. ParentGraphNode is a non-persisted role selecting an already registered Entity or GraphMember in the parent graph, not its own persistent family.

The adapter-only command surface reuses generic lifecycle operations: `get_some_object`, `list_objects`, `create_object`, `update_object`, `list_references`, and `owner_tree`, plus `relation_attach`, `project_attach`, `snapshot_publish`, `traverse_graph`, `context_compile`, and `generate_prose`. All graph-dependent commands accept `act_date?`, `known_at?`, and `graph_snapshot_id?`; an explicit snapshot wins over both temporal arguments. List cursors retain the two resolved instants and sort state.

## 7. Deletion procedures and trigger limits

`registry_hard_delete_preview(object_uuids uuid[])` canonicalizes the supplied exact list, reports direct dependencies, required closure, surviving inbound references, strongly connected components, reverse topological order when possible, and `preview_hash`. It does not add closure members to the requested list.

`registry_hard_delete(object_uuids uuid[], preview_hash text)` compares the exact canonical list and hash. If preview reported a required closure absent from that list, it rejects with the missing UUID values. It rejects every strongly connected foreign-key set for which `ON DELETE RESTRICT` leaves no provably valid constraint-safe, previewed order, including when the exact list contains the entire cycle. It locks targets, rechecks inbound references, deletes typed rows in reverse topological order, deletes registry rows last, writes a successful AuditTombstone with scalar deleted UUID values and no FKs to them, and commits atomically. If execution rejects, its work transaction rolls back first; a separate independent transaction then writes a registered DeletionRejectionAudit containing scalar requested UUID values, preview hash, reason, actor, and time, also without live foreign keys. AuditTombstone and DeletionRejectionAudit are disjoint successful-delete and rejected-attempt subtypes respectively.

Required procedures are `registry_allocate`, `temporal_assertion_insert`, `temporal_assertion_supersede`, `relation_attach`, `project_attach`, `snapshot_publish`, `registry_hard_delete_preview`, and `registry_hard_delete`. Triggers are only local guards: immutable PrimarySource payload, UUID allocation consistency, typed-table kind consistency, field-code/type validation, effective-window shape, endpoint ordinal uniqueness, hierarchy cycle guard, and audit timestamp generation. Cross-object traversal, compiler decisions, cache rebuild, and hard-delete planning stay in procedures/services.

## 8. Provenance and evidence closure

The required provenance path is explicit: PrimarySource **produces** SourceState through ProcessingRun; ProcessingRun has typed output links for every produced SourceState, Document, Chapter, Paragraph, SemanticChunkVersion, TheoryGraph, TheoryTextBlock, CompiledContext, and GeneratedProse; Document/Chapter/Paragraph and SemanticChunkVersion retain the source-state/assertion UUIDs and hashes from which their content was produced; GraphMember retains direct resolved RegisteredObject and TemporalAssertion UUIDs; SupportedFact retains GraphMember UUID plus direct canonical evidence anchors; TheoryTextBlock retains SupportedFact UUIDs; CompiledContext retains GraphSnapshot, selected block, and SufficiencyReport UUIDs; GeneratedProse retains its CompiledContext UUID and generating ProcessingRun UUID. A generated assertion is allowed only when this path closes to canonical source, document, paragraph, chunk-version, or graph evidence.

Graph compilation takes a selector with target kind (`paragraph`, `chapter`, `whole_theory`), target UUID, language, requested `act_date`/`known_at` or snapshot, and hard token/size budget. CompilerPolicy mechanically computes the required-evidence set as every canonical fact reachable from the selector closure under its declared traversal depth, relation allowlist, Project inheritance rule, source-precedence rule, and versioned task-policy exclusions; neither the compiler nor a prose model may self-attest that this set is complete. It orders that set by project hierarchy path, explicit source precedence, entity/relation type order, effective interval, assertion revision order, and UUID tie-breaker. The result contains ordered blocks plus a machine-readable sufficiency report listing: the computed required-evidence set and closure, supported claims, contradictory alternatives, uncertainty markers, omitted supported facts and reasons, token estimate, and `sufficient | insufficient` decision. If the computed set required for the task cannot fit, it returns `insufficient` with an omission declaration; it never silently truncates or invents facts. GeneratedProse may transform wording only and rejects any claim without a SupportedFact path.

## 9. Redis, ingestion, search, security, and transport

PostgreSQL is canonical. Redis may retain only the current accepted CompiledContext projection keyed by snapshot/context identity and carrying PostgreSQL watermark, accepted time, compiler version, and content hash. It has no historical role; a miss, stale watermark, hash mismatch, or outage falls back to PostgreSQL and rebuilds the projection.

Ingestion remains atomic, versioned, and idempotent; incomplete document versions are invisible. SvoChunkerClient performs chunking and EmbeddingClient creates embeddings. SemanticChunk/Version history remains distinct from TheoryTextBlock history. `chunk-metadata-adapter` changes require concrete observed metadata evidence. ChunkQuery remains the sole public full-text, semantic, and hybrid search contract.

No new REST/FastAPI surface is permitted. mcp-proxy-adapter retains transport, JSON-RPC, OpenAPI, authorization, TLS/mTLS, queues, WebSockets, and registration. ServerManager retains the single command manifest and shared main/worker registration hook; live help and info remain synchronized. Authorization is checked before response disclosure and audit captures actor, command, requested IDs, resolved bitemporal pair or snapshot, run UUID, and result hash.

## 10. Implemented-versus-required reuse map

| Current path | Verified current fact | Target action |
|---|---|---|
| `db/schema.py` | Contains `EntityCRUDMixin` stubs, `DictionaryCRUDMixin` UUID plus `descr(100)`, EntityUuidRegistry, Project, document hierarchy, and SemanticChunk text/version/current structures. | **extend in place** for typed temporal entities and Projects; **replace unsafe behavior** only for registry upsert semantics; do not create a parallel schema family. |
| `entity_lifecycle_commands.py` | Contains generic lifecycle commands. | **extend in place** for stable-code bitemporal get/list/create/update/references/owner tree; do not add parallel lifecycle commands. |
| `chunk_version_commands.py` | Handles chunk history. | **extend in place** minimally for temporal-assertion and provenance references while retaining independent chunk ownership; do not generalize it into a second theory version system. |
| `text_reconstruction_commands.py` | Reconstructs chapter/source from current chunks only. | **reuse unchanged** for current-chunk reconstruction; **new concern** for separate graph-to-text compiler, never a parallel interpretation of reconstruction. |
| migration `0005` registry trigger | Uses `ON CONFLICT UPDATE`. | **replace unsafe behavior** with duplicate rejection after duplicate preflight; do not add a second registry. |
| `ServerManager` command manifest and shared registration hook | Owns live command registration/help/info. | **extend in place** for new commands; do not add a parallel registry or transport surface. |
| `ChunkQuery`, `SvoChunkerClient`, `EmbeddingClient`, and `chunk-metadata-adapter` integration | Existing search/chunk/embed boundaries. | **reuse unchanged** unless concrete adapter metadata evidence requires a scoped extension. |

## 11. Migration and acceptance criteria

Migration first inventories existing UUIDs and legacy duplicates, then installs duplicate-rejecting registry allocation, common temporal assertions, typed payload links, explicit provenance, relations/projects, snapshots/compiler, and rebuildable cache. Backfill creates disclosed migration-time assertions where original effective time is unknown; it never presents inferred time as original source fact. No release or code modification is claimed by this specification.

Acceptance requires: UUID-only location and duplicate rejection; exactly one bitemporal resolution for each tested request; historical insertion/removal/restoration preserving earlier visibility; snapshot precedence and cursor-pinned pair; multi-Project membership and pinned/tracking attachment; typed binary/hyper relations; no hard-delete expansion without explicit resubmission; atomic restricted hard delete and scalar tombstone evidence; provenance closure from source to prose; deterministic sufficient/insufficient compiler reports; Redis fallback; and regression preservation of ingestion, chunk history, ChunkQuery, adapter transport, client, manifest/help/info, PostgreSQL, and pgvector.

## 12. Proposed MRS concepts and relations

### 12.1 Typed concepts

1. **C-001 ObjectRegistry [entity]** is the universal directory that maps one UUIDv4 to one registered concrete object. Properties: UUID, kind, table locator; sources: {ID01}, {ID02}, {ID03}.
2. **C-002 RegistryKindLocator [value]** is the allowlisted kind and concrete-table pair for a registered object. Properties: kind, table code; sources: {ID03}, {ID04}.
3. **C-003 RegisteredObject [entity]** is an independently referenceable persistent object with one registry identity. Properties: UUID, kind; sources: {ID01}, {ID06}.
4. **C-004 TemporalAssertion [entity]** is the single registered version identity for a typed payload claim. Properties: assertion UUID, object UUID, revision order; sources: {ID08}, {BT01}, {BT02}, {BT09}.
5. **C-005 EffectiveInterval [value]** is the half-open interval for which an assertion represents the world. Properties: start, optional end; sources: {BT01}, {BT04}.
6. **C-006 TransactionTime [value]** is the immutable time at which an assertion became known to the system. Properties: recorded timestamp; sources: {BT02}, {BT04}.
7. **C-007 SupersessionAssertion [entity]** is an append-only record that changes accepted knowledge of an earlier assertion. Properties: prior UUID, event time, reason; sources: {BT03}, {BT04}.
8. **C-008 DeletionAssertion [entity]** is an append-only statement that removes an effective interval without erasing older as-of truth. Properties: target UUID, interval, recorded time; sources: {BT07}, {DL02}.
9. **C-009 BitemporalQuery [request]** is a request carrying effective and transaction-time inputs for resolution. Properties: act date, known at, defaults; sources: {BT04}, {BT05}, {LC01}.
10. **C-010 ResolvedTimePair [value]** is the request-start pair of resolved effective and transaction times. Properties: act date, known at; sources: {BT05}, {LC02}.
11. **C-011 TemporalCursor [value]** is a pagination token that pins a ResolvedTimePair and sort state. Properties: pair, sort key, hash; sources: {LC03}.
12. **C-012 PrimarySource [entity]** is immutable original material with provenance and content hash. Properties: bytes locator, SHA-256, media type; sources: {SO01}, {PV01}.
13. **C-013 SourceState [entity]** is a lossless restoration state of a PrimarySource. Properties: manifest, restoration hash, assertion UUID; sources: {SO02}, {BT08}.
14. **C-014 ProcessingRun [entity]** is an auditable execution with deterministic inputs and outputs. Properties: configuration hash, actor, status; sources: {SO03}, {PV01}.
15. **C-015 Document [entity]** is canonical documentation content retained in the document hierarchy. Properties: document UUID, source evidence; sources: {IN01}, {PV01}.
16. **C-016 Chapter [entity]** is an ordered document child with source evidence. Properties: chapter UUID, ordinal; sources: {IN01}, {PV01}.
17. **C-017 Paragraph [entity]** is an ordered chapter child with source evidence. Properties: paragraph UUID, ordinal; sources: {IN01}, {PV01}.
18. **C-018 SemanticChunk [entity]** is a retrieval-oriented segment with history distinct from theory text. Properties: chunk UUID, current pointer; sources: {IN02}, {SC01}.
19. **C-019 SemanticChunkVersion [entity]** is a versioned chunk payload with source and embedding evidence. Properties: public/version UUID equals `temporal_assertion.assertion_uuid`, hash; sources: {IN01}, {IN02}, {IN06}, {PV01}.
20. **C-020 Entity [entity]** is a typed domain object with a short description and optional owner. Properties: UUID, type code, `description varchar(100)`; sources: {EN01}.
21. **C-021 EntityType [entity]** is a classification that selects an EntityTypeSchema. Properties: type code, schema UUID; sources: {EN02}.
22. **C-022 EntityTypeSchema [schema]** is a versioned declaration of stable field codes and relation constraints. Properties: schema version, field definitions; sources: {EN02}, {EN03}.
23. **C-023 FieldCode [value]** is a stable textual key for an Entity value. Properties: code, value type, requiredness; sources: {EN02}, {EN03}, {EN04}.
24. **C-024 EntityFieldValue [value]** is a schema-validated value keyed by FieldCode for an Entity assertion. Properties: code, typed value; sources: {EN03}, {ID07}.
25. **C-025 OwnerEntity [role]** is the Entity assigned as owner of an Entity or Project. Properties: owner UUID; sources: {EN01}, {PR01}.
26. **C-026 Project [entity]** is a UUID-identified scope owned by an Entity. Properties: project UUID, owner UUID; sources: {PR01}.
27. **C-027 ProjectHierarchy [relation]** is an acyclic parent/child connection between Projects. Properties: parent UUID, child UUID; sources: {PR03}.
28. **C-028 ProjectEntityMembership [entity]** is a temporal independently referenceable Project-to-Entity membership. Properties: project UUID, entity UUID, assertion UUID; sources: {PR02}, {ID06}.
29. **C-029 InheritanceAttachment [entity]** is a child Project connection governed by an exclusive parent-node or GraphSnapshot target union. Properties: child Project UUID, target-union UUID, mode; sources: {PR04}, {PR05}, {PR06}, {PR08}, {PR09}.
30. **C-030 InheritanceMode [value]** is the pinned or tracking rule for parent resolution. Properties: pinned requires snapshot, tracking requires parent node; sources: {PR05}, {PR06}, {PR10}, {PR11}.
31. **C-031 RelationType [entity]** is the rule that constrains permitted typed relation endpoints. Properties: type code, endpoint constraints; sources: {EN05}, {EN06}.
32. **C-032 EntityRelation [relation]** is a typed directed binary relation between Entity assertions. Properties: subject UUID, object UUID, type UUID; sources: {EN05}.
33. **C-033 HyperRelation [entity]** is a typed relation identity with multiple role-bearing endpoints. Properties: relation UUID, type UUID; sources: {EN06}.
34. **C-034 HyperRelationEndpoint [entity]** is an ordered role-bearing endpoint of a HyperRelation when independently cited. Properties: ordinal, role code, entity UUID; sources: {EN06}, {ID06}.
35. **C-035 TheoryGraph [entity]** is the resolved graph for one Project and bitemporal point. Properties: graph UUID, project UUID, hash; sources: {PR02}, {LC04}, {GT01}.
36. **C-036 GraphSnapshot [entity]** is an immutable publication identity for a resolved TheoryGraph. Properties: snapshot UUID, recorded pair; sources: {LC04}, {LC05}.
37. **C-037 GraphMember [entity]** is an independently cited resolved object or assertion included in a GraphSnapshot. Properties: member UUID, assertion UUID, order; sources: {ID06}, {PV01}.
38. **C-038 SupportedFact [entity]** is an evidence anchor that ties a compiler-usable fact to a GraphMember and canonical evidence. Properties: fact UUID, anchors, hash; sources: {PV01}, {PV02}, {GT03}.
39. **C-039 CompilationTask [request]** is a graph-to-text selector declaring scope, target, language, and budget. Properties: target kind, UUID, token limit; sources: {GT02}, {GT07}.
40. **C-040 CompilerPolicy [schema]** is the stable ordering and contradiction/uncertainty policy for compilation. Properties: version, precedence rules; sources: {GT01}, {GT04}, {GT05}.
41. **C-041 TheoryTextBlock [entity]** is an ordered canonical compiler output block with evidence anchors. Properties: block UUID, ordinal, content hash; sources: {GT01}, {GT03}.
42. **C-042 CompiledContext [entity]** is a persisted ordered selection of TheoryTextBlocks for one task. Properties: context UUID, snapshot UUID, watermark; sources: {GT02}, {PV01}.
43. **C-043 SufficiencyReport [entity]** is a registered machine-readable decision about evidence closure and budget sufficiency. Properties: status, omissions, token estimate; sources: {GT03}, {GT06}, {GT07}, {ID06}.
44. **C-044 ContradictionRecord [entity]** is an explicit record of incompatible supported assertions. Properties: alternatives, evidence anchors; sources: {GT04}.
45. **C-045 UncertaintyMarker [value]** is a preserved indication of confidence or missing evidence. Properties: kind, source anchor; sources: {GT05}.
46. **C-046 OmissionDeclaration [entity]** is a record of a supported fact omitted from a bounded result and its reason. Properties: fact UUID, reason, budget impact; sources: {GT06}, {GT07}.
47. **C-047 GeneratedProse [entity]** is a registered provenance artifact derived from CompiledContext but not canonical theory truth. Properties: prose UUID, context UUID, run UUID; sources: {PV03}, {GT08}.
48. **C-048 RedisProjection [cache]** is a rebuildable cache of one current accepted compiled projection. Properties: snapshot UUID, context UUID, hash; sources: {CA02}, {CA03}.
49. **C-049 ProjectionWatermark [value]** is the PostgreSQL progress marker used to validate a RedisProjection. Properties: sequence, accepted time; sources: {CA02}, {CA03}.
50. **C-050 ChunkQuery [service]** is the sole public full-text, semantic, and hybrid search contract. Properties: mode, filters, cursor; sources: {SC01}.
51. **C-051 ServerManagerManifest [schema]** is the explicit shared command registry that drives live help and info. Properties: command schema, worker parity; sources: {SC03}.
52. **C-052 AdapterTransport [service]** is the adapter-owned transport and security boundary. Properties: JSON-RPC, authorization, TLS/mTLS; sources: {SC02}.
53. **C-053 SvoChunkerClient [service]** is the existing client that produces chunking outputs. Properties: configuration, run UUID; sources: {IN03}.
54. **C-054 EmbeddingClient [service]** is the existing client that produces semantic embeddings. Properties: model configuration, vector reference; sources: {IN03}.
55. **C-055 HardDeletePreview [entity]** is the deterministic report for an exact requested hard-delete list. Properties: UUID list, graph, hash; sources: {DL03}, {DL04}.
56. **C-056 PreviewHash [value]** is the digest binding a hard-delete execution to one preview. Properties: algorithm, digest; sources: {DL03}, {DL04}.
57. **C-057 DependencyGraph [entity]** is the dependency and cycle evidence for deletion planning. Properties: vertices, edges, SCCs; sources: {DL03}, {DL05}, {DL07}.
58. **C-058 AuditTombstone [entity]** is immutable scalar audit evidence of a successful hard-delete transaction only. Properties: deleted UUID values, actor, result; sources: {DL06}, {DL08}, {ID06}.
59. **C-059 RegistryAllocation [procedure]** is the duplicate-rejecting procedure that creates a registry identity. Properties: UUID, kind, locator; sources: {ID04}, {RU01}, {RU05}.
60. **C-060 GenericLifecycleSurface [service]** is the shared stable-field-code CRUD and traversal command surface. Properties: commands, temporal inputs; sources: {EN04}, {LC01}, {LC02}, {RU02}.
61. **C-061 ChunkHistoryBoundary [boundary]** is the rule that keeps existing chunk history distinct from theory versioning and reconstruction. Properties: chunk scope, theory scope; sources: {IN02}, {RU03}, {RU04}.
62. **C-062 ReuseBoundary [boundary]** is the rule that forbids parallel registry, lifecycle, version, and reconstruction implementations. Properties: target action, owned path; sources: {RU01}, {RU02}, {RU03}, {RU04}, {RU06}.
63. **C-063 ParentProjectRole [role]** is the parent Project identity carried by a ProjectHierarchy relation. Properties: parent project UUID; sources: {PR07}.
64. **C-064 ChildProjectRole [role]** is the child Project identity carried by a ProjectHierarchy relation. Properties: child project UUID; sources: {PR07}.
65. **C-065 ParentGraphNode [role]** is a non-persisted selector of an already registered Entity or GraphMember node in the parent graph. Properties: selected object UUID, parent graph UUID; sources: {PR04}, {PR08}, {PR11}.
66. **C-066 BinaryRelationEndpoint [role]** is a typed subject or object Entity endpoint of an EntityRelation. Properties: direction, entity UUID; sources: {EN05}, {EN07}.
67. **C-067 VersionedTaskPolicy [schema]** is the versioned policy that explicitly excludes facts from a compilation selector closure. Properties: policy UUID, exclusion rules; sources: {GT09}.
68. **C-068 RequiredEvidenceSet [entity]** is the mechanically computed canonical fact set required for a CompilationTask. Properties: fact UUIDs, closure hash, policy UUID; sources: {GT03}, {GT09}.
69. **C-069 ReplacementPayloadAssertion [role]** is a left or right TemporalAssertion payload replacement that preserves an unaffected portion after global supersession. Properties: assertion UUID, copied payload hash, interval; sources: {BT11}.
70. **C-070 DeletionRejectionAudit [entity]** is a registered independently committed scalar audit record for a rejected hard-delete attempt only. Properties: requested UUID values, preview hash, reason; sources: {DL09}, {ID06}.
71. **C-071 PlanValidationGate [boundary]** is the mandatory live Plan Manager dry-run and mechanical-validation gate for normative replacement. Properties: candidate identity, validation result; sources: {PM01}.
72. **C-072 InheritanceTargetUnion [schema]** is the exclusive target schema requiring exactly one parent-node or GraphSnapshot alternative for an InheritanceAttachment. Properties: one-of cardinality, mode constraints; sources: {PR09}, {PR10}, {PR11}.

### 12.2 Allowed relations

Only `uses`, `owns`, `implements`, `extends`, `depends_on`, `produces`, and `consumes` are allowed relation verbs; the following relation set uses only semantically necessary members of that set.

| Subject | Verb | Object |
|---|---|---|
| C-001 ObjectRegistry | owns | C-003 RegisteredObject |
| C-003 RegisteredObject | uses | C-002 RegistryKindLocator |
| C-004 TemporalAssertion | uses | C-005 EffectiveInterval |
| C-004 TemporalAssertion | uses | C-006 TransactionTime |
| C-004 TemporalAssertion | depends_on | C-003 RegisteredObject |
| C-007 SupersessionAssertion | depends_on | C-004 TemporalAssertion |
| C-008 DeletionAssertion | depends_on | C-004 TemporalAssertion |
| C-069 ReplacementPayloadAssertion | depends_on | C-004 TemporalAssertion |
| C-009 BitemporalQuery | uses | C-010 ResolvedTimePair |
| C-011 TemporalCursor | uses | C-010 ResolvedTimePair |
| C-014 ProcessingRun | consumes | C-012 PrimarySource |
| C-014 ProcessingRun | produces | C-013 SourceState |
| C-013 SourceState | depends_on | C-012 PrimarySource |
| C-014 ProcessingRun | produces | C-015 Document |
| C-014 ProcessingRun | produces | C-016 Chapter |
| C-014 ProcessingRun | produces | C-017 Paragraph |
| C-014 ProcessingRun | produces | C-019 SemanticChunkVersion |
| C-014 ProcessingRun | produces | C-035 TheoryGraph |
| C-014 ProcessingRun | produces | C-041 TheoryTextBlock |
| C-014 ProcessingRun | produces | C-042 CompiledContext |
| C-014 ProcessingRun | produces | C-047 GeneratedProse |
| C-015 Document | depends_on | C-013 SourceState |
| C-016 Chapter | depends_on | C-015 Document |
| C-017 Paragraph | depends_on | C-016 Chapter |
| C-019 SemanticChunkVersion | depends_on | C-017 Paragraph |
| C-020 Entity | uses | C-021 EntityType |
| C-020 Entity | uses | C-025 OwnerEntity |
| C-021 EntityType | owns | C-022 EntityTypeSchema |
| C-022 EntityTypeSchema | owns | C-023 FieldCode |
| C-024 EntityFieldValue | uses | C-023 FieldCode |
| C-026 Project | uses | C-025 OwnerEntity |
| C-027 ProjectHierarchy | owns | C-063 ParentProjectRole |
| C-027 ProjectHierarchy | owns | C-064 ChildProjectRole |
| C-063 ParentProjectRole | uses | C-026 Project |
| C-064 ChildProjectRole | uses | C-026 Project |
| C-028 ProjectEntityMembership | uses | C-026 Project |
| C-028 ProjectEntityMembership | uses | C-020 Entity |
| C-029 InheritanceAttachment | uses | C-030 InheritanceMode |
| C-029 InheritanceAttachment | uses | C-026 Project |
| C-029 InheritanceAttachment | uses | C-072 InheritanceTargetUnion |
| C-072 InheritanceTargetUnion | uses | C-065 ParentGraphNode (tracking alternative) |
| C-072 InheritanceTargetUnion | uses | C-036 GraphSnapshot (pinned alternative) |
| C-065 ParentGraphNode | uses | C-003 RegisteredObject |
| C-065 ParentGraphNode | depends_on | C-037 GraphMember |
| C-032 EntityRelation | uses | C-031 RelationType |
| C-032 EntityRelation | owns | C-066 BinaryRelationEndpoint |
| C-066 BinaryRelationEndpoint | uses | C-020 Entity |
| C-033 HyperRelation | uses | C-031 RelationType |
| C-033 HyperRelation | owns | C-034 HyperRelationEndpoint |
| C-034 HyperRelationEndpoint | uses | C-020 Entity |
| C-035 TheoryGraph | uses | C-026 Project |
| C-036 GraphSnapshot | depends_on | C-035 TheoryGraph |
| C-037 GraphMember | depends_on | C-036 GraphSnapshot |
| C-037 GraphMember | uses | C-003 RegisteredObject |
| C-037 GraphMember | depends_on | C-004 TemporalAssertion |
| C-038 SupportedFact | depends_on | C-037 GraphMember |
| C-038 SupportedFact | depends_on | C-012 PrimarySource |
| C-038 SupportedFact | depends_on | C-013 SourceState |
| C-038 SupportedFact | depends_on | C-015 Document |
| C-038 SupportedFact | depends_on | C-017 Paragraph |
| C-038 SupportedFact | depends_on | C-019 SemanticChunkVersion |
| C-041 TheoryTextBlock | consumes | C-038 SupportedFact |
| C-042 CompiledContext | consumes | C-041 TheoryTextBlock |
| C-042 CompiledContext | uses | C-039 CompilationTask |
| C-042 CompiledContext | uses | C-040 CompilerPolicy |
| C-042 CompiledContext | uses | C-036 GraphSnapshot |
| C-042 CompiledContext | produces | C-043 SufficiencyReport |
| C-043 SufficiencyReport | depends_on | C-042 CompiledContext |
| C-040 CompilerPolicy | uses | C-067 VersionedTaskPolicy |
| C-043 SufficiencyReport | uses | C-068 RequiredEvidenceSet |
| C-044 ContradictionRecord | depends_on | C-038 SupportedFact |
| C-046 OmissionDeclaration | depends_on | C-038 SupportedFact |
| C-047 GeneratedProse | consumes | C-042 CompiledContext |
| C-047 GeneratedProse | depends_on | C-014 ProcessingRun |
| C-048 RedisProjection | depends_on | C-042 CompiledContext |
| C-048 RedisProjection | uses | C-049 ProjectionWatermark |
| C-050 ChunkQuery | consumes | C-019 SemanticChunkVersion |
| C-018 SemanticChunk | owns | C-019 SemanticChunkVersion |
| C-051 ServerManagerManifest | owns | C-050 ChunkQuery |
| C-052 AdapterTransport | uses | C-051 ServerManagerManifest |
| C-053 SvoChunkerClient | produces | C-019 SemanticChunkVersion |
| C-054 EmbeddingClient | produces | C-019 SemanticChunkVersion |
| C-055 HardDeletePreview | produces | C-056 PreviewHash |
| C-055 HardDeletePreview | produces | C-057 DependencyGraph |
| C-058 AuditTombstone | depends_on | C-055 HardDeletePreview |
| C-059 RegistryAllocation | produces | C-003 RegisteredObject |
| C-060 GenericLifecycleSurface | uses | C-009 BitemporalQuery |
| C-061 ChunkHistoryBoundary | depends_on | C-018 SemanticChunk |
| C-062 ReuseBoundary | depends_on | C-060 GenericLifecycleSurface |
| C-070 DeletionRejectionAudit | depends_on | C-055 HardDeletePreview |
| C-071 PlanValidationGate | depends_on | C-062 ReuseBoundary |

## 13. Open exclusions

- No ORM, JSON-schema library, queue backend, Redis client, vector model, or LLM is selected here.
- No new REST/FastAPI surface, adapter transport rewrite, or `chunk-metadata-adapter` change is authorized by this specification.
- No GeneratedProse is canonical evidence, no registry UUID is upserted, no hard delete cascades by database FK, and no Redis history is canonical.
- No implementation, migration execution, deployment, or Plan Manager mutation is performed by this safety-copy.
