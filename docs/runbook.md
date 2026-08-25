# Runbook

What to do when the sync job goes red. Setup lives in
[`deployment.md`](deployment.md); this is incident response.

**The one thing to know first:** the agent never advances a watermark past work
that did not reach GitLab. A failed run loses nothing — the next execution
re-scans from the same point, re-extracts, and produces identical files. For
most failures the correct response is *fix the cause and let the schedule run
again*. Manual recovery is almost never required.

---

## Triage by exit code

| Exit | Meaning | Go to |
|---|---|---|
| `0` | Success, including "nothing changed" | Nothing to do — see [§7](#7-the-job-passed-but-nothing-was-committed) if you expected a commit |
| `1` | A database failed, or commit/push failed | [§1](#1-a-database-cannot-be-reached), [§2](#2-missing-grants), [§5](#5-push-failed) |
| `2` | Configuration invalid, or state file unparseable | [§3](#3-configuration-rejected), [§4](#4-state-file-unparseable) |

Search the job log for `ERROR` first. Every failure is logged with the database
name attached.

---

## 1. A database cannot be reached

```
ERROR database failed, other databases continue: HRDB: cannot connect: DPY-6005
```

**What already happened:** three connection attempts with exponential backoff.
Other databases in the same run were processed normally, committed, and pushed;
only this one is red.

**What was *not* touched:** this database's watermark. Its files are untouched.

**Check, in order:**

1. Is the listener up? `tnsping <dsn>` from the runner host.
2. Did the job land on the right runner? The `sync` job needs `tags:` set to a
   runner with database network access ([`deployment.md`](deployment.md) §6).
   Without it the job runs on a shared runner that cannot see Oracle at all.
3. Did the credentials change? See [§2](#2-missing-grants).

**Recovery:** none needed. Fix the cause; the next scheduled run picks up
exactly where this one stopped.

---

## 2. Missing grants

```
ERROR database failed: HRDB: cannot read ['dba_views']; check the account's grants
```

The pre-flight check runs before any extraction, so this fails fast and names
the views it could not read. Nothing was extracted or written.

**Fix:** re-apply the grants from [`deployment.md`](deployment.md) §2. The most
commonly missed one is `DBA_VIEWS` — Oracle stores view text there, not in
`DBA_SOURCE`.

---

## 3. Configuration rejected

```
ERROR execution aborted: invalid configuration or state
```

Exit code 2. Nothing was connected to, written, or committed.

The message names the exact key, e.g.
`databases.yml: databases[0].git_folder: must be a relative path inside the repository`.

Validate any change locally before pushing it:

```bash
python -m src.main --dry-run --config-dir config
```

**Common causes:** a secret reference naming a CI/CD variable that does not
exist; a schema listed in both `include` and `exclude`; an object type that is
not one of the twelve supported.

---

## 4. State file unparseable

```
ERROR state file state/execution_state.json is not valid JSON
```

Exit code 2. **The file is never silently recreated** — that would look like
"no history", trigger a full export, and mask whatever damaged it.

**Diagnose first.** A corrupt state file usually means a bad merge or a manual
edit. Check the history:

```bash
git log --oneline -- state/execution_state.json
git show HEAD~1:state/execution_state.json
```

**Recovery, in order of preference:**

1. **Revert to the last good version** — the watermarks stay correct and the
   next run is incremental.
   ```bash
   git checkout <good-sha> -- state/execution_state.json
   ```
2. **Delete it** only if no good version exists. The next run performs a **full
   export** of every database: correct, but slow, and it produces one very large
   commit.

Never hand-edit a watermark forward. A watermark ahead of reality means changes
between the real and the claimed point are **never** exported — silent data
loss. Editing one *backwards* is always safe; it only causes re-extraction.

---

## 5. Push failed

```
ERROR execution failed: push failed: ...
```

**State was not updated.** This is the design's most important invariant: the
local commit exists but is unpushed, and the watermark did not move.

| Symptom in the log | Cause | Action |
|---|---|---|
| `non-fast-forward`, `fetch first` | A concurrent pipeline pushed first | Already retried three times with fetch+rebase. If it still failed, check whether `resource_group` was removed from `.gitlab-ci.yml` — that is what serializes runs |
| `Authentication failed`, `401`, `403` | Token expired, revoked, or wrongly scoped | Reissue `GIT_PUSH_TOKEN` with `write_repository` on this project. Not retried, by design |
| `stale info` on the state push | Someone pushed between our commit and our amend | The lease did its job and refused to clobber them. Re-run |
| `protected branch` | The bot cannot push to the target branch | Allow the token's user to push, or point `git.branch` at an unprotected branch |

**Recovery:** none needed. The runner workspace is discarded, so the orphaned
local commit disappears. The next run recomputes the same changes and commits
them again. No duplicate history, no lost changes.

---

## 6. Reconciliation blocked

```
ERROR reconciliation BLOCKED: 412 of 900 file(s) would be deleted, over the 10.0% threshold.
      Nothing is deleted.
```

The agent found far more orphaned files than a normal run produces and refused
to delete any of them (decision D-6). **Extraction results were still committed** —
only the deletions were withheld. The exit code is unaffected.

**This is a safety net, not a bug. Find out why before overriding it.**

Legitimate causes:

- A schema was genuinely dropped or renamed.
- A schema was removed from the `include` filter — in which case those files are
  *intentionally* out of scope now, and deleting them would discard their
  history. Leave them.
- `git_folder` was changed, so the old subtree no longer matches any object.

Suspicious causes — investigate, do not override:

- The Oracle account lost a grant, so the inventory query returned far fewer
  objects than exist.
- A connection was flaky and the inventory came back partial.

Once you are certain the deletions are correct:

```bash
python -m src.main --allow-mass-delete --database HRDB
```

Run it from a manual (`web`) pipeline so the output is captured, and scope it to
the one database with `--database`.

---

## 7. The job passed but nothing was committed

```
INFO no effective change; no commit, no push, and state is left untouched
```

**Working as designed.** Objects *were* re-extracted — `LAST_DDL_TIME` advances
on recompiles, `GRANT`s, and dependency revalidation, not only on real edits —
but the normalized bytes were identical to what is already committed, so Git
produced no diff.

This is the two-gate design: `LAST_DDL_TIME` decides what is *extracted*
(deliberately generous), and `git status` decides what is *committed*
(precise). A quiet run is the normal outcome for a stable database.

To confirm the agent really is seeing your object:

```bash
python -m src.main --dry-run --database HRDB --log-level DEBUG
```

If the object is not in the discovered list, check the schema and object-type
filters, and whether the object's `LAST_DDL_TIME` really moved.

---

## 8. Individual objects failed

```
WARNING HR.EMPLOYEE_PKG (PACKAGE BODY) failed: ... insufficient privileges
```

Object-scoped: the run continued, other objects were exported, and **the failed
object's existing file was left untouched**. A failed extraction never
overwrites a good file with empty or partial content.

| Status | Meaning |
|---|---|
| `FAILED_EXTRACTION` | Could not read the source — privileges, wrapped source, or malformed output |
| `FAILED_NORMALIZATION` | The normalizer rejected the source. Please report this one; it should not happen |
| `FAILED_COMPARISON` | Could not map or write the file — usually an unmappable object name |
| `SKIPPED` | The object was dropped between discovery and extraction. Not a failure; reconciliation removes its file |

**Unmappable names** (`FAILED_COMPARISON`) are deliberate. An object whose name
contains `/ \ : * ? " < > |`, a control character, a trailing dot or space, or
which is a Windows device name (`CON`, `NUL`, `COM1`…) is failed loudly rather
than silently encoded — encoding would break the path↔object mapping that
deletion detection depends on. Renaming the object in Oracle is the only fix.

Objects retry automatically: because the watermark only advances on a
successful push, and the failed object's `LAST_DDL_TIME` has not changed, the
next run rediscovers it.

---

## 9. A run is taking too long

```
ERROR database failed: HRDB: wall-clock timeout of 3600s exceeded after 412 of 5000 object(s)
```

The run unit's own budget tripped before the CI job timeout, so the job failed
cleanly instead of being killed mid-write.

- **Expected on a first run.** The first execution has no watermark and performs
  a full export. Raise `retry.run_unit_timeout_seconds` and the job `timeout`
  for that run.
- **Unexpected later.** Check whether something is re-extracting the whole
  database every run — usually a watermark that is not advancing because the
  push keeps failing ([§5](#5-push-failed)).

---

## 10. Emergency stop

Deactivate the schedule: **CI/CD → Schedules → toggle off.** The agent holds no
state outside the repository and no server, so nothing else needs stopping. An
in-flight run finishes or fails on its own; either way the invariants hold.

To pause one database without touching the others, set `enabled: false` on it
in `config/databases.yml`.

---

## Invariants worth trusting under pressure

These hold for every failure mode above, and each is enforced by a test:

1. The Oracle database is only ever read.
2. A watermark never advances past changes that did not reach GitLab.
3. A failed extraction never overwrites a good file.
4. Identical content produces no commit.
5. A failure in one database never stops another.
6. Deletion beyond the safety threshold requires a human.
