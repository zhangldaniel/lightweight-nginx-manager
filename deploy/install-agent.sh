#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

APP_NAME="nginx-manager-agent"
APP_USER="nginx-manager-agent"
APP_GROUP="nginx-manager-agent"
APP_DIR="/opt/${APP_NAME}"
ETC_DIR="/etc/${APP_NAME}"
STATE_DIR="/var/lib/${APP_NAME}"
HELPER_STATE_DIR="/var/lib/${APP_NAME}-helper"
CONFIG_FILE="${ETC_DIR}/config.json"
AGENT_SERVICE="/etc/systemd/system/${APP_NAME}.service"
HELPER_SERVICE="/etc/systemd/system/${APP_NAME}-helper.service"
RECOVERY_SERVICE="/etc/systemd/system/${APP_NAME}-recover.service"
NGINX_SERVICE="nginx.service"
NGINX_DROPIN=""

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PACKAGE_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
AGENT_SOURCE="${PACKAGE_DIR}/agent/nginx_agent.py"

SERVER_URL=""
NODE_NAME="$(hostname -s 2>/dev/null || hostname)"
LABELS=""
CA_SOURCE=""
TLS_SKIP_VERIFY="0"
ALLOW_INSECURE_HTTP="0"
NGINX_BINARY=""
NGINX_ROOT="/etc/nginx"
NGINX_CONFIG="/etc/nginx/nginx.conf"
MANAGED_CONFIG_DIR=""
MANAGED_CERT_DIR=""
MANAGED_INCLUDE_FILE=""
HEALTH_URL=""
POLL_SECONDS="3"
INSTALL_NGINX="0"
FORCE_ENROLL="0"
PYTHON_BIN="python3"
INSTALL_TRANSACTION_ACTIVE="0"
INSTALL_BACKUP_DIR=""
OLD_AGENT_ACTIVE="0"
OLD_HELPER_ACTIVE="0"
OLD_AGENT_ENABLED="0"
OLD_HELPER_ENABLED="0"
MANAGED_INCLUDE_CREATED="0"
ENROLLMENT_COMPLETED="0"
PRESERVE_NEW_CONNECTION="0"
PRESERVE_NEW_BINARY="0"

cleanup() {
  local status=$?
  local remove_backup="1"
  if [[ "${status}" -ne 0 && "${INSTALL_TRANSACTION_ACTIVE}" == "1" ]]; then
    if rollback_install; then
      INSTALL_TRANSACTION_ACTIVE="0"
    else
      remove_backup="0"
      printf '%s\n' "automatic rollback was incomplete" >"${INSTALL_BACKUP_DIR}/ROLLBACK_REQUIRED"
      chmod 0600 "${INSTALL_BACKUP_DIR}/ROLLBACK_REQUIRED"
      echo "错误：自动回滚未完整成功，恢复副本保留在 ${INSTALL_BACKUP_DIR}" >&2
    fi
  fi
  if [[ -n "${INSTALL_BACKUP_DIR}" && "${remove_backup}" == "1" ]]; then
    rm -rf -- "${INSTALL_BACKUP_DIR}"
  fi
}
trap cleanup EXIT

usage() {
  cat <<'USAGE'
用法：
  sudo ./deploy/install-agent.sh --server <HTTP(S)地址> [选项]

选项：
  --server <URL>       控制端地址，例如 http://192.0.2.20:8443（必填）
  --node-name <名称>   节点名称，默认当前短主机名
  --labels <键值>      逗号分隔标签，例如 env=prod,region=shanghai
  --ca-file <路径>     自签控制端 CA；公共 CA 证书不需要
  --insecure-skip-tls-verify 不复制 CA，仍使用 HTTPS 但不校验控制端身份（仅可信内网）
  --nginx-binary <路径> Nginx 可执行文件，默认从 PATH 查找
  --nginx-root <路径>  Nginx 配置根；脚本只在其下建立专用托管子目录，默认 /etc/nginx
  --nginx-config <路径> Nginx 主配置，默认 /etc/nginx/nginx.conf
  --managed-config-dir <路径> Agent 专用托管配置目录，默认 <nginx-root>/nginx-manager.d
  --managed-cert-dir <路径> Agent 专用托管证书目录，默认 <nginx-root>/ssl/nginx-manager
  --managed-include-file <路径> 引入托管配置的 include 文件，默认 <nginx-root>/conf.d/00-nginx-manager.conf
  --nginx-service <单元> Nginx systemd 单元，默认 nginx.service
  --health-url <URL>   发布后的节点本地健康检查 URL
  --poll-seconds <秒>  任务轮询周期，默认 3
  --install-nginx      节点未安装 Nginx 时由脚本安装
  --force-enroll       请求管理员批准并替换现有 Agent 身份
  -h, --help           显示帮助

安装后 Agent 会出现在 Web 的“待审批接入”列表；管理员批准后自动上线。
脚本不会在节点开放端口，也不会修改防火墙。
USAGE
}

die() {
  echo "错误：$*" >&2
  exit 1
}

log() {
  echo "[nginx-manager-agent] $*"
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "请使用 root 或 sudo 运行"
}

refuse_unresolved_transactions() {
  local marker
  for marker in /var/tmp/nginx-manager-agent-install.*/ROLLBACK_REQUIRED; do
    [[ -e "${marker}" ]] || continue
    echo "错误：发现未完成的历史 Agent 安装回滚：$(dirname -- "${marker}")" >&2
    echo "请先按该目录内的恢复副本人工修复，确认后再移走该目录。" >&2
    exit 1
  done
}

backup_install_file() {
  local source="$1" name="$2"
  if [[ -e "${source}" || -L "${source}" ]]; then
    cp -a -- "${source}" "${INSTALL_BACKUP_DIR}/${name}"
    : >"${INSTALL_BACKUP_DIR}/${name}.present"
  fi
}

restore_install_file() {
  local target="$1" name="$2" parent_mode="0755"
  if [[ -f "${INSTALL_BACKUP_DIR}/${name}.present" ]]; then
    case "${target}" in
      "${ETC_DIR}"/*) parent_mode="0750" ;;
      "${STATE_DIR}"/*) parent_mode="0700" ;;
    esac
    [[ -d "$(dirname -- "${target}")" ]] || install -d -m "${parent_mode}" "$(dirname -- "${target}")"
    rm -f -- "${target}"
    cp -a -- "${INSTALL_BACKUP_DIR}/${name}" "${target}"
  else
    rm -f -- "${target}"
  fi
}

verify_restored_file() {
  local target="$1" name="$2"
  if [[ -f "${INSTALL_BACKUP_DIR}/${name}.present" ]]; then
    [[ -e "${target}" && ! -L "${target}" ]] && cmp -s -- "${INSTALL_BACKUP_DIR}/${name}" "${target}"
  else
    [[ ! -e "${target}" && ! -L "${target}" ]]
  fi
}

classify_changed_identity() {
  local backup=""
  [[ -f "${INSTALL_BACKUP_DIR}/identity.json.present" ]] && backup="${INSTALL_BACKUP_DIR}/identity.json"
  "${PYTHON_BIN}" - "${STATE_DIR}/identity.json" "${backup}" <<'PY'
import json
import os
import sys

current, backup = sys.argv[1:]
try:
    with open(current, "rb") as handle:
        current_bytes = handle.read()
    value = json.loads(current_bytes.decode("utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
if not isinstance(value, dict):
    raise SystemExit(1)
pending = bool(value.get("enrollment_pending"))
credential = value.get("machine_credential") or value.get("agent_token")
if pending:
    if not value.get("enrollment_id") or not value.get("enrollment_secret"):
        raise SystemExit(1)
elif not value.get("agent_id") or not credential:
    raise SystemExit(1)
if backup:
    try:
        with open(backup, "r", encoding="utf-8") as handle:
            old = json.load(handle)
        if (
            isinstance(old, dict)
            and not value.get("enrollment_pending")
            and not old.get("enrollment_pending")
            and old.get("agent_id") == value.get("agent_id")
            and (old.get("machine_credential") or old.get("agent_token")) == credential
        ):
            raise SystemExit(1)
    except (OSError, ValueError):
        pass
print("pending" if pending else "committed")
PY
}

preserve_pending_install() {
  local failed="0" required
  log "保留待审批接入申请并启动后台轮询"
  for required in \
    "${APP_DIR}/nginx_agent.py" "${CONFIG_FILE}" "${STATE_DIR}/identity.json" \
    "${AGENT_SERVICE}" "${HELPER_SERVICE}" "${RECOVERY_SERVICE}" "${NGINX_DROPIN}"; do
    [[ -f "${required}" && ! -L "${required}" ]] || failed="1"
  done
  systemctl daemon-reload >/dev/null 2>&1 || failed="1"
  systemctl enable "${APP_NAME}-helper.service" "${APP_NAME}.service" >/dev/null 2>&1 || failed="1"
  systemctl restart "${APP_NAME}-helper.service" >/dev/null 2>&1 || failed="1"
  systemctl restart "${APP_NAME}.service" >/dev/null 2>&1 || failed="1"
  systemctl is-enabled --quiet "${APP_NAME}-helper.service" 2>/dev/null || failed="1"
  systemctl is-enabled --quiet "${APP_NAME}.service" 2>/dev/null || failed="1"
  systemctl is-active --quiet "${APP_NAME}-helper.service" || failed="1"
  systemctl is-active --quiet "${APP_NAME}.service" || failed="1"
  if [[ "${failed}" != "0" ]]; then
    echo "错误：pending 身份已保存，但后台重试服务未能可靠启动" >&2
    return 1
  fi
  log "待审批申请已安全保留；Agent 将由 systemd 自动等待 Web 批准"
}

begin_install_transaction() {
  INSTALL_BACKUP_DIR="$(mktemp -d /var/tmp/nginx-manager-agent-install.XXXXXX)"
  chmod 0700 "${INSTALL_BACKUP_DIR}"
  systemctl is-active --quiet "${APP_NAME}.service" && OLD_AGENT_ACTIVE="1" || true
  systemctl is-active --quiet "${APP_NAME}-helper.service" && OLD_HELPER_ACTIVE="1" || true
  systemctl is-enabled --quiet "${APP_NAME}.service" 2>/dev/null && OLD_AGENT_ENABLED="1" || true
  systemctl is-enabled --quiet "${APP_NAME}-helper.service" 2>/dev/null && OLD_HELPER_ENABLED="1" || true
  backup_install_file "${APP_DIR}/nginx_agent.py" agent.py
  backup_install_file "${CONFIG_FILE}" config.json
  backup_install_file "${ETC_DIR}/ca.crt" ca.crt
  backup_install_file "${AGENT_SERVICE}" agent.service
  backup_install_file "${HELPER_SERVICE}" helper.service
  backup_install_file "${RECOVERY_SERVICE}" recovery.service
  backup_install_file "${NGINX_DROPIN}" nginx.dropin
  backup_install_file "${STATE_DIR}/identity.json" identity.json
  INSTALL_TRANSACTION_ACTIVE="1"
}

rollback_install() {
  local failed="0" identity_state=""
  systemctl stop "${APP_NAME}.service" "${APP_NAME}-helper.service" >/dev/null 2>&1 || true
  systemctl is-active --quiet "${APP_NAME}.service" && failed="1"
  systemctl is-active --quiet "${APP_NAME}-helper.service" && failed="1"
  if identity_state="$(classify_changed_identity 2>/dev/null)"; then
    PRESERVE_NEW_CONNECTION="1"
    if [[ "${identity_state}" == "pending" ]]; then
      PRESERVE_NEW_BINARY="1"
      log "控制端暂不可达或申请待审批；保留新 Agent 并转为后台重试"
      preserve_pending_install
      return
    fi
  fi
  log "安装失败，正在恢复上一版本"
  if [[ "${PRESERVE_NEW_BINARY}" != "1" ]]; then
    restore_install_file "${APP_DIR}/nginx_agent.py" agent.py || failed="1"
  fi
  if [[ "${PRESERVE_NEW_CONNECTION}" != "1" ]]; then
    restore_install_file "${CONFIG_FILE}" config.json || failed="1"
    restore_install_file "${ETC_DIR}/ca.crt" ca.crt || failed="1"
  fi
  restore_install_file "${AGENT_SERVICE}" agent.service || failed="1"
  restore_install_file "${HELPER_SERVICE}" helper.service || failed="1"
  restore_install_file "${RECOVERY_SERVICE}" recovery.service || failed="1"
  restore_install_file "${NGINX_DROPIN}" nginx.dropin || failed="1"
  if [[ "${PRESERVE_NEW_CONNECTION}" != "1" && "${ENROLLMENT_COMPLETED}" != "1" ]]; then
    restore_install_file "${STATE_DIR}/identity.json" identity.json || failed="1"
  fi
  if [[ "${MANAGED_INCLUDE_CREATED}" == "1" ]]; then
    rm -f -- "${MANAGED_INCLUDE_FILE}" || failed="1"
    "${NGINX_BINARY}" -t -c "${NGINX_CONFIG}" >/dev/null 2>&1 || failed="1"
  fi
  systemctl daemon-reload >/dev/null 2>&1 || failed="1"

  if [[ "${OLD_HELPER_ENABLED}" == "1" ]]; then
    systemctl enable "${APP_NAME}-helper.service" >/dev/null 2>&1 || failed="1"
    systemctl is-enabled --quiet "${APP_NAME}-helper.service" 2>/dev/null || failed="1"
  else
    systemctl disable "${APP_NAME}-helper.service" >/dev/null 2>&1 || true
    systemctl is-enabled --quiet "${APP_NAME}-helper.service" 2>/dev/null && failed="1"
  fi
  if [[ "${OLD_AGENT_ENABLED}" == "1" ]]; then
    systemctl enable "${APP_NAME}.service" >/dev/null 2>&1 || failed="1"
    systemctl is-enabled --quiet "${APP_NAME}.service" 2>/dev/null || failed="1"
  else
    systemctl disable "${APP_NAME}.service" >/dev/null 2>&1 || true
    systemctl is-enabled --quiet "${APP_NAME}.service" 2>/dev/null && failed="1"
  fi
  if [[ "${OLD_HELPER_ACTIVE}" == "1" ]]; then
    systemctl start "${APP_NAME}-helper.service" >/dev/null 2>&1 || failed="1"
    systemctl is-active --quiet "${APP_NAME}-helper.service" || failed="1"
  else
    systemctl is-active --quiet "${APP_NAME}-helper.service" && failed="1"
  fi
  if [[ "${OLD_AGENT_ACTIVE}" == "1" ]]; then
    systemctl start "${APP_NAME}.service" >/dev/null 2>&1 || failed="1"
    systemctl is-active --quiet "${APP_NAME}.service" || failed="1"
  else
    systemctl is-active --quiet "${APP_NAME}.service" && failed="1"
  fi

  if [[ "${PRESERVE_NEW_BINARY}" != "1" ]]; then
    verify_restored_file "${APP_DIR}/nginx_agent.py" agent.py || failed="1"
  fi
  if [[ "${PRESERVE_NEW_CONNECTION}" != "1" ]]; then
    verify_restored_file "${CONFIG_FILE}" config.json || failed="1"
    verify_restored_file "${ETC_DIR}/ca.crt" ca.crt || failed="1"
  fi
  verify_restored_file "${AGENT_SERVICE}" agent.service || failed="1"
  verify_restored_file "${HELPER_SERVICE}" helper.service || failed="1"
  verify_restored_file "${RECOVERY_SERVICE}" recovery.service || failed="1"
  verify_restored_file "${NGINX_DROPIN}" nginx.dropin || failed="1"
  if [[ "${PRESERVE_NEW_CONNECTION}" != "1" && "${ENROLLMENT_COMPLETED}" != "1" ]]; then
    verify_restored_file "${STATE_DIR}/identity.json" identity.json || failed="1"
  elif [[ ! -s "${STATE_DIR}/identity.json" ]]; then
    failed="1"
  fi
  [[ "${failed}" == "0" ]]
}

detect_package_manager() {
  if command -v apt-get >/dev/null 2>&1; then
    echo apt
  elif command -v dnf >/dev/null 2>&1; then
    echo dnf
  elif command -v yum >/dev/null 2>&1; then
    echo yum
  else
    die "仅支持使用 apt、dnf 或 yum 的 Linux 发行版"
  fi
}

install_base_dependencies() {
  local manager
  manager="$(detect_package_manager)"
  case "${manager}" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -y
      apt-get install -y python3 ca-certificates openssl
      ;;
    dnf)
      dnf install -y python3 ca-certificates openssl
      ;;
    yum)
      yum install -y python3 ca-certificates openssl
      ;;
  esac

  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 6) else 1)' >/dev/null 2>&1; then
    case "${manager}" in
      dnf) dnf install -y python39 >/dev/null 2>&1 || true ;;
      yum) yum install -y python39 >/dev/null 2>&1 || true ;;
    esac
    command -v python3.9 >/dev/null 2>&1 && PYTHON_BIN="python3.9"
  fi
  "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 6) else 1)' || die "需要 Python 3.6 或更高版本"
  command -v systemctl >/dev/null 2>&1 || die "系统必须使用 systemd"
}

install_nginx_if_requested() {
  if [[ -n "${NGINX_BINARY}" ]]; then
    [[ -x "${NGINX_BINARY}" && ! -d "${NGINX_BINARY}" ]] || die "Nginx 二进制不可执行：${NGINX_BINARY}"
    return
  fi
  if command -v nginx >/dev/null 2>&1; then
    NGINX_BINARY="$(command -v nginx)"
    return
  fi
  [[ "${INSTALL_NGINX}" == "1" ]] || die "未发现 Nginx；如需自动安装请添加 --install-nginx"
  local manager
  manager="$(detect_package_manager)"
  case "${manager}" in
    apt) apt-get install -y nginx ;;
    dnf) dnf install -y nginx ;;
    yum) yum install -y nginx ;;
  esac
  systemctl enable --now nginx
  NGINX_BINARY="$(command -v nginx)"
}

prepare_managed_directories() {
  local expected_include created dump temporary include_dir
  [[ -n "${MANAGED_CONFIG_DIR}" ]] || MANAGED_CONFIG_DIR="${NGINX_ROOT}/nginx-manager.d"
  [[ -n "${MANAGED_CERT_DIR}" ]] || MANAGED_CERT_DIR="${NGINX_ROOT}/ssl/nginx-manager"
  [[ -n "${MANAGED_INCLUDE_FILE}" ]] || MANAGED_INCLUDE_FILE="${NGINX_ROOT}/conf.d/00-nginx-manager.conf"
  include_dir="$(dirname -- "${MANAGED_INCLUDE_FILE}")"
  [[ -d "${include_dir}" ]] || die "托管 include 文件的父目录不存在：${include_dir}"

  install -d -m 0750 -o root -g root "${MANAGED_CONFIG_DIR}"
  install -d -m 0700 -o root -g root "${MANAGED_CERT_DIR}"
  expected_include="include ${MANAGED_CONFIG_DIR}/*.conf;"
  created="0"
  if [[ -e "${MANAGED_INCLUDE_FILE}" ]]; then
    [[ -f "${MANAGED_INCLUDE_FILE}" && ! -L "${MANAGED_INCLUDE_FILE}" ]] || die "managed include must be a regular file: ${MANAGED_INCLUDE_FILE}"
    grep -Fqx -- "${expected_include}" "${MANAGED_INCLUDE_FILE}" || die "existing ${MANAGED_INCLUDE_FILE} has unexpected content; inspect it manually"
  else
    temporary="$(mktemp "${include_dir}/.nginx-manager.XXXXXX")"
    printf '%s\n' "${expected_include}" >"${temporary}"
    chmod 0644 "${temporary}"
    mv -f -- "${temporary}" "${MANAGED_INCLUDE_FILE}"
    created="1"
    MANAGED_INCLUDE_CREATED="1"
  fi

  if ! dump="$("${NGINX_BINARY}" -T -c "${NGINX_CONFIG}" 2>&1)"; then
    [[ "${created}" != "1" ]] || rm -f -- "${MANAGED_INCLUDE_FILE}"
    printf '%s\n' "${dump}" >&2
    die "nginx validation failed after adding the managed include"
  fi
  if ! grep -Fq -- "# configuration file ${MANAGED_INCLUDE_FILE}:" <<<"${dump}"; then
    [[ "${created}" != "1" ]] || rm -f -- "${MANAGED_INCLUDE_FILE}"
    die "${MANAGED_INCLUDE_FILE} is not loaded by nginx; verify the conf.d include rule"
  fi
}

validate_server_url() {
  local scheme
  scheme="$("${PYTHON_BIN}" - "${SERVER_URL}" <<'PY'
import sys
from urllib.parse import urlparse
value = urlparse(sys.argv[1])
if value.scheme not in {"http", "https"} or not value.netloc or value.username or value.password or value.query or value.fragment or value.path not in ("", "/"):
    print("错误：控制端必须是无用户名、密码、query 和 fragment 的 HTTP(S) URL", file=sys.stderr)
    raise SystemExit(1)
print(value.scheme)
PY
)" || exit 1
  if [[ "${scheme}" == "http" ]]; then
    [[ "${TLS_SKIP_VERIFY}" != "1" && -z "${CA_SOURCE}" ]] || \
      die "HTTP 控制端不能使用 --ca-file 或 --insecure-skip-tls-verify"
    ALLOW_INSECURE_HTTP="1"
    log "警告：Agent 将通过未加密 HTTP 连接控制端，仅应在隔离且可信的管理网使用"
  fi
}

ensure_identity_user() {
  getent group "${APP_GROUP}" >/dev/null 2>&1 || groupadd --system "${APP_GROUP}"
  if ! id "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --gid "${APP_GROUP}" --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
  fi
}

write_config() {
  local ca_target
  ca_target=""
  if [[ "${ALLOW_INSECURE_HTTP}" == "1" ]]; then
    rm -f -- "${ETC_DIR}/ca.crt"
  elif [[ "${TLS_SKIP_VERIFY}" == "1" ]]; then
    log "警告：已跳过控制端 TLS 身份校验，仅应在可信内网使用"
  elif [[ -n "${CA_SOURCE}" ]]; then
    [[ -f "${CA_SOURCE}" ]] || die "CA 文件不存在"
    ca_target="${ETC_DIR}/ca.crt"
    install -m 0644 -o root -g root "${CA_SOURCE}" "${ca_target}"
  elif [[ -f "${ETC_DIR}/ca.crt" ]]; then
    ca_target="${ETC_DIR}/ca.crt"
  fi

  "${PYTHON_BIN}" - "${CONFIG_FILE}" "${SERVER_URL}" "${NODE_NAME}" "${LABELS}" \
    "${ca_target}" "${TLS_SKIP_VERIFY}" "${ALLOW_INSECURE_HTTP}" "${POLL_SECONDS}" "${NGINX_BINARY}" "$(command -v openssl)" "${NGINX_CONFIG}" "${NGINX_ROOT}" \
    "${MANAGED_CONFIG_DIR}" "${MANAGED_CERT_DIR}" "${STATE_DIR}" "${HELPER_STATE_DIR}" "${HEALTH_URL}" <<'PY'
import json
import os
import socket
import sys
from urllib.parse import urlparse

(
    config_path, server_url, node_name, raw_labels, ca_file,
    tls_skip_verify, allow_insecure_http, poll_seconds, nginx_binary, openssl_binary, nginx_config, nginx_root,
    managed_config_dir, managed_cert_dir, state_dir, helper_state_dir, health_url,
) = sys.argv[1:]

labels = {}
for pair in filter(None, (item.strip() for item in raw_labels.split(","))):
    if "=" not in pair:
        raise SystemExit("标签必须使用 key=value 格式：" + pair)
    key, value = (item.strip() for item in pair.split("=", 1))
    if not key or not value:
        raise SystemExit("标签键和值不能为空")
    labels[key] = value

allowed_health_hosts = ["127.0.0.1", "::1", "localhost"]
health_check = None
if health_url:
    parsed = urlparse(health_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise SystemExit("health URL 必须是无凭据的绝对 HTTP(S) URL")
    if parsed.hostname.lower() not in allowed_health_hosts:
        allowed_health_hosts.append(parsed.hostname.lower())
    health_check = {"url": health_url, "expected_status": 200, "timeout": 5, "attempts": 3}

value = {
    "server_url": server_url.rstrip("/"),
    "node_name": node_name,
    "hostname": socket.gethostname(),
    "labels": labels,
    "ca_file": ca_file or None,
    "tls_skip_verify": tls_skip_verify == "1",
    "allow_insecure_http": allow_insecure_http == "1",
    "poll_interval": float(poll_seconds),
    "heartbeat_interval": 20,
    "api_timeout": 30,
    "command_timeout": 30,
    "nginx_binary": nginx_binary,
    "openssl_binary": openssl_binary,
    "nginx_config": nginx_config,
    "nginx_root": nginx_root,
    "allowed_config_roots": [managed_config_dir],
    "allowed_certificate_roots": [managed_cert_dir],
    "state_dir": state_dir,
    "helper_state_dir": helper_state_dir,
    "helper_socket": "/run/nginx-manager-agent/helper.sock",
    "helper_timeout": 120,
    "helper_max_request_bytes": 8388608,
    "max_file_bytes": 4194304,
    "max_command_output_bytes": 32768,
    "backup_retention": 20,
    "health_check": health_check,
    "allowed_health_hosts": allowed_health_hosts,
}

temporary = config_path + ".tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(value, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(temporary, 0o640)
os.replace(temporary, config_path)
PY
  chown root:"${APP_GROUP}" "${CONFIG_FILE}"
  chmod 0640 "${CONFIG_FILE}"
}

write_services() {
  local python_path systemd_version protect_system write_access_key
  local modern_hardening="" runtime_preserve="" nginx_write_paths=""
  python_path="$(command -v "${PYTHON_BIN}")"
  systemd_version="$(systemctl --version 2>/dev/null | awk 'NR == 1 {print $2}')"
  [[ "${systemd_version}" =~ ^[0-9]+$ ]] || die "无法识别 systemd 版本"
  [[ ! -d /var/log/nginx ]] || nginx_write_paths+=" /var/log/nginx"
  [[ ! -d /var/cache/nginx ]] || nginx_write_paths+=" /var/cache/nginx"
  if (( systemd_version >= 232 )); then
    protect_system="strict"
    write_access_key="ReadWritePaths"
    modern_hardening=$'ProtectKernelTunables=true\nProtectKernelModules=true\nProtectControlGroups=true\nRestrictSUIDSGID=true'
  else
    # CentOS 7 ships systemd 219: it supports ProtectSystem=full and the old
    # ReadWriteDirectories name, but not strict/ReadWritePaths or the newer
    # kernel/control-group hardening directives.
    protect_system="full"
    write_access_key="ReadWriteDirectories"
    log "检测到 systemd ${systemd_version}，使用 CentOS 7 兼容的服务沙箱"
  fi
  if (( systemd_version >= 235 )); then
    runtime_preserve="RuntimeDirectoryPreserve=yes"
  fi
  cat >"${RECOVERY_SERVICE}" <<EOF
[Unit]
Description=Recover interrupted Nginx Manager publications before Nginx starts
After=local-fs.target
Before=${NGINX_SERVICE} ${APP_NAME}-helper.service

[Service]
Type=oneshot
User=root
Group=${APP_GROUP}
ExecStart=${python_path} ${APP_DIR}/nginx_agent.py --config ${CONFIG_FILE} recover
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=${protect_system}
ProtectHome=true
${modern_hardening}
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_FOWNER CAP_CHOWN CAP_KILL
${write_access_key}=${MANAGED_CONFIG_DIR} ${MANAGED_CERT_DIR} ${HELPER_STATE_DIR}${nginx_write_paths}
UMask=0077
EOF

  cat >"${HELPER_SERVICE}" <<EOF
[Unit]
Description=Nginx Manager Agent privileged helper
Requires=${APP_NAME}-recover.service
After=local-fs.target ${APP_NAME}-recover.service
Before=${APP_NAME}.service

[Service]
Type=simple
User=root
Group=${APP_GROUP}
ExecStart=${python_path} ${APP_DIR}/nginx_agent.py --config ${CONFIG_FILE} helper --allowed-uid ${APP_USER} --socket-group ${APP_GROUP}
Restart=on-failure
RestartSec=3s
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=${protect_system}
ProtectHome=true
${modern_hardening}
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_FOWNER CAP_CHOWN CAP_KILL
${write_access_key}=${MANAGED_CONFIG_DIR} ${MANAGED_CERT_DIR} ${HELPER_STATE_DIR} /run/${APP_NAME}${nginx_write_paths}
RuntimeDirectory=${APP_NAME}
RuntimeDirectoryMode=0750
${runtime_preserve}
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

  cat >"${AGENT_SERVICE}" <<EOF
[Unit]
Description=Nginx Manager Agent (unprivileged network client)
After=network-online.target ${APP_NAME}-helper.service
Wants=network-online.target
Requires=${APP_NAME}-helper.service

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
ExecStart=${python_path} ${APP_DIR}/nginx_agent.py --config ${CONFIG_FILE} run
Restart=on-failure
RestartSec=3s
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=${protect_system}
ProtectHome=true
${modern_hardening}
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
${write_access_key}=${STATE_DIR}
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

  install -d -m 0755 -o root -g root "$(dirname -- "${NGINX_DROPIN}")"
  cat >"${NGINX_DROPIN}" <<EOF
[Unit]
Requires=${APP_NAME}-recover.service
After=${APP_NAME}-recover.service
EOF

  systemd-analyze verify "${RECOVERY_SERVICE}" "${HELPER_SERVICE}" "${AGENT_SERVICE}" >/dev/null
  systemctl daemon-reload
}

run_as_agent() {
  command -v runuser >/dev/null 2>&1 || die "系统缺少 runuser（通常由 util-linux 提供）"
  runuser -u "${APP_USER}" -- "$@"
}

enroll_if_needed() {
  if [[ -s "${STATE_DIR}/identity.json" && "${FORCE_ENROLL}" != "1" ]]; then
    log "保留现有 Agent 身份"
    return
  fi
  log "提交节点接入申请；稍后请在 Web 控制台批准"
  if [[ "${FORCE_ENROLL}" == "1" ]]; then
    # Keep the previous identity inside the durable pending document until the
    # administrator approves or rejects the replacement.
    run_as_agent "${PYTHON_BIN}" "${APP_DIR}/nginx_agent.py" --config "${CONFIG_FILE}" enroll --force
  else
    run_as_agent "${PYTHON_BIN}" "${APP_DIR}/nginx_agent.py" --config "${CONFIG_FILE}" enroll
  fi
  ENROLLMENT_COMPLETED="1"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) [[ $# -ge 2 ]] || die "--server 缺少值"; SERVER_URL="$2"; shift 2 ;;
    --node-name) [[ $# -ge 2 ]] || die "--node-name 缺少值"; NODE_NAME="$2"; shift 2 ;;
    --labels) [[ $# -ge 2 ]] || die "--labels 缺少值"; LABELS="$2"; shift 2 ;;
    --ca-file) [[ $# -ge 2 ]] || die "--ca-file 缺少值"; CA_SOURCE="$2"; shift 2 ;;
    --insecure-skip-tls-verify) TLS_SKIP_VERIFY="1"; shift ;;
    --nginx-binary) [[ $# -ge 2 ]] || die "--nginx-binary 缺少值"; NGINX_BINARY="$2"; shift 2 ;;
    --nginx-root) [[ $# -ge 2 ]] || die "--nginx-root 缺少值"; NGINX_ROOT="$2"; shift 2 ;;
    --nginx-config) [[ $# -ge 2 ]] || die "--nginx-config 缺少值"; NGINX_CONFIG="$2"; shift 2 ;;
    --managed-config-dir) [[ $# -ge 2 ]] || die "--managed-config-dir 缺少值"; MANAGED_CONFIG_DIR="$2"; shift 2 ;;
    --managed-cert-dir) [[ $# -ge 2 ]] || die "--managed-cert-dir 缺少值"; MANAGED_CERT_DIR="$2"; shift 2 ;;
    --managed-include-file) [[ $# -ge 2 ]] || die "--managed-include-file 缺少值"; MANAGED_INCLUDE_FILE="$2"; shift 2 ;;
    --nginx-service) [[ $# -ge 2 ]] || die "--nginx-service 缺少值"; NGINX_SERVICE="$2"; shift 2 ;;
    --health-url) [[ $# -ge 2 ]] || die "--health-url 缺少值"; HEALTH_URL="$2"; shift 2 ;;
    --poll-seconds) [[ $# -ge 2 ]] || die "--poll-seconds 缺少值"; POLL_SECONDS="$2"; shift 2 ;;
    --install-nginx) INSTALL_NGINX="1"; shift ;;
    --force-enroll) FORCE_ENROLL="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

require_root
[[ -n "${SERVER_URL}" ]] || { usage; die "必须指定 --server"; }
[[ -n "${NODE_NAME}" && "${NODE_NAME}" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || die "节点名称只允许字母、数字、点、下划线和短横线"
[[ "${TLS_SKIP_VERIFY}" != "1" || -z "${CA_SOURCE}" ]] || die "--ca-file 与 --insecure-skip-tls-verify 不能同时使用"
[[ "${NGINX_ROOT}" = /* && "${NGINX_CONFIG}" = /* ]] || die "Nginx 路径必须是绝对路径"
for optional_path in "${NGINX_BINARY}" "${MANAGED_CONFIG_DIR}" "${MANAGED_CERT_DIR}" "${MANAGED_INCLUDE_FILE}"; do
  [[ -z "${optional_path}" || "${optional_path}" = /* ]] || die "自定义 Nginx 路径必须是绝对路径：${optional_path}"
done
for nginx_path in "${NGINX_ROOT}" "${NGINX_CONFIG}" "${NGINX_BINARY}" "${MANAGED_CONFIG_DIR}" "${MANAGED_CERT_DIR}" "${MANAGED_INCLUDE_FILE}"; do
  [[ ! "${nginx_path}" =~ [[:space:]] ]] || die "Nginx 路径不能包含空白字符：${nginx_path}"
done
[[ "${NGINX_SERVICE}" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || die "--nginx-service 必须是合法的 .service 单元名"
NGINX_DROPIN="/etc/systemd/system/${NGINX_SERVICE}.d/nginx-manager-agent-recovery.conf"

install -d -m 0755 /run/lock
exec 9>/run/lock/nginx-manager-agent-install.lock
flock -n 9 || die "另一个 Agent 安装或升级进程正在运行"
refuse_unresolved_transactions

install_base_dependencies
validate_server_url
install_nginx_if_requested
systemctl cat "${NGINX_SERVICE}" >/dev/null 2>&1 || die "找不到 ${NGINX_SERVICE}；无法建立 Nginx 启动前恢复屏障"
[[ -x "${NGINX_BINARY}" ]] || die "Nginx 二进制不可执行"
[[ -f "${NGINX_CONFIG}" ]] || die "找不到 Nginx 主配置 ${NGINX_CONFIG}"
"${NGINX_BINARY}" -t -c "${NGINX_CONFIG}" || die "现有 Nginx 配置校验失败，Agent 未安装"
[[ -f "${AGENT_SOURCE}" ]] || die "找不到 agent/nginx_agent.py，请从完整发布包内运行"

ensure_identity_user
begin_install_transaction
prepare_managed_directories
systemctl stop "${APP_NAME}.service" "${APP_NAME}-helper.service" >/dev/null 2>&1 || true
install -d -m 0755 -o root -g root "${APP_DIR}"
install -d -m 0750 -o root -g "${APP_GROUP}" "${ETC_DIR}"
install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${STATE_DIR}"
install -d -m 0700 -o root -g root "${HELPER_STATE_DIR}"
install -m 0755 -o root -g root "${AGENT_SOURCE}" "${APP_DIR}/nginx_agent.py"
write_config
run_as_agent "${PYTHON_BIN}" "${APP_DIR}/nginx_agent.py" --config "${CONFIG_FILE}" validate-config
write_services
systemctl enable "${APP_NAME}-helper.service" "${APP_NAME}.service"
enroll_if_needed
systemctl restart "${APP_NAME}-helper.service"
systemctl restart "${APP_NAME}.service"
sleep 2
systemctl is-active --quiet "${APP_NAME}-helper.service" || {
  journalctl -u "${APP_NAME}-helper.service" -n 80 --no-pager >&2 || true
  die "root helper 启动失败"
}
systemctl is-active --quiet "${APP_NAME}.service" || {
  journalctl -u "${APP_NAME}.service" -n 80 --no-pager >&2 || true
  die "Agent 启动失败"
}

INSTALL_TRANSACTION_ACTIVE="0"
log "部署完成：${NODE_NAME} 将主动连接 ${SERVER_URL}"
echo "下一步：登录控制端 Web，在“节点 Agent”中批准 ${NODE_NAME} 的待审批申请。"
echo "服务状态：systemctl status ${APP_NAME} ${APP_NAME}-helper"
echo "服务日志：journalctl -u ${APP_NAME} -f"
