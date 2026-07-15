#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ENV_FILE="/etc/nginx-manager/server.env"
BACKUP_DIR="${1:-/var/backups/nginx-manager}"

[[ "${EUID}" -eq 0 ]] || { echo "请使用 root 或 sudo 运行" >&2; exit 1; }
[[ -f "${ENV_FILE}" ]] || { echo "未发现 ${ENV_FILE}" >&2; exit 1; }

# shellcheck disable=SC1090
source "${ENV_FILE}"
DB_PATH="${NGINX_MANAGER_DB_PATH:-/var/lib/nginx-manager/manager.db}"
PYTHON_BIN="/opt/nginx-manager/current/venv/bin/python"
[[ -x "${PYTHON_BIN}" ]] || PYTHON_BIN="python3"
[[ -f "${DB_PATH}" ]] || { echo "未发现 SQLite 数据库 ${DB_PATH}" >&2; exit 1; }

install -d -m 0700 "${BACKUP_DIR}"
WORK_DIR="$(mktemp -d "${BACKUP_DIR}/.backup.XXXXXX")"
trap 'rm -rf -- "${WORK_DIR}"' EXIT

"${PYTHON_BIN}" - "${DB_PATH}" "${WORK_DIR}/manager.db" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1], timeout=30)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY

install -d -m 0700 "${WORK_DIR}/etc"
cp -a /etc/nginx-manager/. "${WORK_DIR}/etc/"
date -u +'%Y-%m-%dT%H:%M:%SZ' >"${WORK_DIR}/created-at.txt"

ARCHIVE="${BACKUP_DIR}/nginx-manager-$(date -u +'%Y%m%dT%H%M%SZ').tar.gz"
tar -C "${WORK_DIR}" -czf "${ARCHIVE}" manager.db etc created-at.txt
chmod 0600 "${ARCHIVE}"
echo "备份完成：${ARCHIVE}"
echo "归档包含密码摘要、机器身份摘要、LDAP 查询密码及可能存在的 TLS 私钥；复制到远端前请再次加密。"
