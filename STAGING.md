# Staging

`staging.goodtip.com.au` runs the `staging` branch. `goodtip.com.au` runs
`main`. Both live on the same box, from two separate checkouts, against two
separate databases, and neither can reach the other.

The point is that work in progress is visible to the client without being
live. A broken commit on `staging` breaks the site the client is looking at;
it cannot break the one their members are using.

```
  push to staging ──▶ staging.goodtip.com.au ──▶ client approves
                                                       │
                                                       ▼
                                        merge staging → main ──▶ goodtip.com.au
```

Both sides deploy the same way: a systemd timer runs `deploy.sh` every two
minutes, pulls its own branch, and reloads its own gunicorn. Pushing **is**
deploying, to whichever site owns that branch.

| | production | staging |
|---|---|---|
| branch | `main` | `staging` |
| checkout | `~/projects/goodtip` | `~/projects/goodtip-staging` |
| database | `goodtip_db` | `goodtip_staging_db` |
| gunicorn | `:8000` (`goodtipservice`) | `:8001` (`goodtip-staging`) |
| deploy timer | `goodtip-sync.timer` | `goodtip-staging-sync.timer` |
| test gate | `fast` (check only) | `warn` (full suite, reported) |
| email | Postmark, unrestricted | Postmark, `EMAIL_ALLOWLIST=*` (drops `.invalid` only) |
| analytics | on | off |
| scheduled jobs | on | on — its own `goodtip-staging-*` units |

## Daily use

```bash
git push origin HEAD:staging        # live on staging within ~2 minutes
```

Watch it land:

```bash
ssh goodtip 'journalctl -u goodtip-staging-sync.service -n 40 --no-pager'
```

Once the client signs off:

```bash
git checkout main
git merge --ff-only staging
git push origin main                # live on goodtip.com.au within ~2 minutes
```

`--ff-only` is deliberate. If it refuses, `main` has something `staging` does
not — a hotfix — and the answer is to merge main into staging, re-check
staging, and release from there. Never to force it.

## One-time setup

### 1. On the server

```bash
ssh goodtip
cd ~/projects/goodtip
sudo bash deploy/bootstrap-staging.sh
```

Idempotent, so re-running it is safe. It creates the checkout, virtualenv,
database, `.env`, both systemd units, the nginx vhost and the TLS certificate.
It prints a generated staging-gate password — that is the one to give the
client, and it is not recoverable afterwards except from
`~/projects/goodtip-staging/.env`.

Two things it deliberately leaves for you, both in that `.env`:

- **`DATABASE_URL`** is written with a `CHANGE_ME` password. Fill in
  `goodtip_user`'s password before anything else works.
- **`EMAIL_ALLOWLIST`** is empty, which means staging sends *no* email at all.
  Add your address and the client's (`you@example.com,@client.com.au`) when
  you want to preview invites. Anything not on that list is logged and
  dropped — see below for why that matters.

Then fill the database:

```bash
sudo bash ~/projects/goodtip-staging/deploy/refresh-staging-db.sh \
  --keep you@example.com,client@example.com
```

### 2. Block direct pushes to main

On each machine you work from:

```bash
ln -sf ../../deploy/git-hooks/pre-push .git/hooks/pre-push
```

This refuses any push to `main` that is not a fast-forward of `origin/staging`,
which is precisely the sanctioned release. `ALLOW_MAIN_PUSH=1` overrides it for
a genuine emergency.

Also add the server-side rule, which is the one that holds when the hook is not
installed — GitHub → **Settings → Branches → Add branch ruleset**, target
`main`, enable **Restrict deletions**, **Block force pushes** and **Require a
pull request before merging**. On a private repo this needs a paid plan; if it
is unavailable, the hook is the enforcement and it is worth installing on every
clone you use.

## Refreshing staging's data

Staging drifts as it gets used. There are two ways to reset it, and they are
separate scripts on purpose — you should have to name which one you want.

**Verbatim (the current default).** Every account keeps its real address, so
every role can be signed into and the whole system is testable:

```bash
sudo bash ~/projects/goodtip-staging/deploy/restore-staging-verbatim.sh
```

The staging database then holds real personal data, and with `EMAIL_ALLOWLIST=*`
the jobs timer can mail those people for real.

**Scrubbed.** For when staging should not hold real personal data:

```bash
sudo bash ~/projects/goodtip-staging/deploy/refresh-staging-db.sh \
  --keep you@example.com,client@example.com
```

It dumps production read-only, rebuilds `goodtip_staging_db` from that dump,
migrates it up to the staging branch's schema, and then rewrites every real
name, address, login record and Stripe identifier into fake equivalents
(`sysadmin/management/commands/scrub_for_staging.py`).

`--keep` is the list of accounts left able to sign in. Sign-in is a code
emailed to the address on the account, so an account whose address has been
scrubbed to `@staging.invalid` can never receive one. **Pass at least your
own**, or nobody can log in to staging at all.

If the scrub fails for any reason, the script **drops the staging database**
rather than leave a verbatim copy of production's personal data being served
behind one shared password. Staging is then down until it is re-run.

## Things that are load-bearing

**Staging's `.env` must never point at `goodtip_db`.** It is the one line that
matters. `scrub_for_staging` refuses to run if it sees that name, but the
scrub is not what protects the live database — a staging migration is.

**Staging CAN email real members, by choice.** `EMAIL_ALLOWLIST=*` and the
database is unscrubbed, so invites, sign-in codes and notifications reach real
addresses — which is what makes signup, invites and elections testable at all.
`AllowlistEmailBackend` still refuses `.invalid`: the scrub mints those and
they can only hard-bounce against the Postmark token live shares. Be deliberate
about anything that mails a whole organisation.

**Staging has its own `SECRET_KEY`.** Django signs session cookies with it.
Sharing production's would make a session minted on staging valid on
goodtip.com.au, and the staging gate would be all that stood between a demo
password and a signed-in production session.

**The two gunicorns are told apart by working directory.** Both serve
`goodtip.wsgi` as the same user, so the obvious `pkill -f gunicorn.*goodtip`
matches both. `deploy.sh` reads `/proc/<pid>/cwd` instead. If you ever move a
checkout, update the `WorkingDirectory` in its unit or its deploys will stop
reloading it.

**Staging has its own scheduled-job timers, not production's.**
Production's units hardcode production's checkout, so installing *those* from
staging would point production's jobs at staging's branch — `install-timers.sh`
still refuses to run from anywhere but the production directory. Staging's
equivalents are `goodtip-staging-{matchsync,backfill,jobs}`, deliberately offset
from production's schedule so both instances do not scrape the same pages on the
same tick.

## When something is wrong on staging

```bash
ssh goodtip
journalctl -u goodtip-staging-sync.service -n 60 --no-pager   # the deploy
journalctl -u goodtip-staging.service -n 60 --no-pager        # the site
systemctl status goodtip-staging.service
```

A commit that fails the gate is rolled back out of the tree and recorded in
`.git/deploy-blocked-sha`; staging keeps serving the last commit that passed
and will not retry until something new is pushed. The reason is in the sync
journal.

To deploy staging immediately instead of waiting for the timer:

```bash
sudo systemctl start goodtip-staging-sync.service
```
