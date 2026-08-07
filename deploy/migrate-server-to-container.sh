#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_NAME="nginx-manager"
CONTAINER_NAME="nginx-manager-server"
CONTAINER_ROOT="/opt/nginx-manager-container"
ETC_DIR="/etc/nginx-manager"
DATA_DIR="/var/lib/nginx-manager"
SERVICE="nginx-manager.service"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PACKAGE_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_SOURCE="${SCRIPT_DIR}/container/compose.yaml"
DOCKERFILE="${SCRIPT_DIR}/container/Dockerfile"
IMAGE="nginx-manager:local"
BACKUP_ARCHIVE=""
BIND_ADDRESS=""
PORT=""
TLS_MODE=""
PROXY_HEADERS=""
OLD_SERVICE_ACTIVE="0"
ROLLBACK_REQUIRED="0"

usage() {
  cat <<'USAGE'
用法：
  sudo ./deploy/migrate-server-to-container.sh [选项]

选项：
  --backup <归档>       先导入 backup-server.sh 生成的归档（跨机迁移）
  --bind-address <IP>  容器监听地址；默认沿用现有 systemd 服务
  --port <端口>         容器监听端口；默认沿用现有端口或 8443
  --tls                容器直接使用 /etc/nginx-manager/tls 中的证书
  --behind-nginx       仅监听 127.0.0.1，并信任本机 Nginx 代理头
  --image <名称>        本地镜像名，默认 nginx-manager:local
  -h, --help           显示帮助
USAGE
}

die() {
  echo "错误：$*" >&2
  exit 1
}

log() {
  echo "[nginx-manager-container] $*"
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

compose() {
  docker compose --project-directory "${CONTAINER_ROOT}" -f "${CONTAINER_ROOT}/compose.yaml" "$@"
}

rollback_and_exit() {
  local status="$1"
  trap - ERR INT TERM HUP
  if [[ "${ROLLBACK_REQUIRED}" == "1" ]]; then
    echo "[回滚] 容器未通过健康检查，恢复原 systemd 服务" >&2
    if ! compose down >/dev/null 2>&1; then
      echo "[回滚警告] 容器未能自动停止，请立即检查 ${CONTAINER_NAME}" >&2
    fi
    if [[ "${OLD_SERVICE_ACTIVE}" == "1" ]]; then
      if ! systemctl enable "${SERVICE}" >/dev/null 2>&1; then
        echo "[回滚警告] 未能重新启用 ${SERVICE}" >&2
      fi
      if ! systemctl start "${SERVICE}"; then
        echo "[回滚警告] 未能重新启动 ${SERVICE}" >&2
      fi
    fi
  fi
  exit "${status}"
}

on_error() {
  local status=$?
  rollback_and_exit "${status}"
}

trap on_error ERR
trap 'rollback_and_exit 130' INT
trap 'rollback_and_exit 143' TERM
trap 'rollback_and_exit 129' HUP

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) [[ $# -ge 2 ]] || die "--backup 缺少值"; BACKUP_ARCHIVE="$2"; shift 2 ;;
    --bind-address) [[ $# -ge 2 ]] || die "--bind-address 缺少值"; BIND_ADDRESS="$2"; shift 2 ;;
    --port) [[ $# -ge 2 ]] || die "--port 缺少值"; PORT="$2"; shift 2 ;;
    --tls) TLS_MODE="1"; shift ;;
    --behind-nginx) BIND_ADDRESS="127.0.0.1"; PROXY_HEADERS="1"; TLS_MODE="0"; shift ;;
    --image) [[ $# -ge 2 ]] || die "--image 缺少值"; IMAGE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || die "请使用 root 或 sudo 运行"
command -v docker >/dev/null 2>&1 || die "请先安装 Docker Engine 与 Compose 插件"
docker compose version >/dev/null 2>&1 || die "未找到 docker compose 插件"
[[ -f "${DOCKERFILE}" && -f "${COMPOSE_SOURCE}" ]] || die "容器部署文件不完整"
[[ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null || true)" != "true" ]] || \
  die "${CONTAINER_NAME} 已在运行；请使用升级流程，不要重复迁移"

if [[ -n "${BACKUP_ARCHIVE}" ]]; then
  bash "${SCRIPT_DIR}/restore-server-backup.sh" "${BACKUP_ARCHIVE}"
fi
[[ -f "${ETC_DIR}/server.env" ]] || die "未发现 ${ETC_DIR}/server.env"

db_path="$(read_env_value "${ETC_DIR}/server.env" NGINX_MANAGER_DB_PATH)"
db_path="${db_path:-${DATA_DIR}/manager.db}"
[[ "${db_path}" == /* ]] || die "数据库路径必须是绝对路径：${db_path}"
resolved_data="$(readlink -m -- "${DATA_DIR}")"
resolved_db="$(readlink -m -- "${db_path}")"
[[ "${resolved_db}" == "${resolved_data}/"* ]] || \
  die "数据库 ${db_path} 在容器持久化目录 ${DATA_DIR} 外"
[[ "$(dirname -- "${resolved_db}")" == "${resolved_data}" ]] || \
  die "容器迁移只支持 ${DATA_DIR} 直接子文件作为数据库（文件名可自定义）"
[[ -f "${db_path}" ]] || die "未发现 SQLite 数据库 ${db_path}"
attachments_dir="$(read_env_value "${ETC_DIR}/server.env" NGINX_MANAGER_ATTACHMENTS_DIR)"
attachments_dir="${attachments_dir:-$(dirname -- "${db_path}")/attachments}"
resolved_attachments="$(readlink -m -- "${attachments_dir}")"
[[ "${resolved_attachments}" == "${resolved_data}" || "${resolved_attachments}" == "${resolved_data}/"* ]] || \
  die "附件目录 ${attachments_dir} 在持久化目录外；请先迁到 ${DATA_DIR}/attachments"

unit_text="$(systemctl cat "${SERVICE}" 2>/dev/null || true)"
if [[ -z "${BIND_ADDRESS}" ]]; then
  BIND_ADDRESS="$(printf '%s\n' "${unit_text}" | sed -n 's/.*--host[[:space:]]\+\([^[:space:]]\+\).*/\1/p' | tail -n 1)"
  BIND_ADDRESS="${BIND_ADDRESS:-0.0.0.0}"
fi
if [[ -z "${PORT}" ]]; then
  PORT="$(printf '%s\n' "${unit_text}" | sed -n 's/.*--port[[:space:]]\+\([0-9][0-9]*\).*/\1/p' | tail -n 1)"
  PORT="${PORT:-8443}"
fi
if [[ -z "${TLS_MODE}" ]]; then
  if printf '%s\n' "${unit_text}" | grep -q -- '--ssl-keyfile'; then TLS_MODE="1"; else TLS_MODE="0"; fi
fi
if [[ -z "${PROXY_HEADERS}" ]]; then
  if printf '%s\n' "${unit_text}" | grep -q -- '--proxy-headers'; then PROXY_HEADERS="1"; else PROXY_HEADERS="0"; fi
fi
[[ "${PORT}" =~ ^[0-9]+$ ]] && (( PORT >= 1024 && PORT <= 65535 )) || die "端口无效：${PORT}"
if [[ "${TLS_MODE}" == "1" ]]; then
  [[ -s "${ETC_DIR}/tls/server.crt" && -s "${ETC_DIR}/tls/server.key" ]] || \
    die "--tls 需要 ${ETC_DIR}/tls/server.crt 和 server.key"
fi

if id "${APP_NAME}" >/dev/null 2>&1; then
  APP_UID="$(id -u "${APP_NAME}")"
  APP_GID="$(id -g "${APP_NAME}")"
else
  APP_UID="$(stat -c '%u' "${DATA_DIR}")"
  APP_GID="$(stat -c '%g' "${DATA_DIR}")"
fi
[[ "${APP_UID}" =~ ^[0-9]+$ && "${APP_GID}" =~ ^[0-9]+$ ]] || die "运行 UID/GID 无效"
[[ "${IMAGE}" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]] || die "镜像名无效：${IMAGE}"
[[ "${BIND_ADDRESS}" =~ ^[A-Za-z0-9:.%-]+$ ]] || die "监听地址无效：${BIND_ADDRESS}"

log "在停机前构建镜像 ${IMAGE}"
docker build --tag "${IMAGE}" --file "${DOCKERFILE}" "${PACKAGE_DIR}"

install -d -m 0750 "${CONTAINER_ROOT}"
install -m 0640 "${COMPOSE_SOURCE}" "${CONTAINER_ROOT}/compose.yaml"
cat >"${CONTAINER_ROOT}/.env" <<EOF
NGINX_MANAGER_IMAGE=${IMAGE}
NGINX_MANAGER_UID=${APP_UID}
NGINX_MANAGER_GID=${APP_GID}
NGINX_MANAGER_BIND_ADDRESS=${BIND_ADDRESS}
NGINX_MANAGER_PORT=${PORT}
NGINX_MANAGER_PROXY_HEADERS=${PROXY_HEADERS}
NGINX_MANAGER_TLS=${TLS_MODE}
EOF
chmod 0600 "${CONTAINER_ROOT}/.env"

if systemctl is-active --quiet "${SERVICE}"; then
  OLD_SERVICE_ACTIVE="1"
  ROLLBACK_REQUIRED="1"
  log "停止原 systemd 服务"
  systemctl stop "${SERVICE}"
  ! systemctl is-active --quiet "${SERVICE}" || {
    echo "错误：原 systemd 服务未能停止" >&2
    false
  }
fi
ROLLBACK_REQUIRED="1"

log "服务已停止，创建数据库与附件一致的迁移前备份"
bash "${SCRIPT_DIR}/backup-server.sh" >/dev/null

python3 - "${db_path}" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect("file:{}?mode=ro".format(sys.argv[1]), uri=True, timeout=10)
try:
    result = connection.execute("PRAGMA quick_check").fetchone()
finally:
    connection.close()
if result is None or result[0] != "ok":
    raise SystemExit("SQLite quick_check failed: {!r}".format(result))
PY

log "启动容器"
compose up -d
for _attempt in $(seq 1 30); do
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${CONTAINER_NAME}" 2>/dev/null || true)"
  [[ "${health}" != "healthy" ]] || break
  sleep 1
done
[[ "${health:-}" == "healthy" ]] || {
  compose logs --tail=100 >&2 || true
  false
}

if systemctl cat "${SERVICE}" >/dev/null 2>&1; then
  systemctl disable "${SERVICE}" >/dev/null
  ! systemctl is-enabled --quiet "${SERVICE}" 2>/dev/null || {
    echo "错误：${SERVICE} 仍处于开机启用状态" >&2
    false
  }
  ! systemctl is-active --quiet "${SERVICE}" || {
    echo "错误：${SERVICE} 在容器启动后又恢复运行，拒绝留下端口冲突" >&2
    false
  }
fi
ROLLBACK_REQUIRED="0"
trap - ERR INT TERM HUP

scheme="http"
[[ "${TLS_MODE}" != "1" ]] || scheme="https"
log "迁移完成：${scheme}://${BIND_ADDRESS}:${PORT}"
echo "Agent 服务器地址未改变，无需重新审批或注册。"
echo "回滚：cd ${CONTAINER_ROOT} && docker compose down && systemctl enable --now ${SERVICE}"
