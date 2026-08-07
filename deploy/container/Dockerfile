FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NGINX_MANAGER_DB_PATH=/var/lib/nginx-manager/manager.db \
    NGINX_MANAGER_UI_PATH=/app/ui/index.html

WORKDIR /app/server

COPY server/requirements.txt /app/server/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/server/requirements.txt

COPY server/*.py /app/server/
COPY frontend/release/index.html /app/ui/index.html
COPY frontend/public/ui-assets /app/ui/ui-assets
COPY deploy/container/entrypoint.sh /usr/local/bin/nginx-manager-container

RUN chmod 0755 /usr/local/bin/nginx-manager-container \
    && mkdir -p /var/lib/nginx-manager \
    && chown 10001:10001 /var/lib/nginx-manager

USER 10001:10001
EXPOSE 8443

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=6 \
    CMD ["/usr/local/bin/nginx-manager-container", "healthcheck"]

ENTRYPOINT ["/usr/local/bin/nginx-manager-container"]
