# GoodTip — working notes for Claude

Read this before touching anything. The two-environment setup here is easy to
break from a machine that cannot see the server.

## Pushing is deploying

There is no deploy button and no CI. A systemd timer on the server runs
`deploy.sh` every ~2 minutes, pulls its branch, and reloads that branch's
gunicorn.

| | live | staging |
|---|---|---|
| url | goodtip.com.au | staging.goodtip.com.au |
| branch | `main` | `staging` |
| checkout | `~/projects/goodtip` | `~/projects/goodtip-staging` |
| database | `goodtip_db` | `goodtip_staging_db` |

**`git push origin main` puts code in front of real members within two
minutes.** Treat it as a release, not a save. Default to `staging`.

```bash
git push origin HEAD:staging     # → staging.goodtip.com.au, ~2 min
# once it has been checked there:
git checkout main && git merge --ff-only staging && git push origin main
```

`--ff-only` is deliberate. If it refuses, `main` has something `staging` does
not; merge main into staging, re-check, and release from there. Never force.

## What staging is for

Staging is the whole system with a controlled-access gate in front of it — not
a reduced or sandboxed copy. The gate is on live too; it exists so the client
can show the product to chosen people, and so work in progress is never
visible mid-demo. It is not a restriction to design around.

As of 2026-08-25 staging deliberately has **no functional restrictions**:

- the database is a **verbatim, unscrubbed** copy of live, so every account can
  be signed into and every role tested
- `EMAIL_ALLOWLIST=*` — invites, sign-in codes and notifications go to any real
  address through Postmark
- its own sports-sync, backfill and job timers run, so fixtures, results,
  elections and reminders update on their own

Two consequences worth holding on to:

- staging holds **real member addresses** and its jobs timer can mail them for
  real. Be deliberate about anything that mails a whole organisation.
- billing is the one thing genuinely switched off — Stripe keys are blank by
  request. Checkout is untestable; that is not a bug to fix.

## Rules that are load-bearing

**`~/projects/goodtip` is production's live checkout, and it is also the
default working directory.** Never check another branch out in it. `deploy.sh`
pulls into **HEAD** (`git pull --rebase origin main`), not into `main`, so a
checkout left on `staging` means the next push to main rebases staging's
commits into production's tree and deploys them — migrations included. To put
work on staging from here: `git push origin HEAD:staging`, never a branch
switch. If one happens anyway, `git checkout main` before the next 2-minute
tick and confirm with `git status -sb`.

**Never point staging's `.env` at `goodtip_db`.** One wrong word there and a
staging migration alters the live database. It is the single most dangerous
line in either environment.

**Never install production's job timers from the staging checkout.** They
hardcode production's paths. Staging has its own `goodtip-staging-*` units.

**The two gunicorns are told apart by working directory**, not process name —
both serve `goodtip.wsgi` as the same user. `deploy.sh` reads `/proc/<pid>/cwd`.
If a checkout ever moves, update `WorkingDirectory` in its unit or its deploys
stop reloading it.

**Staging has its own `SECRET_KEY`.** Sharing production's would make a session
minted on staging valid on the live site.

**Credentials live in `.env` on the server, never in git.** That includes the
gate password. Do not commit them.

## Things that will look like bugs and are not

- ~~**~17 failures in `orgs.tests`.**~~ Fixed 2026-08-27: those setUps pinned a
  competition to season 2099 while the signup form offers only the current
  season. The suite is green — 576 tests, 0 failures, from the production
  checkout. The deploy gate is still `fast` because of them; it can now be
  moved to `block`, which is a deliberate choice and not made here.
- **`No module named 'sklearn'`** in the jobs log. A lazy import in
  `matchreader/training.py:72`; absent from *both* venvs and not in
  `requirements.txt`, so matchreader retraining is a no-op everywhere.
- **Running the test suite from the staging checkout** fails
  `test_production_has_no_robots_route`, because staging's `.env` sets
  `GOODTIP_ENV=staging`. Not a real failure.

## Shell gotchas on the server

- Each interactive `!` command accepts **one** sudo password. Chained `sudo`
  calls after the first fail silently. Put multi-step work in a script and run
  `sudo bash script.sh`.
- Long commands get wrapped by the terminal and split into broken lines. Prefer
  short commands or a script file.
