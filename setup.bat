@echo off
setlocal EnableDelayedExpansion

echo ==^> Goodtip local setup

REM --- 1. Python ----------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed.
    echo Install Python 3.12+ from https://www.python.org/downloads/
    echo Make sure to tick "Add Python to PATH" during install.
    pause
    exit /b 1
)

REM --- 2. Postgres --------------------------------------------------------
where psql >nul 2>&1
if errorlevel 1 (
    echo ERROR: PostgreSQL is not installed or psql is not on PATH.
    echo Install PostgreSQL from https://www.postgresql.org/download/windows/
    echo Make sure to add the PostgreSQL bin folder to PATH.
    pause
    exit /b 1
)

echo ==^> Ensuring Postgres role 'mbatha' and database 'goodtip' exist
echo     You may be prompted for the postgres superuser password.
psql -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='mbatha'" | findstr 1 >nul
if errorlevel 1 (
    echo     Creating role 'mbatha'
    psql -U postgres -c "CREATE ROLE mbatha WITH LOGIN PASSWORD 'masterclass' CREATEDB;"
)
psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='goodtip'" | findstr 1 >nul
if errorlevel 1 (
    echo     Creating database 'goodtip'
    psql -U postgres -c "CREATE DATABASE goodtip OWNER mbatha;"
)

REM --- 3. venv + dependencies --------------------------------------------
if not exist venv (
    echo ==^> Creating Python virtual environment in .\venv
    python -m venv venv
)

echo ==^> Installing Python dependencies
venv\Scripts\python -m pip install --quiet --upgrade pip
venv\Scripts\pip install --quiet -r requirements.txt

REM --- 4. .env ------------------------------------------------------------
if not exist .env (
    echo ==^> Creating .env from .env.example with a random SECRET_KEY
    powershell -NoProfile -Command "$bytes=New-Object byte[] 36; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes); $s=[Convert]::ToBase64String($bytes) -replace '[/+=]',''; (Get-Content .env.example) -replace '^SECRET_KEY=.*', \"SECRET_KEY=$s\" | Set-Content .env"
) else (
    echo ==^> .env already exists, leaving it as-is
)

REM --- 5. Migrate + seed --------------------------------------------------
echo ==^> Running database migrations
venv\Scripts\python manage.py migrate --noinput

echo ==^> Seeding teams
venv\Scripts\python manage.py seed_teams

REM --- 6. Default admin user ---------------------------------------------
echo ==^> Ensuring default admin user (admin@example.com / admin)
venv\Scripts\python manage.py shell -c "from django.contrib.auth import get_user_model; U=get_user_model(); U.objects.filter(email='admin@example.com').exists() or U.objects.create_superuser(email='admin@example.com', password='admin', display_name='Admin')"

REM --- 7. Run the dev server ---------------------------------------------
echo.
echo ============================================================
echo  Goodtip is ready.
echo.
echo  Sign in:
echo    URL:      http://localhost:8000/admin
echo    email:    admin@example.com
echo    password: admin
echo.
echo  Starting the dev server now on http://localhost:8000
echo  (press Ctrl+C to stop)
echo ============================================================
echo.

venv\Scripts\python manage.py runserver 8000
