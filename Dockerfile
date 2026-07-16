FROM python:3.12-slim

WORKDIR /app

# Copy the entire project first
COPY . .

# Install dependencies plus psycopg2 for plain postgresql:// URLs.
RUN pip install --no-cache-dir psycopg2-binary -e .

# Run migrations before starting the backend process.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
