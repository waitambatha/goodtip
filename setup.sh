#!/usr/bin/env bash
set -euo pipefail

echo "==> Goodtip local setup"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed."
  echo "Install Docker Desktop from https://www.docker.com/products/docker-desktop and re-run this script."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: 'docker compose' is not available. Update Docker Desktop and try again."
  exit 1
fi

if [ ! -f .env ]; then
  echo "==> Creating .env from .env.example with a random SECRET_KEY"
  cp .env.example .env
  SECRET=$(openssl rand -base64 48 | tr -d '\n/=+' | cut -c1-50)
  sed "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" .env > .env.tmp && mv .env.tmp .env
else
  echo "==> .env already exists, leaving it as-is"
fi

echo "==> Building and starting containers (first run can take a few minutes)"
docker compose up -d --build

echo "==> Waiting for the app to respond on http://localhost:8000 ..."
for i in $(seq 1 60); do
  if curl -sf -o /dev/null http://localhost:8000/; then
    break
  fi
  sleep 2
done

echo "==> Ensuring default admin user (admin / admin)"
docker compose exec -T web python manage.py shell -c "
from django.contrib.auth import get_user_model
U = get_user_model()
if not U.objects.filter(username='admin').exists():
    U.objects.create_superuser('admin', 'admin@example.com', 'admin')
    print('Created superuser admin/admin')
else:
    print('Superuser admin already exists')
"

cat <<EOF

============================================================
 Goodtip is running at: http://localhost:8000
 Admin panel:           http://localhost:8000/admin
   username: admin
   password: admin

 Stop:    docker compose down
 Restart: docker compose up -d
 Logs:    docker compose logs -f web
============================================================
EOF
