@echo off
setlocal

echo ==^> Goodtip local setup

where docker >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker is not installed.
    echo Install Docker Desktop from https://www.docker.com/products/docker-desktop and re-run.
    pause
    exit /b 1
)

if not exist .env (
    echo ==^> Creating .env from .env.example with a random SECRET_KEY
    powershell -NoProfile -Command "$bytes=New-Object byte[] 36; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes); $s=[Convert]::ToBase64String($bytes) -replace '[/+=]',''; (Get-Content .env.example) -replace '^SECRET_KEY=.*', \"SECRET_KEY=$s\" | Set-Content .env"
) else (
    echo ==^> .env already exists, leaving it as-is
)

echo ==^> Building and starting containers (first run can take a few minutes)
docker compose up -d --build
if errorlevel 1 (
    echo ERROR: docker compose failed
    pause
    exit /b 1
)

echo ==^> Waiting for the app to respond on http://localhost:8000 ...
powershell -NoProfile -Command "for ($i=0; $i -lt 60; $i++) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://localhost:8000/ ^| Out-Null; break } catch { Start-Sleep 2 } }"

echo ==^> Ensuring default admin user (admin / admin)
docker compose exec -T web python manage.py shell -c "from django.contrib.auth import get_user_model; U=get_user_model(); U.objects.filter(username='admin').exists() or U.objects.create_superuser('admin','admin@example.com','admin')"

echo.
echo ============================================================
echo  Goodtip is running at: http://localhost:8000
echo  Admin panel:           http://localhost:8000/admin
echo    username: admin
echo    password: admin
echo.
echo  Stop:    docker compose down
echo  Restart: docker compose up -d
echo  Logs:    docker compose logs -f web
echo ============================================================
pause
