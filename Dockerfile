FROM python:3.12-slim

WORKDIR /app

# Copy the entire project first
COPY . .

# Install dependencies including psycopg2
RUN pip install --no-cache-dir psycopg2-binary -e .

# Run the backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
