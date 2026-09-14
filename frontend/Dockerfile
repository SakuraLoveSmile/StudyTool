FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS build
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
COPY frontend/vendor ./vendor
RUN npm ci
COPY frontend/ ./

# 反馈组件的构建期配置（Vite 的 import.meta.env 在打包时静态内联，必须是构建参数，
# 仅用容器运行时环境变量不会生效）。两个都为空 -> 产物里没有反馈入口；
# 两个都填写 -> 产物预置该反馈服务地址与 appId。compose.yaml 用
# ${VITE_FEEDBACK_API_BASE:-} / ${VITE_FEEDBACK_APP_ID:-} 传入。
ARG VITE_FEEDBACK_API_BASE=
ARG VITE_FEEDBACK_APP_ID=
ENV VITE_FEEDBACK_API_BASE=$VITE_FEEDBACK_API_BASE
ENV VITE_FEEDBACK_APP_ID=$VITE_FEEDBACK_APP_ID
RUN npm run build

FROM caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /src/dist /srv
RUN addgroup -S app && adduser -S -G app app \
    && mkdir -p /data/caddy /config/caddy \
    && chown -R app:app /srv /data/caddy /config/caddy /etc/caddy
USER app
EXPOSE 8080 8081 8443
