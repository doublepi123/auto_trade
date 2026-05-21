FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .
COPY docker-entrypoint.sh .

RUN mkdir -p data && useradd -m -s /bin/bash appuser && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["./docker-entrypoint.sh"]
