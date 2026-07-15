#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_NAME="nginx-manager"
APP_USER="nginx-manager"
APP_GROUP="nginx-manager"
APP_ROOT="/opt/nginx-manager"
ETC_DIR="/etc/nginx-manager"
DATA_DIR="/var/lib/nginx-manager"
SERVICE_FILE="/etc/systemd/system/nginx-manager.service"
CREDENTIALS_FILE="/root/nginx-manager-credentials.txt"
BACKUP_DIR="/var/backups/nginx-manager"
PURGE="0"

usage() {
  cat <<'USAGE'
用法：
  sudo ./deploy/uninstall-server.sh [--purge]

默认卸载服务和程序，但保留数据库、配置和管理员凭据，方便重新安装。
--purge 会先在 /var/backups/nginx-manager 创建 root-only 备份，再删除全部数据和账号。
USAGE
}

die() {
  echo "错误：$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || die "请使用 root 或 sudo 运行"
command -v systemctl >/dev/null 2>&1 || die "系统必须使用 systemd"
command -v flock >/dev/null 2>&1 || die "系统缺少 flock"
install -d -m 0755 /run/lock
exec 9>/run/lock/nginx-manager-uninstall.lock
flock -n 9 || die "另一个 Nginx Manager 安装或卸载任务正在运行"

systemctl disable --now "${APP_NAME}.service" >/dev/null 2>&1 || \
  systemctl stop "${APP_NAME}.service" >/dev/null 2>&1 || true

if [[ "${PURGE}" == "1" && ( -e "${ETC_DIR}" || -e "${DATA_DIR}" || -e "${CREDENTIALS_FILE}" ) ]]; then
  command -v tar >/dev/null 2>&1 || die "彻底卸载前需要 tar 创建备份"
  install -d -m 0700 "${BACKUP_DIR}"
  archive="${BACKUP_DIR}/nginx-manager-uninstall-$(date -u +'%Y%m%dT%H%M%SZ').tar.gz"
  items=()
  [[ ! -e "${ETC_DIR}" ]] || items+=("etc/nginx-manager")
  [[ ! -e "${DATA_DIR}" ]] || items+=("var/lib/nginx-manager")
  [[ ! -e "${CREDENTIALS_FILE}" ]] || items+=("root/nginx-manager-credentials.txt")
  tar -C / -czf "${archive}" "${items[@]}"
  chmod 0600 "${archive}"
  echo "卸载前备份：${archive}"
fi

rm -f -- "${SERVICE_FILE}"
systemctl daemon-reload
systemctl reset-failed "${APP_NAME}.service" >/dev/null 2>&1 || true
rm -rf -- "${APP_ROOT}"

if [[ "${PURGE}" == "1" ]]; then
  rm -rf -- "${ETC_DIR}" "${DATA_DIR}"
  rm -f -- "${CREDENTIALS_FILE}"
  id "${APP_USER}" >/dev/null 2>&1 && userdel "${APP_USER}" >/dev/null 2>&1 || true
  getent group "${APP_GROUP}" >/dev/null 2>&1 && groupdel "${APP_GROUP}" >/dev/null 2>&1 || true
  echo "Nginx Manager Server 已彻底卸载。"
else
  echo "Nginx Manager Server 已卸载；以下数据被保留：${ETC_DIR}、${DATA_DIR}、${CREDENTIALS_FILE}"
fi
echo "安装器无法判断防火墙端口是否由其他服务共用，因此不会自动删除 8443/tcp 规则。"
