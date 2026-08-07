#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_NAME="nginx-manager"
ETC_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
BACKUP_DIR="/var/backups/${APP_NAME}"
ARCHIVE="${1:-}"
WORK_DIR=""
STAGED_ETC=""
STAGED_DATA=""
PREVIOUS_ETC=""
PREVIOUS_DATA=""
SWAP_STARTED=0
DATA_INSTALLED=0
ETC_INSTALLED=0
RESTORE_COMMITTED=0

die() {
  echo "错误：$*" >&2
  exit 1
}

read_env_value() {
  local env_file="$1" key="$2" line value
  local -a matches=()
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    case "${line}" in
      "${key}="*) matches+=("${line#*=}") ;;
    esac
  done <"${env_file}"
  (( ${#matches[@]} <= 1 )) || die "${env_file} 重复定义 ${key}"
  (( ${#matches[@]} == 1 )) || return 0
  value="${matches[0]}"
  case "${value}" in
    \"*\")
      (( ${#value} >= 2 )) || die "${env_file} 中 ${key} 的外层引号不完整"
      value="${value:1:${#value}-2}"
      ;;
    \'*\')
      (( ${#value} >= 2 )) || die "${env_file} 中 ${key} 的外层引号不完整"
      value="${value:1:${#value}-2}"
      ;;
    \"*|*\"|\'*|*\') die "${env_file} 中 ${key} 的外层引号不完整" ;;
  esac
  printf '%s' "${value}"
}

cleanup() {
  [[ -z "${WORK_DIR}" || ! -e "${WORK_DIR}" ]] || rm -rf -- "${WORK_DIR}"
  [[ -z "${STAGED_ETC}" || ! -e "${STAGED_ETC}" ]] || rm -rf -- "${STAGED_ETC}"
  [[ -z "${STAGED_DATA}" || ! -e "${STAGED_DATA}" ]] || rm -rf -- "${STAGED_DATA}"
}

rollback_path() {
  local current="$1" previous="$2" installed="$3" label="$4" result=0
  if [[ "${installed}" == "1" && -e "${current}" ]]; then
    rm -rf -- "${current}" || result=1
  fi
  if [[ -n "${previous}" && -e "${previous}" ]]; then
    if [[ -e "${current}" ]]; then
      echo "警告：${label} 回滚目标已存在，原目录保留在 ${previous}" >&2
      result=1
    else
      mv -- "${previous}" "${current}" || result=1
    fi
  fi
  return "${result}"
}

rollback_restore() {
  local result=0
  echo "恢复未完成，正在还原原目录" >&2
  rollback_path "${ETC_DIR}" "${PREVIOUS_ETC}" "${ETC_INSTALLED}" "配置目录" || result=1
  rollback_path "${DATA_DIR}" "${PREVIOUS_DATA}" "${DATA_INSTALLED}" "数据目录" || result=1
  if (( result != 0 )); then
    echo "错误：自动回滚未完整成功；请检查 ${PREVIOUS_DATA:-数据回滚目录} 和 ${PREVIOUS_ETC:-配置回滚目录}" >&2
  fi
  return "${result}"
}

finish_exit() {
  local status="$?"
  trap - EXIT ERR INT TERM HUP
  if (( SWAP_STARTED == 1 && RESTORE_COMMITTED == 0 )); then
    rollback_restore || status=1
  fi
  cleanup
  exit "${status}"
}

trap finish_exit EXIT
trap 'exit $?' ERR
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

[[ "${EUID}" -eq 0 ]] || die "请使用 root 或 sudo 运行"
[[ -n "${ARCHIVE}" && -f "${ARCHIVE}" && ! -L "${ARCHIVE}" ]] || \
  die "用法：sudo $0 <nginx-manager-*.tar.gz>"
command -v python3 >/dev/null 2>&1 || die "未找到 python3"
command -v tar >/dev/null 2>&1 || die "未找到 tar"

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "${APP_NAME}.service"; then
  die "请先停止 ${APP_NAME}.service 再恢复，避免 SQLite 被同时写入"
fi
if command -v docker >/dev/null 2>&1 && \
   [[ "$(docker inspect -f '{{.State.Running}}' nginx-manager-server 2>/dev/null || true)" == "true" ]]; then
  die "请先停止 nginx-manager-server 容器再恢复"
fi

while IFS= read -r entry; do
  [[ -n "${entry}" && "${entry}" != /* && ! "${entry}" =~ (^|/)\.\.(/|$) ]] || \
    die "备份包含不安全路径：${entry}"
  case "${entry}" in
    data|data/*|etc|etc/*|manager.db|created-at.txt|format-version.txt|database-name.txt) ;;
    *) die "备份包含未知项：${entry}" ;;
  esac
done < <(tar -tzf "${ARCHIVE}")

WORK_DIR="$(mktemp -d /var/tmp/nginx-manager-restore.XXXXXX)"
tar --no-same-owner -xzf "${ARCHIVE}" -C "${WORK_DIR}"
[[ -z "$(find "${WORK_DIR}" -type l -print -quit)" ]] || die "备份包不允许包含符号链接"
[[ -f "${WORK_DIR}/etc/server.env" ]] || die "备份包缺少 etc/server.env"

ARCHIVED_DB_PATH="$(read_env_value "${WORK_DIR}/etc/server.env" NGINX_MANAGER_DB_PATH)"
ARCHIVED_DB_PATH="${ARCHIVED_DB_PATH:-/var/lib/nginx-manager/manager.db}"
[[ "${ARCHIVED_DB_PATH}" == /* ]] || die "备份中的数据库路径不是绝对路径"
resolved_data="$(readlink -m -- "${DATA_DIR}")"
resolved_archived_db="$(readlink -m -- "${ARCHIVED_DB_PATH}")"
[[ "${resolved_archived_db}" == "${resolved_data}/"* ]] || \
  die "备份中的数据库 ${ARCHIVED_DB_PATH} 不在 ${DATA_DIR} 内"
[[ "$(dirname -- "${resolved_archived_db}")" == "${resolved_data}" ]] || \
  die "备份中的数据库必须是 ${DATA_DIR} 的直接子文件"
DATABASE_RELATIVE="${resolved_archived_db#"${resolved_data}/"}"
ARCHIVED_DB_NAME="$(basename -- "${resolved_archived_db}")"

if [[ -f "${WORK_DIR}/data/manager.db" ]]; then
  [[ -f "${WORK_DIR}/database-name.txt" && ! -L "${WORK_DIR}/database-name.txt" ]] || \
    die "新版备份缺少 database-name.txt"
  mapfile -t archived_db_names <"${WORK_DIR}/database-name.txt"
  (( ${#archived_db_names[@]} == 1 )) || die "database-name.txt 必须只包含一行"
  DATABASE_NAME="${archived_db_names[0]%$'\r'}"
  [[ -n "${DATABASE_NAME}" && "${DATABASE_NAME}" != "." && "${DATABASE_NAME}" != ".." && \
     "${DATABASE_NAME}" != */* ]] || die "database-name.txt 包含不安全的文件名"
  [[ "${DATABASE_NAME}" == "${ARCHIVED_DB_NAME}" ]] || \
    die "database-name.txt 与 server.env 的数据库文件名不一致"
  SOURCE_DATA="${WORK_DIR}/data"
  SOURCE_DB="${SOURCE_DATA}/manager.db"
elif [[ -f "${WORK_DIR}/manager.db" ]]; then
  # 兼容 2026-08 之前只包含 SQLite 和 etc 的旧归档。
  SOURCE_DATA="${WORK_DIR}/legacy-data"
  install -d -m 0700 "${SOURCE_DATA}"
  cp -a -- "${WORK_DIR}/manager.db" "${SOURCE_DATA}/manager.db"
  SOURCE_DB="${SOURCE_DATA}/manager.db"
else
  die "备份包缺少 data/manager.db"
fi

python3 - "${SOURCE_DB}" <<'PY' || die "SQLite quick_check 未通过，拒绝恢复"
import sqlite3
import sys

connection = sqlite3.connect("file:{}?mode=ro".format(sys.argv[1]), uri=True, timeout=10)
try:
    result = connection.execute("PRAGMA quick_check").fetchone()
finally:
    connection.close()
if result is None or result[0] != "ok":
    raise SystemExit("quick_check failed: {!r}".format(result))
PY

if id "${APP_NAME}" >/dev/null 2>&1; then
  APP_UID="$(id -u "${APP_NAME}")"
  APP_GID="$(id -g "${APP_NAME}")"
else
  APP_UID="${NGINX_MANAGER_UID:-10001}"
  APP_GID="${NGINX_MANAGER_GID:-10001}"
fi
[[ "${APP_UID}" =~ ^[0-9]+$ && "${APP_GID}" =~ ^[0-9]+$ ]] || die "运行 UID/GID 无效"

STAGED_DATA="/var/lib/.${APP_NAME}.restore.$$"
STAGED_ETC="/etc/.${APP_NAME}.restore.$$"
install -d -m 0750 "${STAGED_DATA}" "${STAGED_ETC}"
cp -a -- "${SOURCE_DATA}/." "${STAGED_DATA}/"
cp -a -- "${WORK_DIR}/etc/." "${STAGED_ETC}/"
DATABASE_TARGET="${STAGED_DATA}/${DATABASE_RELATIVE}"
if [[ "${DATABASE_TARGET}" != "${STAGED_DATA}/manager.db" ]]; then
  [[ ! -e "${DATABASE_TARGET}" ]] || die "备份数据与数据库恢复路径冲突：${DATABASE_RELATIVE}"
  install -d -m 0750 "$(dirname -- "${DATABASE_TARGET}")"
  mv -- "${STAGED_DATA}/manager.db" "${DATABASE_TARGET}"
fi
chown -R "${APP_UID}:${APP_GID}" "${STAGED_DATA}"
chown -R "root:${APP_GID}" "${STAGED_ETC}"
chmod 0750 "${STAGED_DATA}" "${STAGED_ETC}"
chmod 0600 "${DATABASE_TARGET}"
chmod 0640 "${STAGED_ETC}/server.env"

timestamp="$(date -u +'%Y%m%dT%H%M%SZ')"
PREVIOUS_DATA="/var/lib/.${APP_NAME}.previous.${timestamp}"
PREVIOUS_ETC="/etc/.${APP_NAME}.previous.${timestamp}"
[[ ! -e "${PREVIOUS_DATA}" && ! -e "${PREVIOUS_ETC}" ]] || die "恢复回滚目录已存在"

SWAP_STARTED=1
[[ ! -e "${DATA_DIR}" ]] || mv -- "${DATA_DIR}" "${PREVIOUS_DATA}"
# Set the rollback state before the rename so an interrupt between the rename
# and the next shell statement still removes the staged replacement.
DATA_INSTALLED=1
if ! mv -- "${STAGED_DATA}" "${DATA_DIR}"; then
  die "无法替换 ${DATA_DIR}"
fi
STAGED_DATA=""

[[ ! -e "${ETC_DIR}" ]] || mv -- "${ETC_DIR}" "${PREVIOUS_ETC}"
# Same ordering is required for /etc: signal traps can run between commands.
ETC_INSTALLED=1
if ! mv -- "${STAGED_ETC}" "${ETC_DIR}"; then
  die "无法替换 ${ETC_DIR}"
fi
STAGED_ETC=""
RESTORE_COMMITTED=1

echo "恢复完成：${DATA_DIR} 和 ${ETC_DIR}"
if [[ -e "${PREVIOUS_DATA}" || -e "${PREVIOUS_ETC}" ]]; then
  echo "原目录保留在：${PREVIOUS_DATA} ${PREVIOUS_ETC}"
fi
echo "运行身份：${APP_UID}:${APP_GID}"
