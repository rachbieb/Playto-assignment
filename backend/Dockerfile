FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["sh", "-c", "python manage.py migrate && python manage.py seed_playto && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000}"]
