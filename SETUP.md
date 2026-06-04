# Manual Setup Guide

Step-by-step instructions for setting up Goodtip without running `setup.sh` / `setup.bat`. If you'd rather have it automated, run the script — see [README.md](README.md). Otherwise, follow this guide.

Everything below assumes you've already cloned the repo and have a terminal open in the project folder:

```bash
cd goodtip
```

---

## 1. Install prerequisites

You need **Python 3.12+** and **PostgreSQL 14+** on your machine.

### macOS

```bash
brew install python@3.12 postgresql@16
brew services start postgresql@16
```

### Linux (Debian / Ubuntu)

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip postgresql postgresql-contrib
sudo systemctl start postgresql
```

### Windows

1. Install Python from https://www.python.org/downloads/ — tick **Add Python to PATH** during the installer.
2. Install PostgreSQL from https://www.postgresql.org/download/windows/ — remember the **superuser password** you set during install, and tick the option to add the bin folder to PATH.
3. Open a new PowerShell or Command Prompt window so the PATH changes take effect.

Verify both are installed:

```bash
python3 --version    # or "python --version" on Windows
psql --version
```

---

## 2. Create the Postgres role and database

The app expects:

- role: `mbatha`
- password: `masterclass`
- database: `goodtip`

(You can pick different values, but then you must update `DATABASE_URL` in `.env` to match.)

### macOS

Postgres on Homebrew runs as your local user, so no `sudo` needed:

```bash
psql -d postgres -c "CREATE ROLE mbatha WITH LOGIN PASSWORD 'masterclass' CREATEDB;"
psql -d postgres -c "CREATE DATABASE goodtip OWNER mbatha;"
```

### Linux

The `postgres` system user is the only one that can create roles by default:

```bash
sudo -u postgres psql -c "CREATE ROLE mbatha WITH LOGIN PASSWORD 'masterclass' CREATEDB;"
sudo -u postgres psql -c "CREATE DATABASE goodtip OWNER mbatha;"
```

### Windows (PowerShell or CMD)

Use the `postgres` superuser. You'll be prompted for the superuser password you set during install:

```cmd
psql -U postgres -c "CREATE ROLE mbatha WITH LOGIN PASSWORD 'masterclass' CREATEDB;"
psql -U postgres -c "CREATE DATABASE goodtip OWNER mbatha;"
```

Confirm you can connect as the new role:

```bash
# Mac / Linux
PGPASSWORD=masterclass psql -h localhost -U mbatha -d goodtip -c "SELECT current_user;"
```

```cmd
REM Windows
set PGPASSWORD=masterclass
psql -h localhost -U mbatha -d goodtip -c "SELECT current_user;"
```

You should see `mbatha`.

---

## 3. Create a Python virtual environment

This isolates the project's dependencies from your system Python.

### Mac / Linux

```bash
python3 -m venv venv
source venv/bin/activate
```

### Windows

```cmd
python -m venv venv
venv\Scripts\activate
```

Your shell prompt should now show `(venv)`.

---

## 4. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 5. Create the `.env` file

Copy the template:

### Mac / Linux

```bash
cp .env.example .env
```

### Windows

```cmd
copy .env.example .env
```

Open `.env` in your editor. The only field you **must** change is `SECRET_KEY`. Generate a fresh value:

### Mac / Linux

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

### Windows (PowerShell)

```powershell
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

Paste the output into `.env` so the line looks like:

```
SECRET_KEY=YourRandomStringHere
```

Other fields in `.env` are optional for local dev:

| Variable                                  | Needed for                                   |
|-------------------------------------------|----------------------------------------------|
| `THESPORTS_API_KEY`                       | live sports data sync                        |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | sending password-reset emails                |

You can leave them blank for now — the app will still start.

---

## 6. Run the database migrations

```bash
python manage.py migrate
```

You should see a list of `Applying ...` lines, ending with `OK`.

---

## 7. Seed the teams

```bash
python manage.py seed_teams
```

Expected output: `Seed complete. New: <n>. Totals → AFL=18 AFLW=18 NRL=17 NRLW=12`.

---

## 8. Create a superuser

The app uses **email** as the login (no separate username). You can either let the interactive command prompt you, or run a one-liner.

### Interactive

```bash
python manage.py createsuperuser
```

You'll be asked for an email, display name, and password.

### One-liner

```bash
python manage.py shell -c "from django.contrib.auth import get_user_model; U=get_user_model(); U.objects.create_superuser(email='admin@example.com', password='admin', display_name='Admin')"
```

---

## 9. Start the dev server

```bash
python manage.py runserver 8000
```

Open **http://localhost:8000** in your browser.

To stop the server: press `Ctrl+C`.

---

## Sign in

| Field    | Value                       |
|----------|-----------------------------|
| URL      | http://localhost:8000/admin |
| Email    | `admin@example.com`         |
| Password | `admin`                     |

(Or whatever credentials you used in step 8.)

---

## Day-to-day

Once setup is done, starting up again only needs:

### Mac / Linux

```bash
source venv/bin/activate
python manage.py runserver 8000
```

### Windows

```cmd
venv\Scripts\activate
python manage.py runserver 8000
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `psql: error: connection refused` | Postgres isn't running. Start it (`brew services start postgresql@16`, `sudo systemctl start postgresql`, or via the Services panel on Windows). |
| `FATAL: password authentication failed for user "mbatha"` | The role exists with a different password. Drop it (`DROP ROLE mbatha;` as the postgres superuser) and redo step 2. |
| `ModuleNotFoundError` when running `manage.py` | Virtualenv isn't activated. Re-run the `activate` command from step 3. |
| `Port 8000 already in use` | Run on another port: `python manage.py runserver 8001`. |
| `django.db.utils.OperationalError: FATAL: database "goodtip" does not exist` | Run step 2 again. |
| Need to start from scratch | Drop the DB: `sudo -u postgres psql -c "DROP DATABASE goodtip;"` then redo from step 2. |
