# Manual Test Commands

Not a test log — a command reference. Each section below exercises one part
of the pipeline in isolation, so if something breaks in the future, you can
run just the relevant section to narrow down where the fault actually is,
rather than only ever running the whole thing end to end.

For "what does this specific error mean," see [`docs/runbook.md`](runbook.md)
instead — this file is "how do I re-test part X by hand."

Every section is self-contained and starts with its own `cd` — copy-paste
each block on its own, in a fresh PowerShell window, without needing to
remember where a previous section left you.

Commands are PowerShell (this machine's actual shell). Where a command was
run in Git Bash instead, that's called out explicitly.

---

## 1. Offline safety checks

No Oracle, no network. Confirms the read-only SQL guard and the log
redaction filter both still work.

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
.\.venv\Scripts\python.exe -m pytest tests/test_connection.py tests/test_logging.py -v
```

**Pass:** all tests green. **If this fails,** the fault is in `src/oracle/sql.py`
(the guard) or `src/logging/logger.py` (redaction) — nothing else.

For the full offline suite instead of just these two files:
```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
.\.venv\Scripts\python.exe -m pytest -q
```

---

## 2. Oracle connect + extract only (writes nothing)

Proves the DB connection, discovery, and extraction work, without touching
disk or git at all.

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
$env:CLIENTDB_DSN = "db-host.example.com:1521/DEVDB"
$env:CLIENTDB_USER = "APP_SCHEMA_1"
$env:CLIENTDB_PASSWORD = "..."
.\.venv\Scripts\python.exe -m src.main --dry-run --log-level DEBUG
```

**Pass:** log shows `discovered=N exported=N failed=0`, ends with
`execution finished: status=...`. **If this fails,** the fault is in
`src/oracle/connection.py`, `src/oracle/object_discovery.py`, or
`src/oracle/metadata.py` — extraction never got as far as writing anything.

---

## 3. Oracle extract with real file writes, no git

Same as #2, but actually writes `.sql` files to `repository/`. Still touches
no git command at all.

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
$env:CLIENTDB_DSN = "db-host.example.com:1521/DEVDB"
$env:CLIENTDB_USER = "APP_SCHEMA_1"
$env:CLIENTDB_PASSWORD = "..."
.\.venv\Scripts\python.exe -m src.main --no-git --log-level DEBUG
dir -Recurse repository\CLIENT_DEV\
```

**Pass:** files appear under `repository/CLIENT_DEV/<SCHEMA>/...`.
**If this fails but #2 passed,** the fault is in `src/normalizer/` or
`src/repository/writer.py`.

---

## 4. Git push over SSH — the known-working path

Standalone, no Oracle involved. Proves git identity, staging, commit, and
push all work, using this repo's real `origin` remote.

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
.\.venv\Scripts\python.exe scratch_test_git_push.py
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

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
$env:GIT_PUSH_TOKEN = "glpat-..."
$env:GIT_TEST_REMOTE = "gitlab-token"
git remote add gitlab-token https://gitlab.example.com/developer/oracle_versioning.git
.\.venv\Scripts\python.exe scratch_test_git_push.py
git remote remove gitlab-token
```

**Pass:** prints `pushed <sha> to gitlab-token/main`. **Last known result on
this machine:** fails with `Could not resolve host` — do not treat that as a
code regression here; re-test on the actual Runner instead.

---

## 6. Full local run — extract, commit, push, advance state

The real thing, no flags. Only run this when you're ready for it to actually
commit and push.

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
$env:CLIENTDB_DSN = "db-host.example.com:1521/DEVDB"
$env:CLIENTDB_USER = "APP_SCHEMA_1"
$env:CLIENTDB_PASSWORD = "..."
.\.venv\Scripts\python.exe -m src.main --log-level DEBUG
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

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
docker build -t oracle-versioner:test .
```

**Pass:** ends with `naming to docker.io/library/oracle-versioner:test`, and
the build-time import check (`RUN python -c "import src.main"`) doesn't
error partway through.

**Important:** `Dockerfile` does `COPY config/ ./config/` — the image bakes
in whatever `config/databases.yml` looked like *at build time*. It is not
read from disk at `docker run` time. If you edit `config/databases.yml`
(enable a database, change a schema filter, etc.), you must **rebuild the
image** before that change takes effect inside a container. Running an old
image against a new config file silently uses the old config — it looks
like the app found nothing to do (`0 database(s)`), not like an error.

---

## 8. Docker image run

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
docker run --rm oracle-versioner:test --version
docker run --rm oracle-versioner:test --dry-run
```

**Pass:** `--version` prints the version string; `--dry-run` with no
databases configured in the image's `config/databases.yml` reports
`status=NO_CHANGES`, `pushed=False`.

### Real extraction from inside the container — confirmed working 2026-08-28

Requires the container to reach the Oracle VPN's network, and requires the
image to have been rebuilt (#7) after `config/databases.yml` last changed —
see the note above.

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
docker run --rm `
  -e CLIENTDB_DSN="db-host.example.com:1521/DEVDB" `
  -e CLIENTDB_USER="APP_SCHEMA_1" `
  -e CLIENTDB_PASSWORD="..." `
  oracle-versioner:test --dry-run --log-level DEBUG
```

**Confirmed result:** Docker Desktop's container networking on this machine
*does* reach the VPN-routed Oracle host with no extra configuration —
`discovered=3407 exported=3407 skipped=0 failed=0`, `status=SUCCESS`, ~2m45s
for a full first export of the `APP_SCHEMA_1` schema. This was the last
previously-unproven link in the container path; it's now proven end to end.

---

## 9. Full Docker run with git enabled — local findings, not yet working

Same as #8's real extraction, but without `--no-git` and without `--dry-run`
— the container itself commits and pushes. This needs the container to
operate on the real repo (`.git` included, not just `repository/`), and
needs push credentials. **Local testing hit two dead ends here; both are
documented as findings, not fixed.**

Setup used for both attempts — a temporary `gitlab` remote alias, since
`config/settings.yml` says `git.remote: gitlab` but this clone's real remote
is named `origin` (see #11 below — left unfixed on purpose):

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
git remote add gitlab <same URL as origin, ssh:// or https://>
# ...and later, to clean up:
git remote remove gitlab
```

**Attempt 1 — SSH.** Mounted the whole repo plus `~/.ssh` into the
container, fixed key permissions and `git config --global --add
safe.directory /app` (bind-mounted repos are owned by a different UID than
the container's non-root `agent` user, which Git refuses by default — that
part's a real, generally-applicable fix). **Failed:** `cannot run ssh: No
such file or directory` — the runtime image only installs `git` and
`ca-certificates`, not `openssh-client`. **Not a bug to fix**: production
pushes over HTTPS + `GIT_PUSH_TOKEN`, never SSH; SSH was only ever the local
host-level fallback (#4). Skip SSH for this kind of test — go straight to
HTTPS.

**Attempt 2 — HTTPS + token.** Same setup, `gitlab` remote pointed at the
`https://` URL instead, token passed via `-e GIT_PUSH_TOKEN`. **DNS
resolved fine from inside the container** — a genuinely useful result: the
DNS asymmetry in #5 (SSH resolves the GitLab host from the Windows host,
HTTPS doesn't) does **not** reproduce inside the container's network stack.
**Failed differently:** `server verification failed: certificate signer not
trusted` — the container's minimal `ca-certificates` bundle doesn't include
this organization's internal root CA, which the Windows host trusts via its
OS certificate store.

**Status: open locally; worked around in CI.** Two ways to fix it:

*In CI (done).* `.gitlab-ci.yml`'s `sync` job now runs

```yaml
before_script:
  - git config --global http.sslCAInfo "$ORG_ROOT_CA"
```

with `ORG_ROOT_CA` as a **file-type** CI/CD variable holding the certificate.
Two reasons it takes that shape rather than an env var or
`update-ca-certificates`:

- `src/git/git_client.py`'s `run()` hands each `git` subprocess a minimal
  environment — `GIT_TERMINAL_PROMPT`, `PATH`, `HOME` only — so
  `GIT_SSL_CAINFO` would be stripped before `git` saw it (see the note below).
  `HOME` *is* forwarded, so `$HOME/.gitconfig` does reach the application's
  pushes, as well as the job script's own `git pull`.
- The image runs as the non-root `agent` user, so `update-ca-certificates`
  is not available at job time anyway.

*In the image (not done).* The cleaner long-term fix is still baking the CA in
(`Dockerfile`: copy the `.crt` into `/usr/local/share/ca-certificates/`, then
`update-ca-certificates` while still root). That is what a **local** `docker
run` push would need — the CI workaround above only helps inside a GitLab job,
where the file variable exists. Left for a deliberate later step.

**When triggering a real pipeline (#10), watch for this exact certificate
error anyway.** If it still appears *after* the `before_script` above, the
failure is in the Runner's own **helper** image, which performs the clone
before our image is ever started — a runner configuration matter, not this
image.

### Confirming it's *only* the certificate — nothing else broken

Important: `GIT_PUSH_TOKEN` and a `GIT_SSL_NO_VERIFY` env var **cannot** be
used to sanity-check this by just re-running the app (`python -m src.main`)
with those set — `src/git/git_client.py`'s `run()` method deliberately
builds a minimal, hardcoded environment for every `git` subprocess call
(`GIT_TERMINAL_PROMPT`, `PATH`, `HOME` only), so any other env var — the
token env var name aside, which is read separately and passed explicitly —
is silently stripped before `git` ever sees it. This is intentional
(CLAUDE.md §5.4: credentials must never leak into a subprocess by accident),
but it means you cannot verify this diagnosis by tweaking env vars and
re-running the app — you have to bypass the app and call `git` directly.

To confirm the certificate really is the *only* problem (network, DNS,
token, and token permissions are all fine), run a bare `git ls-remote`
directly inside the container, with certificate verification explicitly
disabled via a `git` command-line flag (not an env var, since here we're
calling `git` ourselves, not through the app):

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
docker run --rm `
  --entrypoint sh `
  -v "${PWD}:/app" `
  oracle-versioner:test -c "git config --global --add safe.directory /app && cd /app && git -c http.sslVerify=false ls-remote https://oauth2:<your-token>@gitlab.example.com/developer/oracle_versioning.git refs/heads/main"
```

Replace `<your-token>` with a real project access token (`glpat-...`). This
embeds the token directly on the command line, so it lands in your
PowerShell history — acceptable for a one-off diagnostic, but run
`Clear-History` afterward if that bothers you. This is a read-only command
(`ls-remote`), so it cannot push, commit, or modify anything either locally
or on GitLab.

**Confirmed result (2026-08-28):** printed `d9936ea2528003ed785b0083bd21b0343548bda9  refs/heads/main`
— matching the actual latest commit SHA on GitLab's `main` branch exactly.
This proves DNS, network reachability, token authentication, and token
permissions all work correctly from inside the container; certificate trust
is the *only* thing standing between this container and a working push.
Fixing the CA (see above) is therefore expected to be sufficient on its
own — no other layered problem to find once that's done.

---

## 10. GitLab Runner status

Confirms a Runner exists and is online, without triggering any pipeline.

```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
git ls-remote origin
```

If that responds, GitLab is reachable. To actually check Runner status, use
the web UI: **Settings → CI/CD → Runners** on the project. Look for a green
"online" indicator on the specific runner.

---

## 11. Known gotcha: `git.remote` vs. actual remote name

`config/settings.yml` used to have:
```yaml
git:
  remote: gitlab
```

But no checkout's git remote is named `gitlab` — this clone's is `origin`:
```powershell
cd "C:\Users\developer\OneDrive\Desktop\GIT\oracle_versioning"
git remote -v
# origin  git@gitlab.example.com:developer/oracle_versioning.git
```

**What this looks like when it bites:** a real run (#6) fails at the push
step with something like `remote 'gitlab' is not configured`, even though
extraction succeeded and a local commit was made — the exact same *shape* of
bug as the earlier `origin` (GitHub) vs `gitlab` (GitLab) mixup from earlier
in this project's history, just inverted.

**The fix**, if it starts happening:
```powershell
# either rename the remote to match the config...
git remote rename origin gitlab

# ...or change the config to match the remote
```
Edit `config/settings.yml`: `remote: origin` instead of `remote: gitlab`.

**Fixed (2026-08-31).** `config/settings.yml` now reads `remote: origin`,
which matches both this clone and any CI checkout — a GitLab job's working
copy only ever has `origin`, so the old value would have failed every
pipeline push after extraction had already succeeded. The `git.remote` value
and the `git pull --ff-only origin` in `.gitlab-ci.yml`'s `sync` script now
name the same remote.

Kept here because the failure *shape* is worth recognising: extraction
succeeds, a local commit exists, and only the push dies — so the fault looks
like a network or credential problem when it is neither.
