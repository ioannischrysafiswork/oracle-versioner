# Deployment and Scheduling

How to get the agent running in GitLab. Covers the image, the runner, the CI/CD
variables, and the pipeline schedule.

Governed by [`docs/architecture.md`](architecture.md) §18-§20 and
[`CLAUDE.md`](../CLAUDE.md).

---

## 1. Prerequisites

| Requirement | Why |
|---|---|
| A GitLab project with Container Registry enabled | Hosts the agent image |
| A runner that can reach **both** the Oracle listeners and GitLab | The sync job needs both; on-premise databases usually mean a self-hosted runner inside the network perimeter (§19) |
| An Oracle account per database, read-only by grant | §2 below |
| A project access token scoped `write_repository` | §3 below |

The runner **must** use the Docker executor. A shell executor leaves state
between executions and breaks the stateless requirement (§19).

---

## 2. Oracle account

The account is read-only *by privilege*, not by convention. Code-level guards
are defence in depth; this grant is the real guarantee (§21.2).

```sql
CREATE USER ddl_reader IDENTIFIED BY "<generated>";
GRANT CREATE SESSION TO ddl_reader;
GRANT SELECT_CATALOG_ROLE TO ddl_reader;
GRANT EXECUTE ON DBMS_METADATA TO ddl_reader;
```

Prefer targeted grants over `SELECT_CATALOG_ROLE` where policy requires it:

```sql
GRANT SELECT ON DBA_OBJECTS      TO ddl_reader;
GRANT SELECT ON DBA_SOURCE       TO ddl_reader;
GRANT SELECT ON DBA_VIEWS        TO ddl_reader;
GRANT SELECT ON DBA_TAB_COLUMNS  TO ddl_reader;
GRANT SELECT ON DBA_SYNONYMS     TO ddl_reader;
GRANT SELECT ON DBA_TRIGGERS     TO ddl_reader;
GRANT SELECT ON DBA_SEQUENCES    TO ddl_reader;
GRANT SELECT ON DBA_MVIEWS       TO ddl_reader;
```

Nothing more. No privilege that permits DML or DDL on application schemas.

> `DBA_VIEWS` is required because Oracle stores view text there, not in
> `DBA_SOURCE`. See the note in [`architecture.md`](architecture.md) §5.1.

---

## 3. CI/CD variables

**Settings → CI/CD → Variables.** Every one of these must be **Masked** and
**Protected**.

| Variable | Example | Notes |
|---|---|---|
| `<DB>_DSN` | `db-host:1521/HRPDB` | One per database, named in `config/databases.yml` |
| `<DB>_USER` | `ddl_reader` | |
| `<DB>_PASSWORD` | | Generated; at least 8 characters — shorter values are refused for masking |
| `GIT_PUSH_TOKEN` | `glpat-…` | Project access token, `write_repository` scope, **this project only** |

Not `CI_JOB_TOKEN`: it cannot push by default (§21.3).

`config/databases.yml` names these variables and never contains their values.
Committing a DSN, username, or password is a security incident.

> GitLab refuses to mask values under 8 characters, and so does the agent's own
> log redaction filter — a shorter secret matches inside ordinary words and
> destroys the log while protecting nothing.

---

## 4. Build the image

Run the `build-image` job manually, or from a change to `Dockerfile` or
`requirements.txt`. It pushes `:latest` and `:<short-sha>` to the project
registry.

The base image is pinned by digest so a rebuild in six months behaves
identically. To refresh it deliberately:

```bash
docker manifest inspect python:3.13-slim | head -1
```

Update the digest in **both** stages of the `Dockerfile`.

---

## 5. Configure the databases

Edit `config/databases.yml`:

```yaml
databases:
  - name: HRDB
    git_folder: HRDB
    dsn_env: HRDB_DSN
    username_env: HRDB_USER
    password_env: HRDB_PASSWORD
    enabled: true
    schemas:
      include: [HR, PAYROLL]
    object_types: [PACKAGE, "PACKAGE BODY", VIEW, TABLE]
```

An empty `include` list means every schema that is not excluded. Omitting
`object_types` means all twelve supported types.

Validate before scheduling anything:

```bash
python -m src.main --dry-run --log-level DEBUG
```

A dry run connects and extracts for real but writes no file, creates no commit,
and updates no state.

---

## 6. Pin the runner

The `sync` job needs the runner with database network access. Uncomment `tags:`
in `.gitlab-ci.yml` and set it to that runner's tag. Without it the job lands on
a shared runner that cannot reach Oracle, and every database fails to connect.

---

## 7. Create the schedule

**CI/CD → Schedules → New schedule.** The schedule is the only automatic
trigger.

| Field | Value |
|---|---|
| Interval | e.g. `0 2 * * *` (daily, 02:00) |
| Target branch | The versioning branch, matching `settings.yml` `git.branch` |
| Variables | `LOG_LEVEL=INFO` (optional) |
| Activated | Yes |

Pick a cadence from how quickly the repository must reflect the database, not
from how long a run takes. A run that finds nothing changed costs one query per
database and creates no commit.

### Why a push cannot trigger the pipeline

The agent pushes to the repository that triggers it, so recursion is guarded
twice, independently (§11.7):

1. `rules:` restricts `sync` to `schedule` and `web` pipeline sources.
2. Every commit message carries `[skip ci]`.

Either alone would be enough. Both are used because a pipeline loop is
expensive and embarrassing.

---

## 8. First run

The first execution has no `execution_state.json` entry for any database, so it
performs a **full export**: every in-scope object, extracted and committed. On a
large database this is the slowest run the agent will ever do. Every subsequent
run is incremental.

Run it manually first (**CI/CD → Pipelines → Run pipeline**) so you can watch it
rather than discovering the result from a schedule at 2am.

---

## 9. Reading the outcome

| Exit code | Meaning | Pipeline |
|---|---|---|
| `0` | Success, including "nothing changed" | Passes |
| `1` | A database failed, or commit/push failed | Fails |
| `2` | Configuration invalid, or state file unparseable | Fails |

A database-level failure produces a non-zero exit so the pipeline is visibly
red, while the healthy databases have still been synchronized and pushed.

Two log lines to look for:

```
reconciliation BLOCKED: N of M file(s) would be deleted, over the 10.0% threshold
```
Reconciliation wanted to delete more than the safety threshold and deleted
nothing (decision D-6). Investigate before overriding. A genuine mass drop is a
deliberate human action:

```bash
python -m src.main --allow-mass-delete
```

```
no effective change; no commit, no push, and state is left untouched
```
Objects were re-extracted — a recompile or a `GRANT` moved `LAST_DDL_TIME` —
but the normalized bytes were identical, so Git created nothing. This is the
two-gate design working as intended, not a fault.

---

## 10. Running locally

The same image runs against a git-ignored `.env`, so a developer reproduces CI
behaviour exactly (§18.4):

```bash
docker run --rm --env-file .env -v "$PWD:/work" -w /work \
  registry.example.com/group/oracle-versioner:latest --dry-run
```

Or without Docker:

```bash
pip install -r requirements-dev.txt
python -m src.main --dry-run
```

| Flag | Effect |
|---|---|
| `--dry-run` | Connects and extracts; writes nothing, commits nothing |
| `--no-git` | Writes files, skips commit and push |
| `--database NAME` | Restricts the run; repeatable |
| `--allow-mass-delete` | Permits reconciliation past the safety threshold |

---

## 11. Running the integration tests

The offline suite uses fakes. The integration suite (`-m oracle`, skipped by
default) runs against a real instance and covers what fakes cannot: that the
driver connects, that the session-initialization statements are accepted, that
the discovery query is valid SQL, and that `DBMS_METADATA` returns storage-free
DDL. Every test in it is read-only.

A disposable instance is enough:

```bash
docker run -d --name ora-test -e ORACLE_PASSWORD=Test123 -p 15210:1521 gvenzl/oracle-free:slim-faststart
```

Create a reader account with the §2 grants, then:

```bash
ORACLE_TEST_DSN=localhost:15210/FREEPDB1 ORACLE_TEST_USER=ddl_reader ORACLE_TEST_PASSWORD=... python -m pytest -m oracle -v
```

Run this before pointing the agent at a production database for the first
time. It has already caught two defects that the offline suite could not see:
a naive `SYSTIMESTAMP` that would have skewed the watermark, and an empty
object list in the published commit message.
