# Manual Test Commands

Not a test log — a command reference. Each section below exercises one part
of the pipeline in isolation, so if something breaks in the future, you can
run just the relevant section to narrow down where the fault actually is,
rather than only ever running the whole thing end to end.

For "what does this specific error mean," see [`docs/runbook.md`](runbook.md)
instead — this file is "how do I re-test part X by hand."

All commands assume you're in the repo root:
```bash
cd "/c/Users/developer/OneDrive/Desktop/GIT/oracle_versioning"
```

---

## 1. Offline safety checks

No Oracle, no network. Confirms the read-only SQL guard and the log
redaction filter both still work.

```bash
./.venv/Scripts/python.exe -m pytest tests/test_connection.py tests/test_logging.py -v
```

**Pass:** all tests green. **If this fails,** the fault is in `src/oracle/sql.py`
(the guard) or `src/logging/logger.py` (redaction) — nothing else.

For the full offline suite instead of just these two files:
```bash
./.venv/Scripts/python.exe -m pytest -q
```

---

## 2. Oracle connect + extract only (writes nothing)

Proves the DB connection, discovery, and extraction work, without touching
disk or git at all.

```bash
export CLIENTDB_DSN="db-host.example.com:1521/DEVDB"
export CLIENTDB_USER="ddl_reader"
export CLIENTDB_PASSWORD="..."
./.venv/Scripts/python.exe -m src.main --dry-run --log-level DEBUG
```

**Pass:** log shows `discovered=N exported=N failed=0`, ends with
`execution finished: status=...`. **If this fails,** the fault is in
`src/oracle/connection.py`, `src/oracle/object_discovery.py`, or
`src/oracle/metadata.py` — extraction never got as far as writing anything.

---

## 3. Oracle extract with real file writes, no git

Same as #2, but actually writes `.sql` files to `repository/`. Still touches
no git command at all.

```bash
./.venv/Scripts/python.exe -m src.main --no-git --log-level DEBUG
ls -R repository/CLIENT_DEV/
```

**Pass:** files appear under `repository/CLIENT_DEV/<SCHEMA>/...`.
**If this fails but #2 passed,** the fault is in `src/normalizer/` or
`src/repository/writer.py`.

---

## 4. Git push over SSH — the known-working path

Standalone, no Oracle involved. Proves git identity, staging, commit, and
push all work, using this repo's real `origin` remote.

```bash
./.venv/Scripts/python.exe scratch_test_git_push.py
```

(Recreate this script from `docs/manual-test-commands.md` history or ask for
it again if it's been deleted — it's a throwaway, not part of the
application. It writes one harmless timestamped file, commits, and pushes.)

**Pass:** prints `pushed <sha> to gitlab/main`. **If this fails,** the fault
is in `src/git/git_client.py` or `src/git/push.py` — or it's a network/VPN
problem, not code (see #10 below and the DNS note in #5).

---

## 5. Git push over HTTPS + token — currently broken on this machine

**Known issue:** on this laptop, SSH resolves
`gitlab.example.com` fine, but git's HTTPS backend
fails with "could not resolve host" for the identical hostname — a DNS
asymmetry under this VPN's split-tunnel setup, not an application bug. Only
matters locally; a GitLab Runner won't have SSH available and must use this
path, so it needs to work *there* even though it doesn't here.

```bash
$env:GIT_PUSH_TOKEN = "glpat-..."
$env:GIT_TEST_REMOTE = "gitlab-token"
git remote add gitlab-token https://gitlab.example.com/developer/oracle_versioning.git
./.venv/Scripts/python.exe scratch_test_git_push.py
git remote remove gitlab-token
```

**Pass:** prints `pushed <sha> to gitlab-token/main`. **Last known result on
this machine:** fails with `Could not resolve host` — do not treat that as a
code regression here; re-test on the actual Runner instead.

---

## 6. Full local run — extract, commit, push, advance state

The real thing, no flags. Only run this when you're ready for it to actually
commit and push.

```bash
./.venv/Scripts/python.exe -m src.main --log-level DEBUG
```

**Pass:** ends with `status=SUCCESS ... pushed=True`, or — if content was
byte-identical to what's already committed — `status=NO_CHANGES` with
`no effective change; no commit, no push`. Both are correct outcomes.
**If it fails partway,** the log line just before the traceback tells you
which stage (extraction vs. git phase) to go re-test in isolation above.

Check `state/execution_state.json` before and after — it should only change
if a push actually succeeded.

---

## 7. Docker image build

```bash
docker build -t oracle-versioner:test .
```

**Pass:** ends with `naming to docker.io/library/oracle-versioner:test`, and
the build-time import check (`RUN python -c "import src.main"`) doesn't
error partway through.

---

## 8. Docker image run — dry-run inside the container (proven working)

```bash
docker run --rm oracle-versioner:test --version
docker run --rm oracle-versioner:test --dry-run
```

**Pass:** `--version` prints the version string; `--dry-run` with no
databases configured in the image's `config/databases.yml` reports
`status=NO_CHANGES`, `pushed=False`.

### Not yet tested — real extraction from inside the container

This is the next thing to actually try, not something already proven:

```bash
docker run --rm \
  -e CLIENTDB_DSN="db-host.example.com:1521/DEVDB" \
  -e CLIENTDB_USER="ddl_reader" \
  -e CLIENTDB_PASSWORD="..." \
  oracle-versioner:test --dry-run --log-level DEBUG
```

Requires the container to reach the Oracle VPN's network — on Docker Desktop
for Windows this usually works via the host's network stack for outbound
connections, but hasn't been confirmed here yet.

---

## 9. GitLab Runner status

Confirms a Runner exists and is online, without triggering any pipeline.

```bash
git ls-remote origin
```

If that responds, GitLab is reachable. To actually check Runner status, use
the web UI: **Settings → CI/CD → Runners** on the project. Look for a green
"online" indicator on the specific runner.

---

## 10. Known gotcha: `git.remote` vs. actual remote name

`config/settings.yml` currently has:
```yaml
git:
  remote: gitlab
```

But this clone's actual git remote is named `origin`, not `gitlab`:
```bash
git remote -v
# origin  git@gitlab.example.com:developer/oracle_versioning.git
```

**What this looks like when it bites:** a real run (#6) fails at the push
step with something like `remote 'gitlab' is not configured`, even though
extraction succeeded and a local commit was made — the exact same *shape* of
bug as the earlier `origin` (GitHub) vs `gitlab` (GitLab) mixup from earlier
in this project's history, just inverted.

**The fix**, if it starts happening:
```bash
# either rename the remote to match the config...
git remote rename origin gitlab

# ...or change the config to match the remote
```
Edit `config/settings.yml`: `remote: origin` instead of `remote: gitlab`.

This has **not** been fixed yet — the repo is currently being tested as-is,
with this inconsistency present but not yet triggered (all testing so far
has bypassed it, by calling git directly or via the `origin` name).
