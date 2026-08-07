#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ENV_FILE="/etc/nginx-manager/server.env"
BACKUP_DIR="${1:-/var/backups/nginx-manager}"
DATA_DIR="/var/lib/nginx-manager"

read_env_value() {
  local env_file="$1" key="$2" line value
  local -a matches=()
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    case "${line}" in
      "${key}="*) matches+=("${line#*=}") ;;
    esac
  done <"${env_file}"
  (( ${#matches[@]} <= 1 )) || {
    echo "${env_file} 重复定义 ${key}" >&2
    return 1
  }
  (( ${#matches[@]} == 1 )) || return 0
  value="${matches[0]}"
  case "${value}" in
    \"*\")
      (( ${#value} >= 2 )) || { echo "${env_file} 中 ${key} 的外层引号不完整" >&2; return 1; }
      value="${value:1:${#value}-2}"
      ;;
    \'*\')
      (( ${#value} >= 2 )) || { echo "${env_file} 中 ${key} 的外层引号不完整" >&2; return 1; }
      value="${value:1:${#value}-2}"
      ;;
    \"*|*\"|\'*|*\')
      echo "${env_file} 中 ${key} 的外层引号不完整" >&2
      return 1
      ;;
  esac
  printf '%s' "${value}"
}

[[ "${EUID}" -eq 0 ]] || { echo "请使用 root 或 sudo 运行" >&2; exit 1; }
[[ -f "${ENV_FILE}" ]] || { echo "未发现 ${ENV_FILE}" >&2; exit 1; }

DB_PATH="$(read_env_value "${ENV_FILE}" NGINX_MANAGER_DB_PATH)"
DB_PATH="${DB_PATH:-/var/lib/nginx-manager/manager.db}"
DB_NAME="$(basename -- "${DB_PATH}")"
ATTACHMENTS_DIR="$(read_env_value "${ENV_FILE}" NGINX_MANAGER_ATTACHMENTS_DIR)"
ATTACHMENTS_DIR="${ATTACHMENTS_DIR:-$(dirname -- "${DB_PATH}")/attachments}"
PYTHON_BIN="/opt/nginx-manager/current/venv/bin/python"
[[ -x "${PYTHON_BIN}" ]] || PYTHON_BIN="python3"
[[ -f "${DB_PATH}" ]] || { echo "未发现 SQLite 数据库 ${DB_PATH}" >&2; exit 1; }
[[ "${DB_PATH}" == /* && "${DB_NAME}" != "." && "${DB_NAME}" != ".." ]] || {
  echo "数据库路径必须是安全的绝对路径" >&2
  exit 1
}
resolved_data="$(readlink -m -- "${DATA_DIR}")"
resolved_db="$(readlink -m -- "${DB_PATH}")"
resolved_attachments="$(readlink -m -- "${ATTACHMENTS_DIR}")"
resolved_backup="$(readlink -m -- "${BACKUP_DIR}")"
[[ "${resolved_db}" == "${resolved_data}/"* ]] || {
  echo "数据库 ${DB_PATH} 不在容器持久化目录 ${DATA_DIR} 内" >&2
  exit 1
}
[[ "$(dirname -- "${resolved_db}")" == "${resolved_data}" ]] || {
  echo "数据库必须是 ${DATA_DIR} 的直接子文件（文件名可自定义）" >&2
  exit 1
}
[[ "${resolved_db}" == "${resolved_data}/manager.db" || ! -e "${DATA_DIR}/manager.db" ]] || {
  echo "自定义数据库文件名与 ${DATA_DIR}/manager.db 冲突，无法生成无损备份" >&2
  exit 1
}
[[ "${resolved_attachments}" == "${resolved_data}" || "${resolved_attachments}" == "${resolved_data}/"* ]] || {
  echo "附件目录 ${ATTACHMENTS_DIR} 不在 ${DATA_DIR} 内，本备份无法安全包含它" >&2
  echo "请先将 NGINX_MANAGER_ATTACHMENTS_DIR 迁到 ${DATA_DIR}/attachments" >&2
  exit 1
}
[[ "${resolved_backup}" != "${resolved_data}" && "${resolved_backup}" != "${resolved_data}/"* ]] || {
  echo "备份目录不能放在数据目录 ${DATA_DIR} 内，避免递归复制备份自身" >&2
  exit 1
}

install -d -m 0700 "${BACKUP_DIR}"
WORK_DIR="$(mktemp -d "${BACKUP_DIR}/.backup.XXXXXX")"
ARCHIVE_TMP=""
cleanup() {
  rm -rf -- "${WORK_DIR}"
  [[ -z "${ARCHIVE_TMP}" || ! -e "${ARCHIVE_TMP}" ]] || rm -f -- "${ARCHIVE_TMP}"
}
trap cleanup EXIT

install -d -m 0700 "${WORK_DIR}/data"
# 先复制数据库之外的持久化内容，再将 SQLite backup API 的一致快照固定写为
# data/manager.db；database-name.txt 与 server.env 共同记录其恢复目标。
cp -a -- "${DATA_DIR}/." "${WORK_DIR}/data/"
database_relative="${resolved_db#"${resolved_data}/"}"
rm -f -- \
  "${WORK_DIR}/data/${database_relative}" \
  "${WORK_DIR}/data/${database_relative}-journal" \
  "${WORK_DIR}/data/${database_relative}-wal" \
  "${WORK_DIR}/data/${database_relative}-shm"
"${PYTHON_BIN}" - "${DB_PATH}" "${WORK_DIR}/data/manager.db" <<'PY'
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

# Online backups may race with an attachment deletion. Refuse a snapshot whose
# SQLite metadata references a file that was not copied, rather than producing
# an archive that looks valid but has a broken screenshot after restore.
attachments_relative="${resolved_attachments#"${resolved_data}"}"
attachments_relative="${attachments_relative#/}"
attachments_snapshot="${WORK_DIR}/data"
[[ -z "${attachments_relative}" ]] || attachments_snapshot="${attachments_snapshot}/${attachments_relative}"
"${PYTHON_BIN}" - "${WORK_DIR}/data/manager.db" "${attachments_snapshot}" <<'PY'
import pathlib
import sqlite3
import sys

database_path = sys.argv[1]
attachments_path = pathlib.Path(sys.argv[2])
connection = sqlite3.connect("file:{}?mode=ro".format(database_path), uri=True)
try:
    has_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'site_attachments'"
    ).fetchone()
    expected = [] if has_table is None else [
        row[0] for row in connection.execute("SELECT storage_name FROM site_attachments")
    ]
finally:
    connection.close()
missing = [
    name for name in expected
    if not (attachments_path / name).is_file() or (attachments_path / name).is_symlink()
]
if missing:
    raise SystemExit(
        "attachment snapshot is incomplete; retry the backup (missing {} file(s))".format(len(missing))
    )
PY

install -d -m 0700 "${WORK_DIR}/etc"
cp -a /etc/nginx-manager/. "${WORK_DIR}/etc/"
date -u +'%Y-%m-%dT%H:%M:%SZ' >"${WORK_DIR}/created-at.txt"
printf '2\n' >"${WORK_DIR}/format-version.txt"
printf '%s\n' "${DB_NAME}" >"${WORK_DIR}/database-name.txt"

ARCHIVE="${BACKUP_DIR}/nginx-manager-$(date -u +'%Y%m%dT%H%M%SZ').tar.gz"
ARCHIVE_TMP="${ARCHIVE}.tmp.$$"
tar -C "${WORK_DIR}" -czf "${ARCHIVE_TMP}" data etc created-at.txt format-version.txt database-name.txt
chmod 0600 "${ARCHIVE_TMP}"
mv -- "${ARCHIVE_TMP}" "${ARCHIVE}"
ARCHIVE_TMP=""
echo "备份完成：${ARCHIVE}"
echo "归档包含数据库、备注附件、密码摘要、机器身份摘要、LDAP 查询密码及可能存在的 TLS 私钥；复制到远端前请再次加密。"
