# Oracle DDL Versioning Agent — Requirements Specification

**Status:** Specification only. No implementation exists yet.

---

## 1. Role and objective

Continuously synchronize Oracle database source code with a GitLab repository. The
repository must always contain the latest version of every Oracle object while avoiding
unnecessary commits. The process works incrementally — it never exports the whole
database if only a few objects changed. Reliability is preferred over speed.

**The application must never update, create, or delete anything on the source Oracle
database.**

---

## 2. Overall workflow

Each execution performs the following steps, in order:

1. Load execution metadata.
2. Connect to one or more Oracle databases.
3. Find changed objects.
4. Extract object source.
5. Normalize the source.
6. Write changed objects to the repository.
7. Stage changed files.
8. Commit changes.
9. Push to GitLab.
10. Save execution metadata.

The process stops immediately if a critical step fails. Metadata is never updated unless
the Git push succeeds.

**Note on scope:** the workflow extracts the latest changed objects, formats
(normalizes) them, and pushes them to Git. It does not compare the normalized source
against the existing file content in the repository — a changed object's current source
is extracted, normalized, written, and pushed on every run in which that object appears
as changed.

---

## 3. Database connections

Support any number of Oracle databases. Each database has:

| Field | Notes |
|---|---|
| Name | |
| Connection string | |
| Username | |
| Password | Never hardcoded — from environment variable / secret store |
| Git folder | Target path within the repository |

Requirements:

- Connections are configured through a configuration file (`config/databases.yml`).
- Connection failures must not stop processing of other databases.
- Each database execution is independent.

---

## 4. Step 1 — Load metadata

Read `execution_state.json`, stored in GitLab.

Contents:

- `last_successful_run`
- `last_commit_sha`
- `processed_objects`
- `execution_status`

Rules:

- If the file does not exist, perform a full export.
- After the first successful execution, create the metadata file.

---

## 5. Step 2 — Find changed objects

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

Rules:

- Only VALID and INVALID objects are exported.
- Ignore Oracle internal schemas.
- Ignore Oracle maintained objects.
- Support include/exclude schema filters.
- Sort objects by `OWNER`, `OBJECT_TYPE`, `OBJECT_NAME`.

---

## 6. Step 3 — Extract source

Extract source using `DBA_SOURCE` whenever possible. Use `DBMS_METADATA` only when
`DBA_SOURCE` is not applicable.

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

Rules:

- Never concatenate source without preserving line order.
- Always order by `LINE`.

---

## 7. Step 4 — Normalize source

Normalization must remove differences that are not logical changes.

Examples:

- Remove trailing spaces.
- Normalize line endings.
- Convert tabs to spaces.
- Remove repeated blank lines.
- Normalize `CREATE OR REPLACE` formatting.
- Normalize quoted identifiers when possible.
- Normalize keyword casing.

Remove environment-specific storage clauses:

- `STORAGE`
- `TABLESPACE`
- `PCTFREE`
- `PCTUSED`
- `INITRANS`
- `MAXTRANS`
- `SEGMENT CREATION`
- `LOGGING`
- `NOCOMPRESS`
- `CACHE`
- `NOPARALLEL`
- `BUFFER_POOL`
- `FLASH_CACHE`
- `CELL_FLASH_CACHE`
- LOB storage clauses
- Physical attributes

Rules:

- Do not modify PL/SQL logic.
- Do not change SQL semantics.
- Normalization must be deterministic.
- Running normalization twice must produce identical output.

---

## 8. Step 5 — Write changed objects to the repository

Each object maps to exactly one SQL file, saved as a `.SQL` file.

Repository structure:

```
/database_name/
    /owner/
        /packages/
        /package_bodies/
        /procedures/
        /functions/
        /views/
        /tables/
        /types/
        /type_bodies/
        /triggers/
        /synonyms/
        /sequences/
        /materialized_views/
```

Rules:

- Every object found as changed in Step 2 has its extracted, normalized source written
  to its mapped file, replacing the existing file if one is present.
- No comparison against existing file content is performed.
- Each written object is marked as processed for this execution.

---

## 9. Step 6 — Git operations

Only files written in Step 5 are staged.

Commit message format:

```
<database>||<sysdate>: Updated Oracle objects
```

Example:

```
HRDB: Updated Oracle objects || <sysdate>
```

Commit body contains:

- Execution time
- Number of objects
- Object list
- Previous commit SHA
- New execution timestamp

Rule: never create empty commits.

---

## 10. Step 7 — Push

Push to GitLab.

If push fails:

- Abort.
- Do not update metadata.
- Return failure.

---

## 11. Step 8 — Save metadata

Only after a successful push. Update:

- `last_successful_run`
- `commit_sha`
- `execution_status`
- `processed_objects`
- `execution_duration`
- `database_name`

Store as JSON. Commit this metadata together with the source changes.

---

## 12. Error handling

Failures must never stop the entire application. Each object has its own status:

- `SUCCESS`
- `FAILED_EXTRACTION`
- `FAILED_NORMALIZATION`
- `FAILED_COMPARISON`
- `FAILED_GIT`
- `FAILED_PUSH`
- `FAILED_DATABASE`

Every exception must be logged.

---

## 13. Logging

Produce structured logs. Each execution logs:

- Database
- Start time
- End time
- Duration
- Objects discovered
- Objects exported
- Objects changed
- Objects skipped
- Objects failed
- Commit SHA
- Push status

Levels: `INFO`, `WARNING`, `ERROR`.

---

## 14. Performance

- Use Oracle connection pooling.
- Fetch source in batches.
- Avoid querying the same object twice.
- Process multiple databases concurrently.
- Do not process multiple objects from the same database in parallel unless it is safe.

---

## 15. Security

- Never hardcode credentials.
- Support: environment variables, Vault, Secrets Manager, encrypted configuration.
- Never log passwords.
- Never log connection strings containing credentials.

---

## 16. Reliability rules

- Never update metadata before Git push succeeds.
- Never lose the previous execution state.
- Always validate extracted source before writing.
- Always validate Git status before committing.
- Always fail safely.
- Always generate deterministic output.
- The repository must always be a faithful representation of the Oracle database source
  code.

---

## 17. Repository layout

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

## 18. Code quality

- Use Python 3.13+.
- Follow PEP-8.
- Use type hints.
- Use dataclasses where appropriate.
- Write modular code.
- Write unit tests.
- Document every public function.
- Avoid duplicated code.
- Favor readability over cleverness.

---

## 19. GitLab deployment

GitLab is responsible for:

- Scheduling executions.
- Running the Python application.
- Providing secrets.
- Storing the Git repository.
- Storing the execution metadata.
- Keeping execution logs.

Rules:

- The application must not require a permanently running server.
- Every execution starts from a clean environment.
- The application must be completely stateless. All persistent information is stored
  inside the Git repository.

Pipeline workflow:

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

If no Oracle object changed:

- No SQL files are modified.
- No commit is created.
- `execution_state.json` is not updated.
- The pipeline exits successfully.

The pipeline must fail if:

- Oracle connection cannot be established.
- Source extraction fails unexpectedly.
- Git push fails.
- `execution_state.json` cannot be parsed.
- Configuration is invalid.

The pipeline must succeed when:

- No Oracle objects changed.
- An object has already been synchronized.

Deliverables: `.gitlab-ci.yml`, `Dockerfile`, `requirements.txt`, `README.md`. The
pipeline supports manual execution, scheduled execution, and future merge request
pipelines.

---

## 20. GitLab variables

- No credentials may be hardcoded.
- Oracle credentials must come from GitLab CI/CD variables.
- GitLab access tokens must also come from CI/CD variables.
- The application must support any number of Oracle databases.
- Each database may have independent credentials.

---

## 21. Docker

The project must run inside Docker. The Docker image includes:

- Python 3.13
- Oracle python driver (`oracledb`)
- Git
- Required Python libraries

The container starts the application, performs one synchronization, and exits.

---

## 22. State management

`execution_state.json` represents the last successful synchronization.

Rules:

- Updated only when the Git push succeeded.
- Never updated for a run in which no SQL files changed.
- Committed together with the source changes.

---

## 23. Multi-database support

- Databases are processed independently.
- A failure in one database must not stop processing of the remaining databases.
- Each database maintains its own execution state.

---

## 24. Additional constraints (from project brief)

- The Oracle source database must be read-only from the application's perspective.
- Never execute `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `ALTER`, or `DROP` against the
  source Oracle database.
- Never hard-code credentials.
- Never commit secrets.
- Credentials must come from environment variables or GitLab CI/CD variables.
- The application must support dry-run mode.
- The application must be suitable for unattended execution.
- Every important feature must have tests.
- Do not introduce dependencies without explaining why.
- Do not modify unrelated files.
- Do not make architectural changes without approval.
- No business logic should exist inside `main.py`. `main.py` should only orchestrate the
  workflow.
- Use dependency injection where appropriate.
