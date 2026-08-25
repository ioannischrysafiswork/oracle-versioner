# Oracle DDL Versioning Agent

## Role

You are a senior Oracle DBA, Python software engineer, and Git automation expert.

Your job is to continuously synchronize Oracle database source code with a GitLab repository.

The repository must always contain the latest version of every Oracle object while avoiding unnecessary commits.

The system must work incrementally.

Never export the whole database if only a few objects changed.

Always prefer reliability over speed.

NEVER update / create / delete anything on the database
---

# Overall Workflow

Each execution performs the following steps:

1. Load execution metadata.
2. Connect to one or more Oracle databases.
3. Find changed objects.
4. Extract object source.
5. Normalize the source.
6. Compare with the Git repository.
7. Update changed files only.
8. Commit changes.
9. Push to GitLab.
10. Save execution metadata.

The process must stop immediately if a critical step fails.

Never update metadata unless the Git push succeeds.

---

# Database Connections

Support any number of Oracle databases.

Each database has:

- Name
- Connection string
- Username
- Password (never hardcoded)
- Git folder

Connections should be configured through a configuration file.

Connection failures must not stop processing of other databases.

Each database execution is independent.

---

# Step 1 — Load Metadata

Read a metadata file stored in GitLab.

Example:

execution_state.json

The metadata contains:

- last_successful_run
- last_commit_sha
- processed_objects
- execution_status

If the file does not exist:

Perform a full export.

After the first successful execution create the metadata file.



---

# Step 2 — Find Changed Objects

Query:

DBA_OBJECTS

Criteria:

LAST_DDL_TIME > last_successful_run

Supported object types:

PACKAGE

PACKAGE BODY

PROCEDURE

FUNCTION

TRIGGER

TYPE

TYPE BODY

VIEW

TABLE

SEQUENCE

SYNONYM

MATERIALIZED VIEW

Only VALID + INVALID objects should normally be exported.

Ignore Oracle internal schemas.

Ignore Oracle maintained objects.

Allow include/exclude schema filters.

Sort objects by:

OWNER

OBJECT_TYPE

OBJECT_NAME

---

# Step 3 — Extract Source

Extract source using DBA_SOURCE whenever possible.

Use DBMS_METADATA only when DBA_SOURCE is not applicable.

Recommended mapping:

PACKAGE -> DBA_SOURCE

PACKAGE BODY -> DBA_SOURCE

PROCEDURE -> DBA_SOURCE

FUNCTION -> DBA_SOURCE

TYPE -> DBA_SOURCE

TYPE BODY -> DBA_SOURCE

TRIGGER -> DBA_SOURCE

VIEW -> DBA_SOURCE

TABLE -> dba_tab_columns

SEQUENCE -> DBMS_METADATA

SYNONYM -> dba_synonyms

MATERIALIZED VIEW -> DBMS_METADATA

Never concatenate source without preserving line order.

Always order by LINE.

---

# Step 4 — Normalize Source

Normalization must remove differences that are not logical changes.

Examples:

Remove trailing spaces.

Normalize line endings.

Convert tabs to spaces.

Remove repeated blank lines.

Normalize CREATE OR REPLACE formatting.

Normalize quoted identifiers when possible.

Remove environment-specific storage clauses.

Remove:

STORAGE

TABLESPACE

PCTFREE

PCTUSED

INITRANS

MAXTRANS

SEGMENT CREATION

LOGGING

NOCOMPRESS

CACHE

NOPARALLEL

BUFFER_POOL

FLASH_CACHE

CELL_FLASH_CACHE

LOB storage clauses

Physical attributes

Normalize keyword casing.

Do not modify PL/SQL logic.

Do not change SQL semantics.

Normalization must be deterministic.

Running normalization twice must produce identical output.

---

# Step 5 — Compare

Each object maps to exactly one SQL file.

it is saved as an .SQL file.

Repository structure:

/database_name/
    /owner/
        /packages/
        /procedures/
        /functions/
        /views/
        /tables/
        /types/
        /triggers/
        /synonyms/
        /sequences/
        /materialized_views/

# Step 6 — Git Operations

Only modified files are staged.

Commit message format:

<database>||<sysdate>: Updated Oracle objects

Example:

HRDB: Updated Oracle objects || <sysdate>

Commit body:

Execution Time

Number of objects

Object list

Previous commit SHA

New execution timestamp

Never create empty commits.

---

# Step 7 — Push

Push to GitLab.

If push fails:

Abort.

Do not update metadata.

Return failure.

---

# Step 8 — Save Metadata

Only after a successful push.

Update:

last_successful_run

commit_sha

execution_status

processed_objects

execution_duration

database_name

Store as JSON.

Commit this metadata together with the source changes.

---

# Error Handling

Failures must never stop the entire application.

Each object should have its own status.

Possible statuses:

SUCCESS

FAILED_EXTRACTION

FAILED_NORMALIZATION

FAILED_COMPARISON

FAILED_GIT

FAILED_PUSH

FAILED_DATABASE

Every exception must be logged.

---

# Logging

Produce structured logs.

Each execution logs:

Database

Start time

End time

Duration

Objects discovered

Objects exported

Objects changed

Objects skipped

Objects failed

Commit SHA

Push status

Use INFO, WARNING and ERROR levels.

---

# Performance

Use Oracle connection pooling.

Fetch source in batches.

Avoid querying the same object twice.

Process multiple databases concurrently.

Do not process multiple objects from the same database in parallel unless it is safe.

---

# Security

Never hardcode credentials.

Support:

Environment variables

Vault

Secrets Manager

Encrypted configuration

Never log passwords.

Never log connection strings containing credentials.

---

# Idempotency

Running the application twice without Oracle changes must produce:

No modified files.

No Git commit.

No metadata update.

---

# Repository Layout

/config
    databases.yml

/src
    connection.py
    extractor.py
    normalizer.py
    comparer.py
    git_manager.py
    metadata.py
    logger.py
    main.py

/repository
    /database
        /schema
            /packages
            /procedures
            /functions
            /tables
            /views
            /types
            /triggers
            /synonyms
            /sequences
            /materialized_views

execution_state.json

README.md

---

# Code Quality

Use Python 3.13+

Follow PEP-8.

Use type hints.

Use dataclasses where appropriate.

Write modular code.

Write unit tests.

Document every public function.

Avoid duplicated code.

Favor readability over cleverness.

---

# Reliability Rules

Never overwrite a file unless the normalized content changed.

Never update metadata before Git push succeeds.

Never lose the previous execution state.

Always validate extracted source before writing.

Always validate Git status before committing.

Always fail safely.

Always generate deterministic output.

The repository must always be a faithful representation of the Oracle database source code.




## GitLab Deployment

The application is designed to run entirely from GitLab.

GitLab is responsible for:

- Scheduling executions.
- Running the Python application.
- Providing secrets.
- Storing the Git repository.
- Storing the execution metadata.
- Keeping execution logs.

The application must not require a permanently running server.

Every execution starts from a clean environment.

The workflow is:

1. GitLab Scheduled Pipeline starts.
2. Pipeline creates a Docker/Python environment.
3. Repository is checked out.
4. execution_state.json is loaded.
5. Oracle databases are processed.
6. SQL files are updated.
7. Git commit is created.
8. Commit is pushed.
9. execution_state.json is updated.
10. Pipeline finishes.

If no Oracle objects changed:

- No commit is created.
- execution_state.json is not modified.
- Pipeline exits successfully.

The application must therefore be completely stateless.

All persistent information must be stored inside the Git repository.

The pipeline must fail if:

- Oracle connection cannot be established.
- Source extraction fails unexpectedly.
- Git push fails.

The pipeline must succeed when:

- No Oracle objects changed.

Empty Git commits must never be created.

# GitLab Native Deployment

The Oracle Versioning application is designed to run entirely inside GitLab CI/CD.

GitLab is responsible for:

- Scheduling executions.
- Running the application.
- Providing secrets.
- Storing the repository.
- Keeping execution logs.
- Managing execution history.

The application must never require a permanently running server.

Each execution starts from a clean environment.

The application must therefore be completely stateless.

No data may be stored locally between executions.

The Git repository is the only persistent storage.

---

# Pipeline Workflow

Every scheduled execution performs the following steps:

1. GitLab Runner starts.
2. Repository is checked out.
3. Dependencies are installed.
4. execution_state.json is loaded.
5. Oracle databases are processed.
6. Changed SQL files are updated.
7. Files are committed.
8. Commit is pushed.
9. execution_state.json is updated.
10. Pipeline exits.

If no Oracle object changed:

- No SQL files are modified.
- No commit is created.
- execution_state.json is not updated.
- The pipeline exits successfully.

---

# GitLab CI/CD

Claude must generate:

- .gitlab-ci.yml
- Dockerfile
- requirements.txt
- README.md

The pipeline should support:

- Manual execution
- Scheduled execution
- Future support for merge request pipelines

---

# GitLab Variables

No credentials may be hardcoded.

Oracle credentials must come from GitLab CI/CD variables.

GitLab access tokens must also come from CI/CD variables.

The application must support any number of Oracle databases.

Each database may have independent credentials.

---

# Docker

The project must run inside Docker.

The Docker image should include:

- Python 3.13
- Oracle python driver (oracledb)
- Git
- Required Python libraries

The container starts the application, performs one synchronization, and exits.

---

# Failure Rules

The application must fail the pipeline when:

- Oracle connection fails.
- Source extraction fails.
- Git push fails.
- execution_state.json cannot be parsed.
- Configuration is invalid.

The application must NOT fail when:

- No Oracle objects changed.
- An object has already been synchronized.
- An object was recompiled but its normalized DDL is identical.

---

# State Management

execution_state.json represents the last successful synchronization.

It must only be updated when:

- At least one SQL file changed.
- The Git push succeeded.

It must never be updated for metadata-only executions.

This prevents unnecessary commits while ensuring the repository always reflects the latest exported DDL.

---

# Multi-Database Support

The application must process databases independently.

A failure in one database must not stop processing of the remaining databases.

Each database maintains its own execution state.

---

# Code Generation Requirements

Claude should generate a professional production-quality Python project.

The project should use:

- dataclasses
- type hints
- logging
- pathlib
- dependency injection where appropriate
- unit tests
- configuration files
- modular architecture

No business logic should exist inside main.py.

main.py should only orchestrate the workflow.


oracle-versioning/
│
├── .gitlab-ci.yml
├── Dockerfile
├── requirements.txt
├── README.md
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
│   │   ├── comparer.py
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
    ├── DB/
    │   └── schema name/
    │       ├── packages/
    │       ├── package_bodies/
    │       ├── procedures/
    │       ├── functions/
    │       ├── views/
    │       ├── tables/
    │       ├── types/
    │       ├── type_bodies/
    │       ├── triggers/
    │       ├── sequences/
    │       ├── synonyms/
    │       └── materialized_views/
    ├── 
		└──schema name/
            ├── packages/
            ├── procedures/
            └── ...