# Importing the GoodTip database

The file `goodtip_export.sql` is a **portable PostgreSQL dump**. It was created
with `--no-owner --no-privileges`, so it contains **no role/ownership statements
and no credentials** — it imports cleanly under whatever PostgreSQL user you run
it as.

## Quick import (3 steps)

```bash
# 1. Create an empty database (use any name you like)
createdb goodtip

# 2. Load the dump:
psql -d goodtip -f goodtip_export.sql

# 3. Done — verify it loaded:
psql -d goodtip -c "SELECT count(*) FROM catalog_charity;"
```

If your `psql` needs a host/user, add them, e.g.:
`psql -h localhost -U <your_pg_user> -d goodtip -f goodtip_export.sql`

## Notes
- The dump uses `DROP ... IF EXISTS`, so re-running it into an existing `goodtip`
  database is safe and idempotent.
- Objects are created and owned by **the user who runs the import** — no need for
  a `mbatha` role or any specific password to exist on the target machine.
- To run the app against it afterwards, point `DATABASE_URL` in `.env` at this
  database, e.g. `postgres://<user>:<pass>@localhost:5432/goodtip`.
