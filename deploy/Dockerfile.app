FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
COPY frontend/vendor ./vendor
RUN npm ci
COPY frontend/ ./
ARG VITE_FEEDBACK_API_BASE
ARG VITE_FEEDBACK_APP_ID
RUN npm run build

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app \
    FRONTEND_DIST_DIR=/app/frontend_dist
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app
COPY backend/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --timeout 60 --require-hashes -r /tmp/requirements.lock
COPY backend/app /app/app
COPY --from=frontend /src/dist /app/frontend_dist
RUN mkdir -p /data/logs && chown -R app:app /app /data
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
