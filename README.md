# Playto Payout Engine

Minimal payout engine for the Playto founding engineer challenge. It lets merchants view balances, request payouts with idempotency keys, and watch a background worker move payouts through `pending -> processing -> completed/failed`.

## Stack

- Backend: Django, Django REST Framework, Celery
- Frontend: React, Vite, Tailwind, lucide-react
- Database: PostgreSQL
- Queue: Redis

## Quick Start With Docker

```bash
docker compose up --build
```

The compose stack runs Postgres, Redis, Django, Celery worker, Celery beat, and the frontend.

- API: http://localhost:8000/api/health/
- Dashboard: http://localhost:8080
- Explainer: http://localhost:8080/explainer

Seed data is loaded automatically by `python manage.py seed_playto`.

## Local Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy ..\.env.example .env
python manage.py migrate
python manage.py seed_playto
python manage.py runserver
```

Run worker processes in separate terminals:

```bash
celery -A config worker -l info
celery -A config beat -l info
```

## Local Frontend

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server defaults to http://localhost:5173 and calls `http://localhost:8000`.

The in-app explainer is available at http://localhost:5173/explainer.

## API

Pick a merchant from:

```bash
curl http://localhost:8000/api/v1/merchants
```

Request a payout:

```bash
curl -X POST http://localhost:8000/api/v1/payouts \
  -H "Content-Type: application/json" \
  -H "X-Merchant-Id: <merchant_uuid>" \
  -H "Idempotency-Key: <uuid>" \
  -d "{\"amount_paise\":6000,\"bank_account_id\":\"<bank_account_uuid>\"}"
```

Simulate a customer payment credit:

```bash
curl -X POST http://localhost:8000/api/v1/credits/simulate \
  -H "Content-Type: application/json" \
  -H "X-Merchant-Id: <merchant_uuid>" \
  -d "{\"amount_paise\":10000,\"description\":\"Simulated customer payment\"}"
```

## Tests

```bash
cd backend
pytest
```

The idempotency test runs on any Django-supported database. The concurrency test intentionally skips unless the test database is PostgreSQL, because the assignment is about real row-level locks.

## Deployment Notes

For Render/Railway/Fly/Koyeb, provision:

- PostgreSQL database
- Redis instance
- Web process from `backend/Procfile`
- Worker process from `backend/Procfile`
- Beat process from `backend/Procfile`

Set `DATABASE_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `SECRET_KEY`, `ALLOWED_HOSTS`, and `CORS_ALLOWED_ORIGINS`.

For this deployed pair:

- Frontend `VITE_API_BASE_URL`: `https://playto-assignment-09c4.onrender.com`
- Backend `CORS_ALLOWED_ORIGINS`: `https://playto-assignment-zeta.vercel.app`

The frontend now falls back to localhost only during local development. In production, if `VITE_API_BASE_URL` is missing, it will use the Render backend URL above.
