# Oracle DDL Versioning Agent — Architecture Design

**Version:** 1.0 — **APPROVED**
**Status:** Official project architecture. Frozen; changes require owner approval per `CLAUDE.md` §8.
**Governed by:** `CLAUDE.md`, `README.md`, `docs/requirements.md`

---

## 0. Design principles

Everything below follows from six rules taken directly from the specification:

1. **Oracle is strictly read-only.** Only dictionary `SELECT`s and read-only
   `DBMS_METADATA` calls.
2. **`LAST_DDL_TIME` in `DBA_OBJECTS` is the single authority for "did this object
   change".** Nothing else decides whether an object is processed.
3. **No content-comparison step.** A changed object is extracted, normalized, and written
   unconditionally.
4. **Git is the final gate against unnecessary commits.** Writing a file whose bytes are
   identical to the committed version produces no entry in `git status`, so no commit is
   created. This satisfies "never create empty commits" without a comparer module.
5. **State advances only after a successful push.**
6. **Failure is contained.** Object-level failures never stop a database; database-level
   failures never stop other databases.

---

## 1. Application architecture

### 1.1 Layering

Four layers, with dependencies pointing strictly downward. No layer imports from a layer
above it.

```
┌─────────────────────────────────────────────────────────────────┐
│ ORCHESTRATION            src/main.py                            │
│  CLI parsing · run pipeline · aggregate results · exit code     │
│  (no business logic)                                            │
└─────────────────────────────────────────────────────────────────┘
                               │
┌─────────────────────────────────────────────────────────────────┐
│ WORKFLOW                 pipeline / run-unit coordination       │
│  per-database sequence · status aggregation · Git phase         │
└─────────────────────────────────────────────────────────────────┘
                               │
┌───────────────┬───────────────┬───────────────┬─────────────────┐
│ ORACLE        │ NORMALIZER    │ REPOSITORY    │ GIT / STATE     │
│ connection    │ pure text     │ file mapping  │ git client      │
│ discovery     │ transforms    │ file writing  │ commit / push   │
│ extraction    │ (no I/O)      │               │ execution state │
└───────────────┴───────────────┴───────────────┴─────────────────┘
                               │
┌─────────────────────────────────────────────────────────────────┐
│ FOUNDATION      config · models · logging · utils               │
└─────────────────────────────────────────────────────────────────┘
```

**Why this shape:** only the Oracle layer touches a database and only the Git/Repository
layers touch the filesystem. Normalization is pure functions over strings. That means the
entire normalizer, file mapper, state model, and commit-message builder are unit-testable
with no Oracle instance and no network.

### 1.2 Execution model

One execution = one process = one synchronization = exit.

The unit of work is one **schema**, not one database. Each schema is carried all the
way from extraction to a pushed commit before the next schema begins, and each advances
its own watermark only on success (§4.4, decisions D-15…D-18).

```
main
 └─ load configuration          (fatal on invalid)
 └─ load execution_state.json   (fatal on parse error, migrate v1 → v2)
 └─ for each enabled database, sequentially:
 │    connect (pool)
 │    resolve the schema list   (config include, or SELECT DISTINCT OWNER)
 │
 │    └─ for each schema, sequentially — one complete cycle each:
 │         1. discover changed objects    LAST_DDL_TIME > this schema's watermark
 │         2. extract source              per-type strategy
 │         3. normalize                   deterministic
 │         4. write .sql files            unconditional overwrite
 │         5. reconcile deletions         scoped to this schema only
 │         6. stage this schema's written files and deletions
 │         7. nothing staged  → no commit, no push, watermark untouched, next schema
 │         8. commit                      <database>.<schema>||<ts>: …
 │         9. push (rebase + retry)
 │        10. write this schema's state, amend into the commit, force-push with lease
 │        ── any failure in 1-10 ⇒ STOP THE EXECUTION (§14.3). This schema's
 │           watermark is not advanced, and no later schema is attempted.
 │           Schemas already pushed keep their advanced watermarks.
 │
 └─ emit summary · exit code
```

The consequence of step 10 preceding the next schema is that partial progress is durable:
an execution that fails on the fourth schema leaves the first three correctly synchronized
and recorded, and the next run resumes from the fourth.

### 1.3 Concurrency model

| Scope | Model | Reason |
|---|---|---|
| Across databases | Sequential | Was a thread pool. Per-schema commits made it untenable — see below (decision D-16). |
| Across schemas | Sequential | Each schema commits and pushes before the next starts, so they cannot overlap by construction. |
| Within a schema | Sequential | Requirement: do not process multiple objects from the same database in parallel unless safe. Sequential also keeps `DBMS_METADATA` session transform state predictable. |
| Git operations | Single-threaded, interleaved with extraction, once per schema | The working tree and the index are one shared mutable resource. |

**Why the thread pool was removed.** It existed because extraction is I/O-bound and
databases are independent. That reasoning still holds for *extraction* — but the index is
not independent. Once each schema owns a commit, concurrent extraction would either have
to buffer every schema's files until a serialized Git phase at the end (which is the
previous design, and forfeits the durable partial progress that motivated the change), or
interleave `git add`/`commit` across threads, which corrupts the index and produces
commits containing another schema's files.

Concurrency could be recovered by extracting concurrently into per-schema buffers and
committing sequentially afterwards. That is a genuine option if extraction time ever
becomes the constraint; it is not implemented, because a full export of ~9,800 objects
currently takes minutes, not hours, and sequential execution is far easier to reason
about when a failure must stop everything after it.

---

## 2. Python package structure

Structure is exactly the one specified in `docs/requirements.md` §17. Responsibilities:

| Module | Responsibility | Depends on |
|---|---|---|
| `src/main.py` | CLI argument parsing, wiring dependencies, invoking the pipeline, exit code. **No business logic.** | workflow, config, logging |
| `src/config/models.py` | Frozen dataclasses: `DatabaseConfig`, `Settings`, `SchemaFilter`, `ObjectTypeFilter`, `AppConfig` | — |
| `src/config/loader.py` | Read + validate YAML, resolve secret references, fail fast on invalid configuration | models, secrets |
| `src/oracle/connection.py` | Connection pool creation/teardown, session initialization, connectivity retry, read-only guard | config, models |
| `src/oracle/object_discovery.py` | Build and run the `DBA_OBJECTS` query; return ordered `OracleObject` list | connection, models |
| `src/oracle/source_extractor.py` | `DBA_SOURCE` / `DBA_TAB_COLUMNS` / `DBA_SYNONYMS` based extraction | connection, models |
| `src/oracle/ddl_extractor.py` | `DBMS_METADATA` based extraction, transform parameter setup | connection, models |
| `src/oracle/metadata.py` | Extraction dispatcher: maps object type → strategy; validates extracted source | source_extractor, ddl_extractor |
| `src/normalizer/ddl_normalizer.py` | Whitespace, line endings, tabs, blank lines, header/`CREATE OR REPLACE` formatting, quoted identifiers, terminator | — (pure) |
| `src/normalizer/plsql_normalizer.py` | Keyword casing restricted to non-literal, non-comment regions | — (pure) |
| `src/normalizer/storage_clause_remover.py` | Removal of storage/physical/environment clauses | — (pure) |
| `src/repository/file_mapper.py` | `OracleObject` → `Path` mapping; object-type → folder table; name sanitization | models |
| `src/repository/writer.py` | Directory creation, atomic file write, UTF-8/LF/trailing-newline enforcement, dry-run suppression | file_mapper |
| `src/repository/comparer.py` | *(retained per the specified layout)* Path-set reconciliation for deleted-object detection; **not** content diffing | file_mapper |
| `src/git/git_client.py` | Thin wrapper over `git` invocations: `status`, `add`, `rm`, `commit`, `rebase`, `push`, `rev-parse` | — |
| `src/git/commit.py` | Commit message subject + body construction; "is anything staged" check | git_client, models |
| `src/git/push.py` | Push with rebase-and-retry, remote URL assembly with token, failure classification | git_client |
| `src/state/models.py` | `DatabaseState`, `ExecutionState` dataclasses, JSON (de)serialization | — |
| `src/state/execution_state.py` | Load/validate/save `execution_state.json`, atomic write, watermark resolution | state.models |
| `src/logging/logger.py` | Logger configuration, structured formatter, secret redaction filter | config |
| `src/utils/hashing.py` | Content hashing (used for logging/reporting and reconciliation bookkeeping only) | — |
| `src/utils/filesystem.py` | Atomic write helper, safe path join, directory ensure | — |
| `src/utils/timers.py` | Duration measurement, UTC timestamp helpers | — |
| `src/models/oracle_object.py` | `OracleObject`, `ObjectStatus`, `ExtractedSource` | — |
| `src/models/database.py` | Runtime database descriptor | — |
| `src/models/execution.py` | `RunUnitResult`, `ExecutionResult`, counters | — |

**Dependency injection:** the pipeline receives its collaborators (connection factory,
extractor dispatcher, normalizer, writer, git client, state store) as constructor
arguments. Tests substitute fakes without patching modules.

---

## 3. Oracle connection layer

### 3.1 Driver and mode

`python-oracledb`. Thin mode is the default target — it needs no Oracle Instant Client,
which keeps the Docker image small and removes a class of native-library failures in CI.
Thick mode remains configurable per database in `settings.yml` for environments that
require it (e.g. certain legacy auth schemes).

### 3.2 Pooling

One pool per database, created at run-unit start, closed in a `finally` block.

| Parameter | Default | Note |
|---|---|---|
| `min` | 1 | Objects are processed sequentially within a database |
| `max` | 2 | Headroom for a supervisory/metadata session |
| `increment` | 1 | |
| `ping_interval` | 60s | Detects stale sessions after idle periods |
| `getmode` | wait with timeout | Never block indefinitely |

### 3.3 Session initialization

On acquiring a session the layer sets, once per session:

- `NLS_DATE_FORMAT` / `NLS_TIMESTAMP_FORMAT` to fixed ISO-like values — **determinism**:
  DDL text and dictionary output must not vary with the runner's locale.
- `NLS_SORT` / `NLS_COMP` to binary — deterministic ordering.
- `DBMS_METADATA` transform parameters (see §5.3).
- Optionally `SET TRANSACTION READ ONLY` for the session, as defense in depth.

`ALTER SESSION` is a session-scoped, non-persistent statement that changes nothing in the
database and creates no object. It does not violate the read-only rule. If the owner
prefers zero `ALTER SESSION`, NLS can instead be forced client-side; this is flagged in
§23 as decision D-2.

### 3.4 Read-only enforcement

Three independent layers:

1. **Privilege level** — the account is granted only `CREATE SESSION`,
   `SELECT_CATALOG_ROLE` (or targeted `SELECT` on the required `DBA_*` views), and
   `EXECUTE ON DBMS_METADATA`. This is the real guarantee.
2. **Code level** — all SQL lives in a single constants module; a guard rejects any
   statement whose leading keyword is not `SELECT`, `WITH`, `BEGIN` (metadata call), or
   `ALTER SESSION`. Enforced by a unit test that scans the SQL constants for forbidden
   keywords.
3. **Review level** — the guard's deny-list is part of the test suite, so adding a
   mutating statement fails CI.

### 3.5 Connectivity failure

Handled by the retry policy in §15. After exhausting retries the run unit terminates with
`FAILED_DATABASE`; other databases continue.

---

## 4. Oracle object discovery

### 4.1 Source and criteria

Single set-based query against `DBA_OBJECTS`, per database, per run unit.

Predicates:

| Predicate | Purpose |
|---|---|
| `LAST_DDL_TIME > :watermark` | **The change-detection criterion.** Omitted entirely on full export. |
| `OBJECT_TYPE IN (:configured types)` | Restrict to the 12 supported types |
| `STATUS IN ('VALID','INVALID')` | Requirement: only these are exported |
| `OWNER IN (:include)` / `OWNER NOT IN (:exclude)` | Schema filters, applied **in SQL** |
| `ORACLE_MAINTAINED = 'N'` | Exclude Oracle-maintained objects (12c+); a static deny-list of internal schemas is applied additionally and covers pre-12c |
| `GENERATED = 'N'` | Exclude system-generated names |
| `SECONDARY = 'N'` | Exclude domain-index secondary objects |
| `SUBOBJECT_NAME IS NULL` | Exclude partitions/subpartitions (they belong to the table's DDL) |
| `OBJECT_NAME NOT LIKE 'BIN$%'` | Exclude recycle-bin objects |

Ordering: `OWNER, OBJECT_TYPE, OBJECT_NAME` — required, and it also makes the object list
in the commit body deterministic.

All filters are passed as bind variables / bound collections. No string interpolation of
configuration values into SQL.

### 4.2 Watermark

The watermark is `last_successful_run` for that **schema** from `execution_state.json`
(§4.4). It was per-database until decision D-15.

- **Clock source:** the *database* clock, not the runner clock. At run-unit start the
  agent reads the database time (`SYSTIMESTAMP`) and records it as the *candidate* new
  watermark. It is only persisted after a successful push. This avoids missed changes
  caused by runner/database clock skew.
- **Safety overlap:** a configurable overlap (default 5 minutes) is subtracted from the
  watermark used in the query. `LAST_DDL_TIME` is a `DATE` (second granularity) and DDL
  that commits slightly after the watermark was captured could otherwise be missed. The
  cost of re-processing an object is only extraction time, because a re-written identical
  file produces no Git diff.
- **No state file:** the predicate is omitted and every matching object is exported (full
  export).

### 4.3 Output

An ordered list of `OracleObject` value objects carrying owner, type, name, status,
`created`, `last_ddl_time`, and the database name. This list is the work queue for
extraction and also the basis for the "objects discovered" log counter.

### 4.4 Per-schema state (`execution_state.json` v2)

State is keyed by database **and schema**. A schema's watermark advances only when that
schema's own extract → commit → push cycle has completed (decision D-15).

```json
{
  "schema_version": 2,
  "databases": {
    "CLIENT_DEV": {
      "database_name": "CLIENT_DEV",
      "schemas": {
        "APP_SCHEMA_1": {
          "schema_name": "APP_SCHEMA_1",
          "last_successful_run": "2026-08-31T07:54:38.286542+00:00",
          "commit_sha": "296cace…",
          "execution_status": "SUCCESS",
          "processed_objects": 3407,
          "execution_duration_seconds": 153.12
        }
      }
    }
  }
}
```

Three properties follow from this shape, and each solves a defect in the v1 model:

1. **Adding a schema is self-correcting.** A schema with no entry has no watermark, so it
   receives a full export automatically. Under v1, adding a schema to the include filter
   inherited the database's watermark and silently skipped everything that had changed in
   that schema beforehand, while reconciliation — which is never incremental — applied to
   it in full. Deletions took effect; content updates did not.
2. **A failure cannot poison an unrelated schema.** Under v1 one watermark covered every
   schema, so advancing it after a partial run claimed currency the repository did not
   have.
3. **Reconciliation is scoped to the schema being processed** (§9.3), which also makes the
   mass-deletion threshold a percentage of that schema's files rather than the whole
   database's (decision D-6, amended).

**A schema records its first watermark even when nothing changed** (decision D-19). State
is otherwise written only when a commit happens, and that rule alone leaves a schema which
is *already* in sync permanently un-watermarked: it exports in full, produces no diff,
records nothing, and does the same again on the next run forever. That is not hypothetical
— it is exactly the state D-18's migration puts every schema into, and the first
post-migration run reproduced it: two schemas, 6,700 objects, five minutes, no commit, no
state. So a schema holding **no** watermark records one on its first successful
completion, in a commit carrying only the state file. A schema that already has one keeps
the existing rule and stays silent. The churn is therefore bounded at one commit per
schema, ever.

**Migration from v1.** A v1 file records one watermark per database and *does not record
which schemas were in scope when it was written*. Nothing in the file can prove the
watermark applies to any particular schema, so the migration **discards v1 watermarks**
and every schema takes one full export (decision D-18). The alternative — attributing the
watermark to whichever schemas happen to be configured now — would silently skip changes
for any schema added since, which is precisely the undetectable data loss this design
exists to prevent. Identical content still produces no Git diff, so the cost is extraction
time, not commit churn.

---

## 5. DDL extraction

### 5.1 Dispatcher

`src/oracle/metadata.py` holds a type → strategy table and is the only place that decides
which mechanism is used. Adding a type means adding one table entry plus one strategy.

| Object type | Strategy | Mechanism |
|---|---|---|
| PACKAGE | `SourceStrategy` | `DBA_SOURCE`, `ORDER BY LINE` |
| PACKAGE BODY | `SourceStrategy` | `DBA_SOURCE`, `ORDER BY LINE` |
| PROCEDURE | `SourceStrategy` | `DBA_SOURCE`, `ORDER BY LINE` |
| FUNCTION | `SourceStrategy` | `DBA_SOURCE`, `ORDER BY LINE` |
| TYPE | `SourceStrategy` | `DBA_SOURCE`, `ORDER BY LINE` |
| TYPE BODY | `SourceStrategy` | `DBA_SOURCE`, `ORDER BY LINE` |
| TRIGGER | `SourceStrategy` | `DBA_SOURCE`, `ORDER BY LINE` |
| VIEW | `SourceStrategy` | `DBA_SOURCE`, `ORDER BY LINE` |
| TABLE | `ColumnStrategy` | `DBA_TAB_COLUMNS`, `ORDER BY COLUMN_ID` |
| SEQUENCE | `MetadataStrategy` | `DBMS_METADATA.GET_DDL` |
| SYNONYM | `DictionaryStrategy` | `DBA_SYNONYMS` |
| MATERIALIZED VIEW | `MetadataStrategy` | `DBMS_METADATA.GET_DDL` |

### 5.2 Line-ordered extraction (`DBA_SOURCE`)

- One query per object, bound on `OWNER`, `NAME`, `TYPE`, `ORDER BY LINE`.
- Lines are joined in retrieved order. Interior content of a line is never trimmed,
  reflowed, or reordered at this stage — that is normalization's job.
- Batch fetching via a tuned `arraysize`/`prefetchrows` so a large package body is one
  round trip, not thousands.
- The `CREATE OR REPLACE` prefix is added by the extractor because `DBA_SOURCE` line 1
  begins at the object keyword. Formatting of that prefix is then normalized.

### 5.3 `DBMS_METADATA` extraction

Used for `SEQUENCE` and `MATERIALIZED VIEW`.

Transform parameters are set **once per session**, before any extraction, via
`DBMS_METADATA.SET_TRANSFORM_PARAM` (a read-only, session-scoped call):

| Parameter | Value | Effect |
|---|---|---|
| `PRETTY` | true | Deterministic, readable layout |
| `SQLTERMINATOR` | true | Consistent statement terminator |
| `STORAGE` | false | Suppresses `STORAGE(...)` at the source |
| `SEGMENT_ATTRIBUTES` | false | Suppresses `TABLESPACE`, `PCTFREE`, `INITRANS`, `LOGGING`, … |
| `TABLESPACE` | false | Suppresses tablespace clauses |
| `EMIT_SCHEMA` | true | Schema-qualified, stable output |

**Design rationale:** removing storage noise *in Oracle* is far safer than removing it
with regular expressions afterwards. The textual `storage_clause_remover` (§6) is the
second line of defense for anything the transforms do not cover, and for output that does
not come from `DBMS_METADATA` at all.

`DBMS_METADATA` returns a CLOB; it is read fully and never truncated.

### 5.4 Non-source dictionary strategies

- **TABLE** — column list assembled from `DBA_TAB_COLUMNS` ordered by `COLUMN_ID`
  (data type, length/precision/scale, nullability, default), rendered as a
  `CREATE TABLE` statement. This is the mapping specified in the requirements.
- **SYNONYM** — rendered from `DBA_SYNONYMS` as
  `CREATE OR REPLACE [PUBLIC] SYNONYM … FOR …`.

### 5.5 Validation before writing

Extracted source is validated before it reaches the normalizer or the writer:

- Not `None`, not empty, not whitespace-only.
- Contains the expected leading construct for its object type.
- No CLOB-truncation marker; length consistent with what the database reported.

Validation failure ⇒ `FAILED_EXTRACTION` for that object, existing repository file left
untouched, processing continues.

### 5.6 No duplicate work

Discovery produces a de-duplicated set keyed by `(owner, type, name)`. Each key is
extracted at most once per execution.

---

## 6. DDL normalization

### 6.1 Pipeline

A fixed, ordered chain of pure functions. Order is part of the contract because
determinism depends on it.

```
raw source
  → 1. decode / unify line endings to LF
  → 2. expand tabs to spaces
  → 3. strip trailing whitespace per line
  → 4. remove storage / physical / environment clauses
  → 5. normalize CREATE OR REPLACE header formatting
  → 6. normalize quoted identifiers (only when safely unquotable)
  → 7. normalize keyword casing (literal- and comment-aware)
  → 8. collapse repeated blank lines
  → 9. normalize statement terminator
  → 10. ensure exactly one trailing newline
normalized source
```

Steps 8–10 run last so that earlier removals cannot leave residual blank lines or
trailing space behind.

### 6.2 Safety model

The dangerous steps are 4, 6, and 7 — all three could damage code if applied naively.

- A lightweight **lexical scanner** splits the text into regions: string literals,
  `--` comments, `/* */` comments, quoted identifiers, and code. Steps 6 and 7 operate on
  code regions only. A package containing the word `CACHE` in a comment or a literal is
  never touched.
- The storage-clause remover is **parenthesis-aware** so that `STORAGE(...)` is removed as
  a balanced unit rather than by line-based pattern matching.
- Keyword casing is restricted to a fixed, explicit keyword list applied to code regions;
  identifiers are never re-cased.

### 6.3 Determinism guarantees

- No dependence on dict iteration order, locale, timezone, environment variables, or
  filesystem state.
- No timestamps, hostnames, or run identifiers are ever embedded in the output file.
- Property test in CI: `normalize(normalize(x)) == normalize(x)` across the full fixture
  corpus.
- Regression corpus test: for a set of representative PL/SQL objects, the parsed
  token stream of code regions before and after normalization is unchanged (proves logic
  preservation).

### 6.4 Configurability

Each rule is individually switchable in `config/settings.yml`. Defaults are all enabled.
Rule switches are recorded in the log so an unexpected diff can be traced to a
configuration change.

---

## 7. Change detection

**`DBA_OBJECTS.LAST_DDL_TIME` is the sole change-detection mechanism**, as specified.

```
object appears in discovery  ⟺  LAST_DDL_TIME > (last_successful_run − overlap)
```

There is no content comparison stage. Every discovered object is extracted, normalized,
and written.

### 7.1 What `LAST_DDL_TIME` actually reflects

It is important the operator understands the semantics, because they shape the system's
observable behaviour:

- It advances on `CREATE`, `CREATE OR REPLACE`, and `ALTER`.
- It **also** advances on recompilation, on `GRANT`/`REVOKE` against the object, and on
  some dependency-driven invalidation/revalidation events — even when the source text is
  identical.
- It does **not** exist for dropped objects (the row disappears entirely). This is why
  deletion needs a separate mechanism (§9).
- It is a `DATE`: one-second granularity, in the database's timezone.

### 7.2 Consequence, and why it is harmless

Because `LAST_DDL_TIME` is noisier than "the source really changed", the agent will
sometimes re-extract and re-write a file whose content is unchanged.

**Git absorbs this.** Writing byte-identical content produces no working-tree
modification, so `git status` reports nothing, nothing is staged, and no commit is
created. The "avoid unnecessary commits" requirement is satisfied by the version control
system rather than by an application-level comparer — which is simpler, and impossible to
get wrong.

The only cost of a false positive is extraction time.

### 7.3 The two-phase gate

```
Gate 1 — LAST_DDL_TIME  → decides what is EXTRACTED   (application)
Gate 2 — git status     → decides what is COMMITTED   (git)
```

---

## 8. New object detection

A newly created object requires no special handling: `CREATE` sets both `CREATED` and
`LAST_DDL_TIME` to the creation moment, so the object satisfies
`LAST_DDL_TIME > watermark` and is discovered by the normal query.

- The writer creates any missing directories in the path
  (`repository/<DB>/<SCHEMA>/<type_folder>/`) before writing.
- The file is new, so Git reports it as untracked → staged → committed as an addition.
- On the very first execution (no `execution_state.json`), every in-scope object is "new"
  by definition and a full export occurs.

New schemas are handled the same way, provided they pass the include/exclude filters.
A new schema that is not in an explicit include list is intentionally ignored — this is
the point of the filter.

---

## 9. Deleted object detection

`LAST_DDL_TIME` cannot detect drops: when an object is dropped its `DBA_OBJECTS` row
disappears, so an incremental query can never see it. A separate mechanism is required.

### 9.1 Design: inventory reconciliation

A **reconciliation pass**, cheap because it reads metadata only — never source:

1. Query the full in-scope object inventory for the database: `OWNER, OBJECT_TYPE,
   OBJECT_NAME` from `DBA_OBJECTS` with all the §4.1 filters **except** the
   `LAST_DDL_TIME` predicate. One query, no source extraction.
2. Map that inventory to the set of expected file paths using `file_mapper`.
3. Enumerate the actual `.sql` files under `repository/<DATABASE>/`.
4. Any file present on disk but absent from the inventory corresponds to an object that no
   longer exists → stage a deletion (`git rm`).

This is the role of `src/repository/comparer.py` in the specified layout: it compares
**path sets**, not file contents.

### 9.2 When it runs

Proposed default: on every execution. The added cost is one metadata query and one
directory walk per database — negligible relative to extraction. It can be restricted to
a schedule or a `--reconcile` flag if the owner prefers (decision D-1, §23).

### 9.3 Safety rules

Deletion is destructive, so it is deliberately conservative:

- Reconciliation is **skipped entirely** if the inventory query failed or returned an
  implausibly empty result for a schema that previously had objects — a transient
  permissions or connectivity problem must never be interpreted as "everything was
  dropped".
- A configurable safety threshold: if reconciliation would delete more than N% of the
  **schema's** files (default 10%), the run unit logs an `ERROR`, deletes nothing, and
  reports `FAILED_COMPARISON`. A genuine mass drop is then a deliberate human action with
  `--allow-mass-delete`. The scope is per schema rather than per database (decision D-6,
  amended by D-15): a database-wide denominator lets a small schema be wiped entirely
  while staying under the threshold.
- Deletions are never performed in dry-run mode.
- Reconciliation only ever considers paths under the databases and schemas currently in
  scope. Removing a schema from the include filter must not delete its history.

### 9.4 Renames

A rename appears as one deletion plus one creation. Git's rename detection presents this
as a rename in the diff automatically; no special handling is needed.

---

## 10. Repository file structure

### 10.1 Layout

Exactly as specified:

```
repository/<DATABASE_NAME>/<SCHEMA>/<type_folder>/<OBJECT_NAME>.sql
```

| Object type | Folder |
|---|---|
| PACKAGE | `packages/` |
| PACKAGE BODY | `package_bodies/` |
| PROCEDURE | `procedures/` |
| FUNCTION | `functions/` |
| VIEW | `views/` |
| TABLE | `tables/` |
| TYPE | `types/` |
| TYPE BODY | `type_bodies/` |
| TRIGGER | `triggers/` |
| SEQUENCE | `sequences/` |
| SYNONYM | `synonyms/` |
| MATERIALIZED VIEW | `materialized_views/` |

Separate folders for `package_bodies` and `type_bodies` mean a spec and its body are two
distinct files and never collide — this is why the split matters, not just tidiness.

### 10.2 File conventions

| Property | Value | Reason |
|---|---|---|
| Extension | `.sql` (lowercase on disk) | Case-consistent across case-sensitive and case-insensitive filesystems |
| Encoding | UTF-8, no BOM | A BOM would appear as a phantom diff |
| Line endings | LF | Cross-platform stability; enforced additionally by `.gitattributes` |
| Trailing newline | Exactly one | POSIX convention; avoids "\ No newline at end of file" churn |
| Name | Oracle object name, unmodified | Direct, predictable mapping back to the database |

### 10.3 Path mapping rules

- The mapping is total and injective: one object ⇒ exactly one path; one path ⇒ exactly
  one object. Reconciliation (§9) depends on this being invertible.
- Object names containing characters that are illegal or ambiguous on a filesystem
  (`/ \ : * ? " < > |`, control characters, trailing dots/spaces) are **not silently
  encoded**. The object is marked `FAILED_COMPARISON` and logged as `ERROR`. Silent
  encoding would break the invertibility that deletion detection relies on. Such names are
  rare and warrant a human decision.
- A `.gitattributes` at the repository root pins `*.sql text eol=lf` so that a developer
  cloning on Windows cannot introduce CRLF diffs.

---

## 11. Git integration

### 11.1 Mechanism

The `git` CLI driven through a thin `subprocess` wrapper (`git_client.py`), rather than a
Git library. Rationale: no additional dependency, exact and auditable command lines,
predictable behaviour on the detached HEAD that GitLab CI produces, and identical
behaviour locally and in CI. Every invocation is logged (with the remote URL redacted).

### 11.2 Working tree model

The pipeline operates on the checkout that GitLab CI already produced. Because CI checks
out a detached HEAD, the Git phase explicitly checks out the target branch and fast-
forwards it to the remote tip before staging.

### 11.3 Staging

Only paths written by this execution are staged — explicitly, by path. `git add -A` and
`git add .` are forbidden, so an unrelated stray file in the workspace can never be swept
into a commit.

Staged set = files written in the extraction phase + deletions from reconciliation +
`state/execution_state.json`.

### 11.4 The empty-commit gate

Before committing, the pipeline inspects the staged set (`git diff --cached --name-only`).
If it is empty, the pipeline **does not commit, does not push, does not update state**, and
exits successfully. This is the mechanism that makes recompilation-only runs silent.

### 11.5 Commit message

Subject:

```
<database>.<schema>||<sysdate>: Updated Oracle objects
```

The schema component is an amendment to the format `docs/requirements.md` §9 specifies
(`<database>||<sysdate>`), and follows from one commit per schema (decision D-17). Without
it, a run touching four schemas produces four commits whose subjects differ only by
timestamp, and `git log --oneline` stops telling you which schema changed.

Body:

```
Execution time     : <start> → <end> (<duration>)
Number of objects  : <n>
Previous commit    : <sha>
New execution ts   : <timestamp>

Objects:
  <OWNER>.<OBJECT_NAME> (<OBJECT_TYPE>) [MODIFIED|ADDED|DELETED]
  ...
```

The object list is ordered by `OWNER, OBJECT_TYPE, OBJECT_NAME`, so two runs producing the
same change set produce identical message bodies. Every object in one commit belongs to
the one schema that commit is for, so no grouping is needed.

### 11.6 Push

- Push to the configured branch over HTTPS, using a token supplied by a CI/CD variable and
  injected into the remote URL at call time. The URL is never logged, never written to
  `.git/config` in a form that persists in artifacts, and is redacted in all error output.
- On rejection due to a non-fast-forward (a concurrent pipeline pushed first), the client
  fetches, rebases, and retries within a bounded budget (§15).
- Push failure ⇒ abort, no state update, non-zero exit.

### 11.7 Preventing pipeline recursion

The agent pushes to the same repository that triggers it. Two independent guards:

1. `[skip ci]` in the commit message.
2. A `rules` clause in `.gitlab-ci.yml` that runs the job only for `schedule` and `web`
   pipeline sources, so a `push` event cannot start it.

Either alone would suffice; both are used because a pipeline loop is expensive and
embarrassing.

### 11.8 Concurrency

`resource_group` in `.gitlab-ci.yml` serializes pipeline executions of this job so two
scheduled runs cannot race on the same branch.

### 11.9 State commit ordering

State must not be persisted before the push succeeds, yet it must be committed *with* the
source changes. The sequence runs **once per schema** (decision D-3, amended by D-17):

1. Stage that schema's DDL changes → commit → push. If this fails, nothing has been
   persisted for this schema and the execution stops (§14.3).
2. On success, write that schema's entry into `execution_state.json`, `git commit --amend`,
   and force-push with lease (`--force-with-lease`) to the same branch.

`--force-with-lease` (never a bare `--force`) refuses to overwrite anything that arrived
after our fetch, and is given an explicit expected SHA — the commit we ourselves just
pushed — rather than relying on a remote-tracking ref that pushing by URL never updates.

One case commits without any DDL change: a schema recording its first watermark (D-19).
Its subject is ``<database>.<schema>||<ts>: Recorded initial watermark``, so it is never
mistaken for a DDL change in the log, and its ``commit_sha`` in state is ``null`` because
no DDL landed in it.

An execution therefore produces **one commit per changed schema**, not one per run. Only
schemas that actually changed produce a commit; a schema whose re-extracted content is
byte-identical stages nothing, commits nothing, and leaves its watermark untouched
(§11.4). Because each schema's state is written between its own push and the next
schema's work, an execution that fails partway leaves every completed schema correctly
recorded.

---

## 12. Configuration

### 12.1 Files

| File | Contents |
|---|---|
| `config/databases.yml` | Per-database: name, DSN reference, user reference, password reference, git folder, schema include/exclude, object type list, enabled flag |
| `config/settings.yml` | Global behaviour: concurrency, watermark overlap, batch/array sizes, normalization rule switches, reconciliation policy, retry budgets, branch name |
| `config/logging.yml` | Log level, format (text/JSON), sinks |

### 12.2 Secret referencing

Configuration files contain **the names of secrets, never their values**:

```yaml
databases:
  - name: HRDB
    dsn_env: HRDB_DSN
    username_env: HRDB_USER
    password_env: HRDB_PASSWORD
```

A `SecretProvider` interface resolves those references at runtime. Implementations:
environment variable (default), Vault, cloud Secrets Manager, encrypted file. The rest of
the application never knows which provider is in use.

### 12.3 Precedence

`CLI arguments > environment variables > settings.yml > built-in defaults`

### 12.4 Validation

Performed at start-up, before any connection is attempted. Fatal on: malformed YAML,
unknown keys, duplicate database names, unknown object types, missing required secret
references, unresolvable secrets, invalid git folder paths, contradictory include/exclude
filters. A configuration error exits with code `2` and touches nothing.

---

## 13. Logging

### 13.1 Format

Structured records — key/value or JSON, selectable in `logging.yml`. JSON is the default
in CI so log aggregation can parse it; text is the default locally for readability.

Standard fields on every record: timestamp (UTC, ISO-8601), level, logger, message,
`execution_id`, and `database` where applicable.

### 13.2 Per-execution summary

Emitted once per run unit, at `INFO`, containing exactly the fields required:
database, start time, end time, duration, objects discovered, objects exported, objects
changed, objects skipped, objects failed, commit SHA, push status.

A final execution-level summary aggregates all run units.

### 13.3 Levels

| Level | Used for |
|---|---|
| `INFO` | Lifecycle, counts, summaries, commit SHA, push result |
| `WARNING` | Per-object failures, retries, skipped objects, reconciliation suppressed by threshold |
| `ERROR` | Database-level failure, push failure, configuration error, unparseable state |

### 13.4 Redaction

A logging filter installed on the root logger redacts, in every record and every
exception traceback: password values, token values, and any DSN containing an embedded
credential. Redaction happens at the filter, so no call site can leak by forgetting.

A unit test asserts that a log stream produced with a known sentinel password never
contains that sentinel.

### 13.5 Sinks

stdout (captured by GitLab) always; an optional rotating file under `logs/` for local
debugging, git-ignored.

---

## 14. Error handling

### 14.1 Exception hierarchy

```
OracleVersioningError
├── ConfigurationError          → fatal, exit 2
├── StateError                  → fatal, exit 2  (unparseable execution_state.json)
├── DatabaseError               → run-unit fatal
│   ├── ConnectionError
│   └── DiscoveryError
├── ObjectError                 → object-scoped
│   ├── ExtractionError
│   └── NormalizationError
├── RepositoryError             → object-scoped (write/mapping) or run-unit (reconciliation)
└── GitError                    → execution fatal
    ├── CommitError
    └── PushError
```

### 14.2 Status model

Per-object statuses, as specified: `SUCCESS`, `FAILED_EXTRACTION`,
`FAILED_NORMALIZATION`, `FAILED_COMPARISON`, `FAILED_GIT`, `FAILED_PUSH`,
`FAILED_DATABASE`.

Every object carries its own status; the run unit aggregates counts; the execution
aggregates run units.

### 14.3 Blast radius

| Failure | Scope | Behaviour |
|---|---|---|
| Invalid configuration | Execution | Abort before connecting. Exit 2. |
| Unparseable state file | Execution | Abort. Never silently recreate — that would look like "no history" and trigger a full export that masks data loss. Exit 2. |
| Oracle connection fails | Execution | Retry per §15, then `FAILED_DATABASE`. **Stops the execution** (D-17); no later schema or database is attempted. |
| Discovery query fails | Execution | `FAILED_DATABASE`. **Stops the execution.** No files written for that schema. |
| Schema list cannot be resolved | Execution | `FAILED_DATABASE`. **Stops the execution.** Guessing the schema list would silently narrow scope. |
| Extraction fails for one object | Object | `FAILED_EXTRACTION`. Existing file untouched. The schema continues. |
| Normalization fails for one object | Object | `FAILED_NORMALIZATION`. Existing file untouched. The schema continues. |
| File write fails | Object | `FAILED_COMPARISON`. Logged. The schema continues. |
| Reconciliation threshold exceeded | Schema | No deletions staged. `ERROR` logged. That schema's extraction results still commit. |
| Commit fails | Execution | `FAILED_GIT`. This schema's watermark is not advanced. **Stops the execution.** Non-zero exit. |
| Push fails | Execution | Retry per §15, then `FAILED_PUSH`. **This schema's watermark is not advanced. Stops the execution.** Non-zero exit. |

**Object-level failures remain contained; everything above them now stops the execution**
(decision D-17). This is a deliberate reversal of the previous "a failure in one database
must not stop the others" behaviour, and it is safe only because state is committed per
schema: work completed before the failure is already pushed and recorded, so stopping
loses nothing and the next run resumes at the schema that failed. Under the previous
per-run state model the same rule would have discarded the entire run's progress.

### 14.4 Fail-safe guarantees

- Files are written atomically (temp file in the same directory, then rename), so an
  interrupted execution can never leave a half-written `.sql` file.
- `execution_state.json` is written atomically by the same mechanism, so the previous
  state is never lost.
- If the Git phase aborts, the working tree is restored so the next execution starts
  clean.
- Every exception is logged with object identity, object type, and cause before being
  converted to a status.

### 14.5 Exit codes

| Code | Meaning |
|---|---|
| `0` | Success, including "nothing changed" |
| `1` | A schema failed, or commit/push failed — the execution stopped there |
| `2` | Configuration invalid or state file unparseable |

The codes are unchanged; what `1` means has narrowed. It previously meant "some database
failed, the rest were still synchronized". It now means "the execution stopped at the
first failure", and the summary names the schema it stopped on together with the schemas
that completed before it. The requirement "the pipeline must fail if an Oracle connection
cannot be established" is still met. The requirement "a failure in one database must not
stop the others" (`docs/requirements.md` §23) is **deliberately not met** — see D-17;
per-schema state makes stopping safe, because completed work is already durable.

---

## 15. Retry strategy

Retries apply only to failures that are plausibly transient. A deterministic failure is
never retried — retrying it only wastes time and multiplies log noise.

| Operation | Retried | Attempts | Backoff | Rationale |
|---|---|---|---|---|
| Oracle connect | Yes | 3 | Exponential, 2s → 4s → 8s, ±20% jitter | Listener restarts, brief network faults |
| Discovery query | Yes, if the error is a connectivity/session class error | 2 | Exponential | Session killed mid-query |
| Object extraction | Yes, once, for connectivity-class errors only | 1 | 2s | A single dropped session should not lose an object |
| Object extraction, logical errors (missing object, insufficient privilege, wrapped-source failure) | **No** | — | — | Deterministic; will fail identically |
| Normalization | **No** | — | — | Pure function; deterministic |
| File write | **No** | — | — | Disk errors are environmental, not transient |
| `git push`, non-fast-forward | Yes | 3 | Fetch + rebase between attempts, 1s → 2s → 4s | A concurrent pipeline pushed first |
| `git push`, auth/permission failure | **No** | — | — | Deterministic; a token problem needs a human |
| Reconciliation query | Same policy as discovery | 2 | Exponential | |

Global guards: each retried operation has an overall time budget, and the run unit has a
wall-clock timeout, so no execution can hang the pipeline until the runner timeout.

An object that disappears between discovery and extraction (dropped mid-run) is **not** a
failure — it is logged at `WARNING`, marked skipped, and picked up by reconciliation on
this or the next run.

---

## 16. Dry-run mode

`--dry-run` proves what an execution *would* do, while touching nothing.

| Layer | Behaviour under `--dry-run` |
|---|---|
| Oracle | Fully live — connects, discovers, extracts. This is read-only anyway, and the extraction is the part worth validating. |
| Normalizer | Fully live |
| Repository writer | **Suppressed.** No file created, modified, or deleted. |
| Reconciliation | Computed and reported; no deletion staged. |
| Git | **Suppressed.** No `add`, `commit`, or `push`. |
| State | **Suppressed.** `execution_state.json` never written. |

Suppression is implemented by injecting no-op writer and Git implementations, not by
scattering `if dry_run:` checks through the business logic — so a dry run exercises the
same code path as a real run, and there is no branch where a stray write could survive.

Output: a full report of objects discovered, objects that would be written (with target
paths), objects that would be deleted, per-object statuses, and the commit message that
would have been produced. Exit code reflects extraction outcomes only.

A related flag, `--no-git`, writes files but skips commit/push — useful for inspecting
normalized output locally.

---

## 17. Testing strategy

### 17.1 Principle

The unit suite runs with **no Oracle, no network, no GitLab**. Anything requiring a real
database is marked and skipped by default. This keeps CI fast and makes the suite runnable
on any laptop.

### 17.2 Coverage by area

| Area | Approach |
|---|---|
| Configuration | Valid/invalid YAML, missing secrets, duplicate database names, unknown object types, filter contradictions, precedence order |
| Connection | Fake pool; retry/backoff sequencing with a controlled clock; **read-only guard test that scans every SQL constant for forbidden keywords** |
| Discovery | Query construction asserted against expected predicates and bind names; filter logic verified without a database |
| Extraction | Fake cursors returning ordered `DBA_SOURCE` rows — asserts line order preserved, no truncation, validation rejects empty/garbage |
| Normalization | Golden files per object type (input → expected); idempotency property test; literal/comment-safety corpus proving `CACHE`, `STORAGE` etc. inside strings and comments survive; logic-preservation regression corpus |
| File mapping | Every object type → expected path; invertibility (path → object) property test; illegal-name handling |
| Writer | Atomic write, directory creation, encoding/LF/trailing-newline enforcement, dry-run suppression |
| Reconciliation | Path-set differencing; safety threshold blocks mass deletion; suppressed when inventory query failed |
| Git | Real temporary repository under `tmp_path`: only-changed-files staged, **no empty commit**, message format, rebase/retry on non-fast-forward, redaction of remote URL |
| State | Round-trip serialization; missing file ⇒ full export; corrupt file ⇒ fatal; **never written before push succeeds** |
| Logging | Sentinel password never appears in any emitted record or traceback |
| End-to-end | Fake Oracle layer + temp Git repository, asserting each behavioural scenario in §22 |
| Integration | `@pytest.mark.oracle`, skipped unless a DSN is provided; run against a disposable instance |

### 17.3 CI enforcement

Lint (PEP 8), type check, and the full offline unit suite run on every merge request. The
read-only guard test and the no-secrets-in-logs test are treated as blocking.

---

## 18. Docker architecture

### 18.1 Image

- Base: `python:3.13-slim`. Slim keeps the attack surface and pull time down; thin-mode
  `oracledb` means no Instant Client is needed, so a full OS image buys nothing.
- Installed: `git`, `ca-certificates`, and the Python requirements.
- Multi-stage build: dependencies resolved in a builder stage, only the resulting
  site-packages and application copied into the runtime stage.

### 18.2 Runtime

| Property | Value |
|---|---|
| User | Non-root, created in the image |
| Workdir | `/app` |
| Entrypoint | The agent, one synchronization, then exit |
| Secrets | Environment only — never baked into a layer, never in `ARG`/`ENV` defaults |
| Filesystem | Only the checked-out repository is written; no local state persists |

### 18.3 Determinism

Pinned base image digest and fully pinned `requirements.txt`, so an image rebuilt in six
months behaves identically. `LANG`/`LC_ALL` pinned to a UTF-8 locale and `TZ` to UTC, so
container locale can never alter normalized output.

### 18.4 Local use

The same image runs locally against a `.env` file (git-ignored) with the repository
mounted, so a developer reproduces CI behaviour exactly.

---

## 19. GitLab Runner architecture

- **Executor:** Docker executor running the image from §18. No shell executor — a shell
  runner would leave state between executions and break the stateless requirement.
- **Network:** the runner must reach both the Oracle listeners and the GitLab instance.
  For on-premise databases this usually means a self-hosted runner inside the network
  perimeter, tagged so only this job targets it.
- **Tags:** the job carries a tag matching the runner that has database access.
- **Clean environment:** every execution starts from a fresh container and a fresh
  checkout. Nothing persists between runs except what is in Git.
- **Checkout:** the job needs full repository history and must run as a real branch
  checkout, not a shallow detached snapshot, so that rebase and push work. Fetch depth is
  configured accordingly.
- **Serialization:** `resource_group` ensures only one execution of this job runs at a
  time.
- **Timeout:** a job timeout comfortably above the expected full-export duration; the
  application's own wall-clock guard trips first in normal operation.

---

## 20. GitLab CI/CD pipeline

### 20.1 Stages

| Stage | Job | Runs on |
|---|---|---|
| `validate` | Lint, type check, unit tests | Merge requests, manual |
| `build` | Build and push the Docker image | Changes to `Dockerfile`/`requirements.txt`, manual |
| `sync` | The synchronization job | **Scheduled** and **manual (web)** |

### 20.2 The `sync` job

```
rules   : run only when pipeline source is `schedule` or `web`   (blocks push-triggered recursion)
image   : the pinned agent image
tags    : the runner with database network access
resource_group : oracle-versioning-sync                          (serializes executions)
script  :  1. configure git identity (bot name/email from variables)
           2. checkout the target branch, fast-forward to remote
           3. run the agent (one synchronization)
           4. agent performs its own commit and push
artifacts: execution logs and the run summary, retained for audit
```

The job does **not** contain business logic; it is a thin wrapper. Everything meaningful
lives in the application, so the same behaviour is reproducible locally.

### 20.3 Scheduling

A GitLab Pipeline Schedule at the desired cadence, targeting the versioning branch. The
schedule is the only automatic trigger.

### 20.4 Pass/fail contract

| Outcome | Pipeline result |
|---|---|
| No Oracle objects changed | **Success**, no commit |
| Objects changed, committed and pushed | **Success** |
| Object already synchronized | **Success** |
| Oracle connection cannot be established | **Failure** |
| Source extraction fails unexpectedly | **Failure** |
| Git push fails | **Failure** |
| `execution_state.json` unparseable | **Failure** |
| Configuration invalid | **Failure** |

### 20.5 Future merge-request pipelines

The structure already supports it: the `validate` stage runs on merge requests today, and
a future MR job can run the agent in `--dry-run` to report what would change without
touching the repository.

---

## 21. Security

### 21.1 Credentials

- No credential in code, configuration, tests, fixtures, or commit history.
- Configuration references secrets by **name**; a `SecretProvider` resolves them at
  runtime (environment variable, Vault, Secrets Manager, encrypted file).
- GitLab CI/CD variables are **masked** and **protected**.
- Each database may have entirely independent credentials.

### 21.2 Database privileges

The Oracle account is read-only by grant, not merely by convention:
`CREATE SESSION`, plus `SELECT_CATALOG_ROLE` (or targeted `SELECT` on `DBA_OBJECTS`,
`DBA_SOURCE`, `DBA_TAB_COLUMNS`, `DBA_SYNONYMS`, `DBA_VIEWS`, `DBA_TRIGGERS`,
`DBA_SEQUENCES`, `DBA_MVIEWS`), plus `EXECUTE ON DBMS_METADATA`. No privilege that permits
DML or DDL on application schemas.

A start-up pre-flight check verifies the required objects are readable and reports exactly
which grants are missing, rather than failing obscurely mid-run.

### 21.3 Git token

A project access token scoped to `write_repository` on this project only — not a personal
token, not `CI_JOB_TOKEN` (which cannot push by default). Injected at call time, never
persisted to `.git/config` in a form that survives into artifacts, always redacted in
logs.

### 21.4 Logging

Redaction filter on the root logger covering passwords, tokens, and credential-bearing
DSNs, applied to messages *and* exception tracebacks. Test-enforced.

### 21.5 Repository hygiene

`.gitignore` covers `.env`, `logs/`, `*.log`, virtual environments, `__pycache__`,
`.pytest_cache`, wallets, and `*.ora` client files. Secret scanning runs in the `validate`
stage.

### 21.6 Container

Non-root user, pinned base digest, no secrets in image layers, minimal package set.

### 21.7 Data sensitivity

Exported DDL is source code, not data — the agent never reads table contents. Even so,
DDL can reveal schema design, so repository visibility should be restricted to the same
audience trusted with database access. Default values and check constraints occasionally
embed business rules, which is a reason to keep the repository private.

---

## 22. Behavioural scenarios

### 22.1 An object is created

1. `CREATE` sets `LAST_DDL_TIME` to now, which is greater than the watermark.
2. Discovery returns the object in the normal incremental query — no special path.
3. Extraction, normalization, and writing proceed; missing directories are created.
4. Git sees an untracked file → staged as an addition → committed → pushed.
5. State advances after the push.

**Log:** `objects discovered +1`, `objects exported +1`, `objects changed +1`.

### 22.2 An object is altered

Identical to creation, except the target file already exists and is overwritten. Git
reports a modification, and the commit shows a diff of the actual DDL change. Because the
stored file is normalized, the diff contains the logical change only — no tablespace or
whitespace noise.

### 22.3 An object is unchanged

Two distinct cases, and the distinction matters:

**(a) No DDL activity at all.** `LAST_DDL_TIME` is below the watermark. The object is not
discovered, not extracted, not written. Zero cost.

**(b) `LAST_DDL_TIME` moved but the source did not** — a recompile, a `GRANT`, or a
dependency revalidation. The object *is* discovered and *is* extracted, normalized, and
written. The bytes are identical to what is already committed, so Git reports no
modification, nothing is staged, and **no commit is created**. State is not updated.

Case (b) is the reason the two-gate design (§7.3) exists: `LAST_DDL_TIME` is a
deliberately generous filter, and Git is the precise one.

If *no* object in the entire execution produces a change, the pipeline exits `0` having
committed nothing and touched no state — satisfying the idempotency requirement exactly.

### 22.4 An object is dropped

`LAST_DDL_TIME` cannot signal this: the `DBA_OBJECTS` row is simply gone. The incremental
query will never return the object again, and without reconciliation its `.sql` file would
linger forever.

Reconciliation (§9) resolves it:

1. Full in-scope inventory is queried (metadata only).
2. The file on disk has no matching object.
3. Subject to the safety threshold, `git rm` stages the deletion.
4. The commit records the removal; the object's history remains fully available in Git.

If the drop happens *between* discovery and extraction within a single run, extraction
raises a "does not exist" error. This is **not** treated as a failure: it is logged at
`WARNING`, the object is skipped, and reconciliation removes the file.

### 22.5 DDL extraction fails

Scope is the object, never the run.

1. The error is classified: connectivity-class (retry once) versus logical-class
   (privileges, object vanished, wrapped source, malformed output — no retry).
2. On final failure the object is marked `FAILED_EXTRACTION` and logged at `WARNING` with
   owner, type, name, and cause.
3. **The existing repository file is left untouched.** A failed extraction must never
   overwrite a good file with empty or partial content — this is what the §5.5 validation
   gate protects.
4. The run unit continues with the next object.
5. The execution summary reports the failure count; the exit code reflects it.

Because the watermark only advances on a successful push, and the failed object's
`LAST_DDL_TIME` is unchanged, the object is naturally retried on the next execution. No
special retry queue is needed.

### 22.6 Oracle becomes unavailable

**At connect time:** retried per §15 (3 attempts, exponential backoff). If all fail, the
run unit ends with `FAILED_DATABASE`. Its files are not written and its state is not
advanced. **Other configured databases continue normally** and their changes are still
committed and pushed. The execution exits non-zero so the pipeline is visibly red.

**Mid-run:** the current object's extraction fails and is retried once; if the session
cannot be re-established, the run unit aborts with `FAILED_DATABASE`. Objects already
extracted and written before the outage are legitimate and are still committed — their
content is correct. However, the state for that database is **not** advanced, because the
run did not complete. The next execution re-scans from the same watermark, re-extracts
those objects, produces identical files, and Git records no change for them. Correctness
is preserved at the cost of some repeated extraction.

**Key invariant:** a database outage never produces a partial or corrupted file, and never
advances a watermark past work that was not finished.

### 22.7 Git push fails

Push failure is execution-fatal, by design.

1. **Non-fast-forward** (a concurrent pipeline pushed first): fetch, rebase, retry — up to
   3 attempts with backoff. This resolves the common case automatically.
2. **Authentication / permission failure:** not retried. A token that is expired, revoked,
   or wrongly scoped will fail identically on every attempt.
3. **Network failure:** retried within the same budget.

On final failure:

- `execution_state.json` is **not** updated. This is the single most important rule in the
  design: the watermark must never advance past changes that did not reach GitLab.
- The local commit exists but is unpushed. The working tree is left clean.
- Status `FAILED_PUSH` is recorded; an `ERROR` is logged with the redacted remote.
- The execution exits non-zero and the pipeline fails.

**Recovery is automatic.** The next execution starts from the same watermark, rediscovers
the same objects, re-extracts them, and writes identical files. The runner's workspace is
fresh, so the orphaned local commit from the failed run does not exist; the changes are
simply recomputed and committed again. No manual intervention, no lost changes, no
duplicate history.

---

## 23. Approved decisions

All decisions below are approved. They are binding and may not be changed without owner
approval (`CLAUDE.md` §8). D-1…D-6 date from the original design; D-15…D-18 were approved
on 2026-08-31 and amend D-3 and D-6 where noted.

| ID | Decision | Approved position |
|---|---|---|
| D-1 | Deleted-object reconciliation frequency | **Every execution.** Costs one metadata query and one directory walk; keeps the repository faithful. |
| D-2 | `ALTER SESSION` for NLS and `DBMS_METADATA` transform parameters | **Permitted.** Session-scoped, changes nothing in the database, and buys determinism that client-side normalization cannot match. |
| D-3 | State commit strategy | **Amend + `--force-with-lease`** — one commit per **schema** (amended by D-17; originally one per run). Safe because `resource_group` serializes executions. Never a bare `--force`. |
| D-4 | Git access mechanism | **`git` CLI via `subprocess`.** No Git library dependency. |
| D-5 | Illegal/unsafe object names | **Fail the object loudly** (`FAILED_COMPARISON` + `ERROR`). Never silently encode — encoding would break the path↔object invertibility that deletion detection depends on. |
| D-6 | Mass-deletion safety threshold | **10%** of a **schema's** files (amended by D-15; originally a database's). Exceeding it blocks all deletions for that schema and logs `ERROR`; an intentional mass drop requires `--allow-mass-delete`. |

### Approved 2026-08-31 — per-schema processing

| ID | Decision | Approved position |
|---|---|---|
| D-15 | Unit of work and state granularity | **The schema, not the database.** `execution_state.json` moves to v2, keyed by database and schema, and each schema carries its own watermark advanced only by its own successful push (§4.4). Reconciliation and the D-6 threshold are scoped to the schema being processed. |
| D-16 | Concurrency | **Sequential throughout** — the cross-database thread pool is removed (§1.3). Per-schema commits and a shared Git index cannot coexist with concurrent extraction without either buffering everything until the end (forfeiting durable partial progress) or corrupting the index. Recoverable later by extracting into per-schema buffers and committing sequentially, if extraction time ever becomes the constraint. |
| D-17 | Failure semantics | **Stop the execution at the first schema-level failure.** No later schema or database is attempted; the failing schema's watermark is not advanced. Object-level failures stay contained as before. This knowingly departs from `docs/requirements.md` §23 ("a failure in one database must not stop the others") and is safe only because D-15 makes completed work durable before the failure. |
| D-18 | v1 → v2 state migration | **Discard v1 watermarks.** A v1 file does not record which schemas were in scope when its watermark was written, so no attribution can be proven. Every schema takes one full export. Attributing it to the currently-configured schemas would silently skip changes for any schema added since — the same class of undetectable data loss the timezone defect produced. |

| D-19 | First watermark for an in-sync schema | **Recorded, in a state-only commit.** A schema with no watermark records one on its first successful completion even when nothing changed; a schema that already has one stays silent. Without this an already-synchronized schema never becomes incremental — it re-exports in full on every run forever. Bounded at one commit per schema, ever. Amends `docs/requirements.md` §22 and `SPEC.MD` ("never updated for metadata-only executions"). |

Commit subjects gain a schema component as a consequence of D-17 — see §11.5. That
amends the format `docs/requirements.md` §9 specifies.

---

## 24. Traceability

Every requirement in `docs/requirements.md` maps to at least one module in §2 and at least
one test area in §17. The full requirement → module → test matrix will be produced as the
first deliverable of the implementation phase, once this architecture is approved.
