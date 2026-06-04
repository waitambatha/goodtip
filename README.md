# Goodtip

Django sports tipping app.

## Quick start (local dev)

**Prerequisite:** [Docker Desktop](https://www.docker.com/products/docker-desktop) installed and running. Nothing else is required — Python, Postgres, and all dependencies run inside containers.

### Mac / Linux

```bash
chmod +x setup.sh
./setup.sh
```

### Windows

Double-click `setup.bat`, or from a terminal in the project folder:

```cmd
setup.bat
```

When the script finishes, open **http://localhost:8000**.

- Admin panel: http://localhost:8000/admin
- Default admin login: `admin` / `admin` (change this if anyone else can reach the app)

## What the setup script does

1. Verifies Docker is installed.
2. Creates `.env` from `.env.example` and fills in a random `SECRET_KEY`.
3. Builds the web image and starts Postgres + web with `docker compose up -d --build`.
4. Waits for the app to respond on port 8000.
5. Creates a default `admin / admin` Django superuser if one doesn't exist.

## Common commands

| Action                | Command                                            |
|-----------------------|----------------------------------------------------|
| Stop the app          | `docker compose down`                              |
| Start again           | `docker compose up -d`                             |
| View logs             | `docker compose logs -f web`                       |
| Shell into web        | `docker compose exec web bash`                     |
| Run Django management | `docker compose exec web python manage.py <cmd>`   |
| Reset everything      | `docker compose down -v` (wipes the database)      |

## Environment variables

The script copies `.env.example` to `.env`. Edit `.env` if you want to fill in:

- `THESPORTS_API_KEY` — sports data API key
- `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` — outbound SMTP for password resets, etc.

After editing `.env`, restart: `docker compose up -d`.

## Troubleshooting

- **Port 8000 already in use** — stop whatever else is using it, or change `"8000:8000"` to `"8001:8000"` in `docker-compose.yml`.
- **`docker compose` command not found** — update Docker Desktop; the compose plugin is bundled with recent versions.
- **App returns 500 errors** — check logs with `docker compose logs -f web`.
