# CLAUDE.md

Official working agreement for the **Oracle DDL Versioning Agent**.

This file is normative. Where it conflicts with an ad-hoc instruction, stop and ask.

| | |
|---|---|
| **Architecture status** | **APPROVED** — `docs/architecture.md` v0.1, approved by the owner |
| **Current phase** | Phases 2-8 built; **14 deviations await ratification** — see `docs/traceability.md` |
| **Authoritative documents** | `docs/architecture.md` (design), `docs/requirements.md` (specification), this file (working rules) |
| **Operational documents** | `docs/deployment.md` (setup), `docs/runbook.md` (incident response), `docs/traceability.md` (requirement → module → test) |

> **The specification documents have deliberately not been edited.** Where the
> implementation departs from them, the departure is listed as D-7…D-14 in
> `docs/traceability.md` with its rationale, rather than silently written into
> the spec. §8.3 requires the document to change first and by owner decision;
> that decision is still outstanding. Until it is made, `requirements.md` and
> `architecture.md` remain the authority and those deviations are defects on
> the record.

---

## 1. What this project is

A continuous synchronization agent that keeps a GitLab repository up to date with the
latest source code of Oracle database objects.

**Core behavior:** find objects that changed since the last run, extract their current
source, normalize it, write it, commit, push. There is no content-comparison step — a
changed object's latest normalized source is always written. Git itself is what prevents
unnecessary commits: writing byte-identical content produces no diff, so no commit.

The system is incremental, stateless, read-only against Oracle, and runs entirely inside
GitLab CI/CD with no permanently running server.

more details regarding the structure , architecture and functionality of each component on @SPEC.MD

---

## 2. Approved architecture

The architecture in `docs/architecture.md` is approved and is the official project
architecture. The following are its load-bearing decisions. **None may be changed without
explicit owner approval** (see §8).

### 2.1 Layering

Four layers, dependencies strictly downward. No layer imports from a layer above it.

```
ORCHESTRATION   src/main.py — CLI, wiring, exit code. No business logic.
     │
WORKFLOW        per-database run units, status aggregation, Git phase
     │
DOMAIN          oracle │ normalizer │ repository │ git/state
     │
FOUNDATION      config · models · logging · utils
```

Only the Oracle layer touches a database. Only the repository and Git layers touch the
filesystem. The normalizer is pure functions over strings. Consequence: the normalizer,
file mapper, state model, and commit-message builder are fully testable with no Oracle
instance and no network.

### 2.2 Execution model

One execution = one process = one synchronization = exit.

```
load config (fatal on invalid)
load execution_state.json (fatal on parse error)
EXTRACTION PHASE — concurrent across databases, sequential within a database
    connect → discover → extract → normalize → write
GIT PHASE — serialized, once per execution
    stage written files + reconciliation deletions + state
    nothing staged → exit 0, no commit, no state update
    commit → push (rebase + retry) → update state → amend → push
emit summary → exit code
```

### 2.3 The two-gate design

```
Gate 1 — LAST_DDL_TIME  → decides what is EXTRACTED   (application)
Gate 2 — git status     → decides what is COMMITTED   (git)
```

`DBA_OBJECTS.LAST_DDL_TIME` is the **sole** change-detection mechanism. It is deliberately
generous: it also advances on recompiles, grants, and dependency revalidation. Those false
positives cost only extraction time, because a re-written identical file produces no Git
diff. Git is the precise gate.

### 2.4 Concurrency

| Scope | Model | Reason |
|---|---|---|
| Across databases | Thread pool | Extraction is I/O-bound |
| Within a database | Sequential | Required; keeps session state predictable |
| Git operations | Single-threaded, after all extraction | The index is one shared mutable resource |

### 2.5 Deleted-object detection

`LAST_DDL_TIME` cannot detect drops — the `DBA_OBJECTS` row disappears. Deletion is
handled by **inventory reconciliation**: query the full in-scope object list (metadata
only, no source extraction), map to expected paths, and `git rm` any `.sql` file with no
matching object. `src/repository/comparer.py` compares **path sets, not file contents**.

Guarded by a mass-deletion threshold (default 10%), skipped when the inventory query
failed, and never active in dry-run mode.

### 2.6 Approved decisions (from `docs/architecture.md` §23)

| ID | Decision | Approved position |
|---|---|---|
| D-1 | Reconciliation frequency | Runs on **every execution** |
| D-2 | `ALTER SESSION` for NLS and `DBMS_METADATA` transform parameters | **Permitted** — session-scoped, changes nothing in the database |
| D-3 | State commit strategy | **Amend + `--force-with-lease`** (one commit per run) |
| D-4 | Git access | **`git` CLI via `subprocess`**, no Git library dependency |
| D-5 | Illegal/unsafe object names | **Fail the object loudly** — never silently encode |
| D-6 | Mass-deletion safety threshold | **10%** of a database's files |

---

## 3. Project structure

Authoritative. Do not add, move, or rename modules without approval.

```
oracle-versioning/
├── .gitlab-ci.yml
├── Dockerfile
├── requirements.txt
├── .gitignore
├── .gitattributes
├── CLAUDE.md
├── README.md
│
├── docs/
│   ├── requirements.md
│   └── architecture.md
│
├── config/
│   ├── databases.yml          # secret NAMES only, never values
│   ├── settings.yml
│   └── logging.yml
│
├── state/
│   └── execution_state.json
│
├── src/
│   ├── main.py                # orchestration only
│   ├── config/                loader.py · models.py
│   ├── oracle/                connection.py · object_discovery.py
│   │                          ddl_extractor.py · source_extractor.py · metadata.py
│   ├── normalizer/            ddl_normalizer.py · plsql_normalizer.py
│   │                          storage_clause_remover.py
│   ├── repository/            file_mapper.py · comparer.py · writer.py
│   ├── git/                   git_client.py · commit.py · push.py
│   ├── state/                 execution_state.py · models.py
│   ├── logging/               logger.py
│   ├── utils/                 hashing.py · filesystem.py · timers.py
│   └── models/                oracle_object.py · database.py · execution.py
│
├── tests/                     test_connection.py · test_extractor.py
│                              test_normalizer.py · test_repository.py
│                              test_git.py · test_state.py
│
├── logs/                      # local debugging only, git-ignored
│
└── repository/
    └── <DATABASE>/<SCHEMA>/
        ├── packages/          ├── package_bodies/
        ├── procedures/        ├── functions/
        ├── views/             ├── tables/
        ├── types/             ├── type_bodies/
        ├── triggers/          ├── sequences/
        ├── synonyms/          └── materialized_views/
```

Module responsibilities are defined in `docs/architecture.md` §2. One object maps to
exactly one `.sql` file, and the mapping is invertible — deletion detection depends on it.

---

## 4. Technology choices

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.13+ | Required |
| Oracle driver | `python-oracledb`, **thin mode** default | No Instant Client → smaller image, no native-library CI failures. Thick mode configurable per database. |
| Git | `git` CLI via `subprocess` | No extra dependency, exact command control, predictable on CI's detached HEAD |
| Config | YAML (`PyYAML`) | Required format; readable filter definitions |
| Tests | `pytest` | Fixtures and parametrization suit table-driven normalization tests |
| Container | `python:3.13-slim`, pinned digest, multi-stage, non-root | Minimal surface; thin mode makes a full OS image unnecessary |
| CI/CD | GitLab: schedule + manual, Docker executor | Required |
| Style | PEP 8, type hints, `dataclasses`, `pathlib`, stdlib `logging` | Required |

**Approved dependency set: `python-oracledb`, `PyYAML`, `pytest`.** Anything else needs
written justification (what it does, why the stdlib is insufficient, maintenance status,
license, transitive footprint) and owner approval. Not permitted without approval: ORMs,
async frameworks, SQLAlchemy, pandas, heavyweight config libraries, Git libraries.

---

## 5. Security rules

1. **The Oracle database is strictly read-only.** Never generate, suggest, or execute
   `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`, `ALTER` (except `ALTER SESSION` per
   D-2), `DROP`, `TRUNCATE`, or `GRANT` against a source database. Permitted: dictionary
   `SELECT`s, read-only `DBMS_METADATA` calls, session-scoped `ALTER SESSION`.
2. **Enforced in three layers:** account privileges (the real guarantee), a code-level
   guard rejecting non-`SELECT` leading keywords, and a unit test that scans every SQL
   constant for forbidden keywords. That test is blocking in CI.
3. **No credentials in code, config, tests, fixtures, logs, or commit messages.** Config
   files reference secrets **by name only**; a `SecretProvider` resolves them at runtime
   (environment variable, Vault, Secrets Manager, encrypted file).
4. **Never log a password, token, or credential-bearing DSN.** Redaction is a filter on
   the root logger, applied to messages *and* tracebacks — no call site can leak by
   forgetting. Test-enforced with a sentinel value.
5. **Git token** is a project access token scoped to `write_repository` on this project
   only. Injected at call time, never persisted into `.git/config` in a form that survives
   into artifacts, always redacted in output.
6. **Never commit secrets.** `.gitignore` covers `.env`, `logs/`, `*.log`, virtualenvs,
   `__pycache__`, `.pytest_cache`, wallets, `*.ora`. Secret scanning runs in `validate`.
7. **Container:** non-root user, pinned base digest, no secrets in image layers.
8. **Required Oracle grants:** `CREATE SESSION`, `SELECT_CATALOG_ROLE` (or targeted
   `SELECT` on `DBA_OBJECTS`, `DBA_SOURCE`, `DBA_TAB_COLUMNS`, `DBA_SYNONYMS`, `DBA_VIEWS`,
   `DBA_TRIGGERS`, `DBA_SEQUENCES`, `DBA_MVIEWS`), `EXECUTE ON DBMS_METADATA`. Nothing more.

---

## 6. Testing requirements

**Every feature ships with tests. No test, no merge.**

The unit suite runs with **no Oracle, no network, no GitLab**. Anything needing a real
database is marked `@pytest.mark.oracle` and skipped by default.

| Area | Required tests |
|---|---|
| Configuration | Valid/invalid YAML, missing secrets, duplicate names, unknown object types, precedence |
| Connection | Fake pool; retry/backoff with a controlled clock; **read-only guard scanning all SQL constants** |
| Discovery | Query predicates and bind names; filter logic without a database |
| Extraction | Fake cursors — line order preserved, no truncation, validation rejects empty/garbage |
| Normalization | Golden files per type; **idempotency property test** `normalize(normalize(x)) == normalize(x)`; literal/comment-safety corpus (`CACHE`, `STORAGE` inside strings and comments must survive); logic-preservation regression corpus |
| File mapping | Every type → expected path; **invertibility property test**; illegal-name handling |
| Writer | Atomic write, directory creation, UTF-8/LF/trailing-newline, dry-run suppression |
| Reconciliation | Path-set differencing; threshold blocks mass deletion; suppressed when inventory query failed |
| Git | Real temp repo: only-changed-files staged, **no empty commit**, message format, rebase/retry, remote URL redaction |
| State | Round-trip; missing file ⇒ full export; corrupt file ⇒ fatal; **never written before push succeeds** |
| Logging | Sentinel password never appears in any record or traceback |
| End-to-end | Fake Oracle + temp repo, asserting every scenario in `docs/architecture.md` §22 |

**Blocking in CI:** lint, type check, the offline unit suite, the read-only guard test,
and the no-secrets-in-logs test.

---

## 7. Development workflow

### 7.1 Phase plan

| Phase | Deliverable | State |
|---|---|---|
| 0 | Specification (`README.md`, `docs/requirements.md`) | Done |
| 1 | Architecture (`docs/architecture.md`) | **Approved** |
| 2 | Skeleton: config, models, logging, state, test harness | Done |
| 3 | Oracle connection, discovery, extraction | Done |
| 4 | Normalization engine | Done |
| 5 | Repository mapping, writing, reconciliation | Done |
| 6 | Git commit/push, state persistence | Done |
| 7 | Docker, `.gitlab-ci.yml`, scheduling | Done |
| 8 | Hardening, runbook, traceability matrix | Done |

Never exercised against a live Oracle instance: pooling, batch fetching, and
session initialization are covered by fakes only. `@pytest.mark.oracle` is
wired and skipped by default, waiting for a disposable instance.

Work proceeds **one phase at a time**, in reviewable increments, never as one large drop.
Each phase ends by reporting what was built and waiting for the owner.

### 7.2 Per-change rules

- **Ask before implementing.** Propose, wait for approval, then build.
- **Do not modify unrelated files.** One change set, one purpose.
- **When requirements conflict, stop and ask.** Do not silently pick one.
- Surface risks early: privilege requirements, Oracle version differences, CI push loops,
  large-object behaviour, anything that could touch the source database.

### 7.3 Code conventions

- `main.py` orchestrates only — no SQL, no normalization, no Git, no I/O logic.
- Collaborators are injected via constructor arguments; tests substitute fakes without
  patching modules.
- Full type hints; `dataclasses` (frozen where possible) instead of dicts for domain data.
- `pathlib.Path` always; never string path concatenation.
- Timezone-aware UTC in state and logs. **Watermarks come from the database clock**, never
  the runner clock.
- Bind variables only — never interpolate configuration values into SQL.
- `ORDER BY` is mandatory wherever output order affects a produced file (always
  `ORDER BY LINE` for `DBA_SOURCE`).
- Project exception hierarchy; no bare `except:`; no silent swallowing.
- No `print()` — use the configured logger.
- No module beyond ~300 lines; no duplicated logic; readability over cleverness.
- Every public function documented: purpose, args, returns, raises.

### 7.4 Definition of done

- [ ] Read-only guarantee preserved
- [ ] No secret material in repo or logs
- [ ] Deterministic output; normalization idempotent
- [ ] Tests added/updated, passing offline
- [ ] Public functions documented
- [ ] `docs/requirements.md` / `docs/architecture.md` updated if behaviour changed
- [ ] No unrelated files touched

---

## 8. Rules about modifying the architecture

The architecture in `docs/architecture.md` is **approved and frozen**. It changes only by
explicit owner approval, never by implementation convenience.

### 8.1 Requires owner approval before any code is written

- Adding, removing, renaming, or relocating any module or package
- Changing layer boundaries or dependency direction
- Changing the two-gate design (`LAST_DDL_TIME` for extraction, `git status` for commit)
- Introducing a content-comparison step
- Changing the concurrency model
- Changing the repository file layout, path mapping, or file conventions
- Changing the `execution_state.json` schema or the rule that it is written only after a
  successful push
- Changing the commit message format or the Git phase sequence
- Adding, removing, or upgrading a third-party dependency
- Changing any approved decision D-1 … D-6
- Changing exit-code semantics, the error/status model, or the retry policy
- Anything that widens what the application may do to the Oracle database

### 8.2 Does not require approval

Implementation detail *within* an approved module: internal helper functions, local
refactoring that preserves the public interface, additional tests, docstrings, log message
wording, and type-hint precision.

### 8.3 Procedure when a change looks necessary

1. **Stop.** Do not implement it and do not work around it.
2. State the problem, the specific architecture element it affects, the options, and a
   recommendation.
3. Wait for approval.
4. On approval: update `docs/architecture.md` **first**, then this file if a rule changed,
   then implement.

Documentation and implementation must never diverge. If code and `docs/architecture.md`
disagree, the document is correct and the code is a defect.
