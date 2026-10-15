FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app
COPY backend/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --timeout 60 --require-hashes -r /tmp/requirements.lock
COPY backend/app /app/app
RUN mkdir -p /data/logs && chown -R app:app /app /data

USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
