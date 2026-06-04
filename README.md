# Goodtip

Django sports tipping app.

## Quick start

**Prerequisites:** Python 3.12+ and PostgreSQL 14+ installed locally.

- macOS: `brew install python@3.12 postgresql@16 && brew services start postgresql@16`
- Linux (Debian/Ubuntu): `sudo apt install python3 python3-venv python3-pip postgresql postgresql-contrib`
- Windows: install [Python](https://www.python.org/downloads/) (tick "Add to PATH") and [PostgreSQL](https://www.postgresql.org/download/windows/) (add bin folder to PATH).

Then, in the project folder:

### Mac / Linux
```bash
chmod +x setup.sh
./setup.sh
```

### Windows
Double-click `setup.bat`, or in a terminal:
```cmd
setup.bat
```

When the script finishes it starts the dev server. Open **http://localhost:8000**.

> Prefer to do it by hand? Follow the step-by-step guide in [SETUP.md](SETUP.md).

## Sign in

| Field    | Value                              |
|----------|------------------------------------|
| URL      | http://localhost:8000/admin        |
| Email    | `admin@example.com`                |
| Password | `admin`                            |

> The app uses **email** as the login (no separate username). Change this password before exposing the app to anyone else.

## What the setup script does

1. Checks Python 3 and PostgreSQL are installed.
2. Creates a Postgres role `mbatha` (password `masterclass`) and database `goodtip` if they don't exist.
3. Creates a Python virtual environment in `./venv` and installs `requirements.txt`.
4. Copies `.env.example` to `.env` with a fresh random `SECRET_KEY`.
5. Runs `migrate` and seeds teams.
6. Creates the default `admin@example.com / admin` superuser (idempotent).
7. Starts `python manage.py runserver 8000`.

Safe to re-run — every step is idempotent.

## Day-to-day commands

After the first setup, you can start the server without the script:

```bash
# Mac / Linux
source venv/bin/activate
python manage.py runserver 8000

# Windows
venv\Scripts\activate
python manage.py runserver 8000
```

Common management commands:

| Action               | Command                                      |
|----------------------|----------------------------------------------|
| Make migrations      | `python manage.py makemigrations`            |
| Apply migrations     | `python manage.py migrate`                   |
| Open Django shell    | `python manage.py shell`                     |
| Create another admin | `python manage.py createsuperuser`           |
| Reset the database   | drop+recreate `goodtip` DB, then re-run setup |

## Environment variables

`setup` copies `.env.example` → `.env`. Edit `.env` to fill in:

- `THESPORTS_API_KEY` — sports data API key
- `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` — outbound SMTP for password resets

Restart the dev server after editing `.env`.

## Troubleshooting

- **`psql: error: connection refused`** — Postgres isn't running. Start it (`brew services start postgresql@16` on macOS, `sudo systemctl start postgresql` on Linux, Services panel on Windows).
- **`Port 8000 already in use`** — run on another port: `python manage.py runserver 8001`.
- **Permission denied creating role/DB on Linux** — the script uses `sudo -u postgres`; enter your sudo password when prompted.
- **Boss installed Postgres with a different superuser password** — set it inline before running the script: `PGPASSWORD=yourpw ./setup.sh`.
