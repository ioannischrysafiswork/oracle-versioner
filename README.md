# Oracle DDL Versioning Agent

Continuously synchronize Oracle database source code with a GitLab repository.

The repository always contains the latest version of every Oracle object, while avoiding
unnecessary commits. The process is incremental: it never exports the whole database if
only a few objects changed.

**Workflow:** find objects changed since the last run → extract their source → normalize
it → write the files → commit → push. There is no comparison against the existing
repository content — a changed object's latest extracted and normalized source is always
written and pushed.

> **Status: phases 1-7 complete.** Configuration, logging, state, Oracle
> connection/discovery/extraction, normalization, repository mapping and
> reconciliation, the Git phase, Docker, and CI/CD are implemented and tested.
> Phase 8 (hardening, runbook, traceability matrix) remains.
>
> To deploy, see [`docs/deployment.md`](docs/deployment.md). For the design, see
> [`docs/architecture.md`](docs/architecture.md),
> [`docs/requirements.md`](docs/requirements.md) and [`CLAUDE.md`](CLAUDE.md).

```bash
python -m src.main --dry-run
```

---

## How it works

```
1.  Load execution metadata (execution_state.json, from GitLab)
2.  Connect to one or more Oracle databases
3.  Find changed objects        (DBA_OBJECTS, LAST_DDL_TIME > last_successful_run)
4.  Extract object source       (DBA_SOURCE, or DBMS_METADATA where not applicable)
5.  Normalize the source        (deterministic, logic-preserving)
6.  Write changed objects to their mapped .sql file in the repository
7.  Stage only the files touched by this execution
8.  Commit the changes          (never an empty commit)
9.  Push to GitLab               (abort and do not update metadata if push fails)
10. Save execution metadata     (only after a successful push)
```

If no Oracle objects changed since the last run, no files are written, no commit is
created, and `execution_state.json` is not modified. The pipeline still exits
successfully.

If `execution_state.json` does not exist, a full export is performed and the metadata
file is created after the first successful execution.

---

## Database connections

Any number of Oracle databases can be configured. Each has:

- Name
- Connection string
- Username
- Password (from environment variable / secret store — never hardcoded)
- Git folder

Each **schema** is processed in turn and carried all the way to its own pushed commit
before the next begins, and each keeps its own watermark in `execution_state.json`. A
failure stops the execution there: every schema already pushed keeps its advanced
watermark, and the next run resumes at the one that failed. Adding a schema to the
include filter is therefore self-correcting — it has no watermark, so it receives a full
export.

---

## Object discovery

Query: `DBA_OBJECTS`
Criteria: `LAST_DDL_TIME > last_successful_run`

Supported object types:

- PACKAGE
- PACKAGE BODY
- PROCEDURE
- FUNCTION
- TRIGGER
- TYPE
- TYPE BODY
- VIEW
- TABLE
- SEQUENCE
- SYNONYM
- MATERIALIZED VIEW

Only VALID and INVALID objects are exported. Oracle internal schemas and Oracle
maintained objects are ignored. Include/exclude schema filters are supported. Results are
sorted by `OWNER`, `OBJECT_TYPE`, `OBJECT_NAME`.

---

## Source extraction

`DBA_SOURCE` is used wherever applicable; `DBMS_METADATA` is used only when `DBA_SOURCE`
does not apply. Source is always ordered by `LINE` and never concatenated out of order.

| Object type | Source |
|---|---|
| PACKAGE | DBA_SOURCE |
| PACKAGE BODY | DBA_SOURCE |
| PROCEDURE | DBA_SOURCE |
| FUNCTION | DBA_SOURCE |
| TYPE | DBA_SOURCE |
| TYPE BODY | DBA_SOURCE |
| TRIGGER | DBA_SOURCE |
| VIEW | DBA_SOURCE |
| TABLE | DBA_TAB_COLUMNS |
| SEQUENCE | DBMS_METADATA |
| SYNONYM | DBA_SYNONYMS |
| MATERIALIZED VIEW | DBMS_METADATA |

---

## Normalization

Normalization removes differences that are not logical changes. It does not modify
PL/SQL logic or SQL semantics, and it is deterministic — running it twice produces
identical output.

Includes:

- Remove trailing spaces
- Normalize line endings
- Convert tabs to spaces
- Remove repeated blank lines
- Normalize `CREATE OR REPLACE` formatting
- Normalize quoted identifiers where possible
- Normalize keyword casing
- Remove environment-specific storage clauses:
  `STORAGE`, `TABLESPACE`, `PCTFREE`, `PCTUSED`, `INITRANS`, `MAXTRANS`,
  `SEGMENT CREATION`, `LOGGING`, `NOCOMPRESS`, `CACHE`, `NOPARALLEL`, `BUFFER_POOL`,
  `FLASH_CACHE`, `CELL_FLASH_CACHE`, LOB storage clauses, other physical attributes

---

## Repository layout

Each object maps to exactly one `.sql` file.

```
oracle-versioning/
│
├── .gitlab-ci.yml
├── Dockerfile
├── requirements.txt
├── README.md
├── CLAUDE.md
├── .gitignore
│
├── config/
│   ├── databases.yml
│   ├── logging.yml
│   └── settings.yml
│
├── state/
│   └── execution_state.json
│
├── src/
│   ├── main.py
│   │
│   ├── config/
│   │   ├── loader.py
│   │   └── models.py
│   │
│   ├── oracle/
│   │   ├── connection.py
│   │   ├── object_discovery.py
│   │   ├── ddl_extractor.py
│   │   ├── source_extractor.py
│   │   └── metadata.py
│   │
│   ├── normalizer/
│   │   ├── ddl_normalizer.py
│   │   ├── plsql_normalizer.py
│   │   └── storage_clause_remover.py
│   │
│   ├── repository/
│   │   ├── file_mapper.py
│   │   └── writer.py
│   │
│   ├── git/
│   │   ├── git_client.py
│   │   ├── commit.py
│   │   └── push.py
│   │
│   ├── state/
│   │   ├── execution_state.py
│   │   └── models.py
│   │
│   ├── logging/
│   │   └── logger.py
│   │
│   ├── utils/
│   │   ├── hashing.py
│   │   ├── filesystem.py
│   │   └── timers.py
│   │
│   └── models/
│       ├── oracle_object.py
│       ├── database.py
│       └── execution.py
│
├── tests/
│   ├── test_connection.py
│   ├── test_extractor.py
│   ├── test_normalizer.py
│   ├── test_repository.py
│   ├── test_git.py
│   └── test_state.py
│
├── logs/
│   └── (generated locally for debugging)
│
└── repository/
    └── <DATABASE>/
        └── <SCHEMA>/
            ├── packages/
            ├── package_bodies/
            ├── procedures/
            ├── functions/
            ├── views/
            ├── tables/
            ├── types/
            ├── type_bodies/
            ├── triggers/
            ├── sequences/
            ├── synonyms/
            └── materialized_views/
```

---

## Git operations

Only modified files are staged.

Commit message format:

```
<database>||<sysdate>: Updated Oracle objects
```

Commit body contains:

- Execution time
- Number of objects
- Object list
- Previous commit SHA
- New execution timestamp

Empty commits are never created.

---

## Metadata (`execution_state.json`)

Updated only after a successful push:

- `last_successful_run`
- `commit_sha`
- `execution_status`
- `processed_objects`
- `execution_duration`
- `database_name`

Never updated before a push succeeds. Never lost between executions.

---

## Error handling

Failures never stop the entire application. Each object has its own status:

- `SUCCESS`
- `FAILED_EXTRACTION`
- `FAILED_NORMALIZATION`
- `FAILED_COMPARISON`
- `FAILED_GIT`
- `FAILED_PUSH`
- `FAILED_DATABASE`

Every exception is logged.

---

## Logging

Structured logs per execution: database, start time, end time, duration, objects
discovered, objects exported, objects changed, objects skipped, objects failed, commit
SHA, push status. Levels: `INFO`, `WARNING`, `ERROR`.

---

## Security

- No hardcoded credentials.
- Support for environment variables, Vault, Secrets Manager, encrypted configuration.
- Passwords and connection strings containing credentials are never logged.

---

## GitLab deployment

The application runs entirely from GitLab. GitLab is responsible for scheduling
executions, running the application, providing secrets, storing the repository, and
keeping execution logs.

Pipeline:

1. GitLab Runner starts.
2. Repository is checked out.
3. Dependencies are installed.
4. `execution_state.json` is loaded.
5. Oracle databases are processed.
6. Changed SQL files are written.
7. Files are committed.
8. Commit is pushed.
9. `execution_state.json` is updated.
10. Pipeline exits.

If no Oracle object changed: no files are written, no commit is created,
`execution_state.json` is not updated, and the pipeline exits successfully.

The pipeline fails when: Oracle connection fails, source extraction fails unexpectedly,
Git push fails, `execution_state.json` cannot be parsed, or configuration is invalid.

The pipeline does not fail when: no Oracle objects changed, or an object was already
synchronized.

Supports manual execution, scheduled execution, and future merge request pipelines.

---

## Docker

Image includes Python 3.13, `python-oracledb`, Git, and required Python libraries. The
container starts the application, performs one synchronization, and exits.

---

## Constraints

- The Oracle source database is read-only from the application's perspective.
- Never `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `ALTER`, or `DROP` against the source
  Oracle database.
- Never hardcode credentials.
- Never commit secrets.
- Credentials come from environment variables or GitLab CI/CD variables.
- The application supports dry-run mode.
- The application is suitable for unattended execution.

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/deployment.md`](docs/deployment.md) | Deployment and scheduling: Oracle grants, CI/CD variables, image build, pipeline schedule, reading the outcome, running locally |
| [`docs/runbook.md`](docs/runbook.md) | Incident response: triage by exit code, every failure mode and its recovery |
| [`docs/traceability.md`](docs/traceability.md) | Requirement → module → test matrix, deviations, and known gaps |
| [`docs/requirements.md`](docs/requirements.md) | Full specification |
| [`docs/architecture.md`](docs/architecture.md) | Architecture design: layering, modules, Oracle layer, change/new/deleted object detection, Git integration, error handling, retry, dry-run, Docker, GitLab CI/CD, security, behavioural scenarios |
| [`CLAUDE.md`](CLAUDE.md) | Working agreement and golden rules |
