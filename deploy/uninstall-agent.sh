#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_NAME="nginx-manager-agent"
APP_USER="nginx-manager-agent"
APP_GROUP="nginx-manager-agent"
APP_DIR="/opt/nginx-manager-agent"
ETC_DIR="/etc/nginx-manager-agent"
STATE_DIR="/var/lib/nginx-manager-agent"
HELPER_STATE_DIR="/var/lib/nginx-manager-agent-helper"
AGENT_SERVICE="/etc/systemd/system/nginx-manager-agent.service"
HELPER_SERVICE="/etc/systemd/system/nginx-manager-agent-helper.service"
RECOVERY_SERVICE="/etc/systemd/system/nginx-manager-agent-recover.service"
PURGE="0"

usage() {
  cat <<'USAGE'
用法：
  sudo ./deploy/uninstall-agent.sh [--purge]

默认卸载 Agent 服务和程序，但保留连接配置、机器身份及托管的 Nginx 配置/证书。
--purge 额外删除 Agent 配置和机器身份；托管的 Nginx 配置/证书仍保留，避免站点中断。
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
exec 9>/run/lock/nginx-manager-agent-uninstall.lock
flock -n 9 || die "另一个 Nginx Manager Agent 安装或卸载任务正在运行"

systemctl disable --now "${APP_NAME}.service" "${APP_NAME}-helper.service" >/dev/null 2>&1 || true
systemctl disable "${APP_NAME}-recover.service" >/dev/null 2>&1 || true
systemctl stop "${APP_NAME}-recover.service" >/dev/null 2>&1 || true
rm -f -- "${AGENT_SERVICE}" "${HELPER_SERVICE}" "${RECOVERY_SERVICE}"

for dropin in /etc/systemd/system/*.service.d/nginx-manager-agent-recovery.conf; do
  [[ -e "${dropin}" ]] || continue
  rm -f -- "${dropin}"
  rmdir -- "$(dirname -- "${dropin}")" >/dev/null 2>&1 || true
done

systemctl daemon-reload
systemctl reset-failed "${APP_NAME}.service" "${APP_NAME}-helper.service" >/dev/null 2>&1 || true
rm -rf -- "${APP_DIR}" /run/nginx-manager-agent

if [[ "${PURGE}" == "1" ]]; then
  rm -rf -- "${ETC_DIR}" "${STATE_DIR}" "${HELPER_STATE_DIR}"
  id "${APP_USER}" >/dev/null 2>&1 && userdel "${APP_USER}" >/dev/null 2>&1 || true
  getent group "${APP_GROUP}" >/dev/null 2>&1 && groupdel "${APP_GROUP}" >/dev/null 2>&1 || true
  echo "Nginx Manager Agent 已彻底卸载；下次安装需要重新在 Web 页面审批。"
else
  echo "Nginx Manager Agent 已卸载；连接配置和机器身份已保留，方便重新安装。"
fi
echo "为避免中断现有站点，托管的 Nginx 配置、证书和 include 文件没有自动删除。"
