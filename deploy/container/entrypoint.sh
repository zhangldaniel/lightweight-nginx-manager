#!/bin/sh
set -eu

bind_address="${NGINX_MANAGER_CONTAINER_BIND:-0.0.0.0}"
port="${NGINX_MANAGER_CONTAINER_PORT:-8443}"
tls="${NGINX_MANAGER_CONTAINER_TLS:-0}"

if [ "${1:-}" = "healthcheck" ]; then
  health_host="${bind_address}"
  case "${health_host}" in
    0.0.0.0) health_host="127.0.0.1" ;;
    ::) health_host="::1" ;;
  esac
  case "${health_host}" in
    *:*) health_authority="[${health_host}]" ;;
    *) health_authority="${health_host}" ;;
  esac
  export NGINX_MANAGER_HEALTH_URL="$(if [ "${tls}" = "1" ]; then printf https; else printf http; fi)://${health_authority}:${port}/healthz"
  exec python - <<'PY'
import os
import ssl
import urllib.request

url = os.environ["NGINX_MANAGER_HEALTH_URL"]
context = ssl._create_unverified_context() if url.startswith("https://") else None
with urllib.request.urlopen(url, timeout=2, context=context) as response:
    if response.status != 200:
        raise SystemExit("health endpoint returned {}".format(response.status))
PY
fi

set -- python -m uvicorn app:app \
  --host "${bind_address}" \
  --port "${port}" \
  --no-server-header

if [ "${NGINX_MANAGER_CONTAINER_PROXY_HEADERS:-0}" = "1" ]; then
  set -- "$@" --proxy-headers --forwarded-allow-ips 127.0.0.1
fi

if [ "${tls}" = "1" ]; then
  set -- "$@" \
    --ssl-keyfile /etc/nginx-manager/tls/server.key \
    --ssl-certfile /etc/nginx-manager/tls/server.crt
fi

exec "$@"
