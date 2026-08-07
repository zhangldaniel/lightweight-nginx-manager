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
NODE_PROFILE="nginx"
KEEPALIVED_BINARY=""
KEEPALIVED_CONFIG=""
KEEPALIVED_SERVICE=""
KEEPALIVED_VIP=""
LVS_TOPOLOGY=""
ENABLE_LVS_OBSERVER="0"
ENABLE_LVS_MANAGEMENT="0"
LVS_MANAGED_FILE=""
DEFAULT_KEEPALIVED_CONFIG="/etc/keepalived/keepalived.conf"
DEFAULT_KEEPALIVED_SERVICE="keepalived.service"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PACKAGE_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
AGENT_SOURCE="${PACKAGE_DIR}/agent/nginx_agent.py"
LVS_CONTROL_SOURCE="${PACKAGE_DIR}/agent/lvs_control.py"

SERVER_URL=""
NODE_NAME="$(hostname -s 2>/dev/null || hostname)"
NODE_IP=""
LABELS=""
CA_SOURCE=""
TLS_SKIP_VERIFY="0"
ALLOW_INSECURE_HTTP="0"
NGINX_BINARY=""
NGINX_PREFIX=""
NGINX_ROOT=""
NGINX_CONFIG=""
MANAGED_CONFIG_DIRS=()
MANAGED_STREAM_DIRS=()
MANAGED_CERT_DIR=""
MANAGED_INCLUDE_FILE=""
MANAGED_CONFIG_ALREADY_INCLUDED="0"
MANAGE_STREAM="0"
ALLOW_MAIN_CONFIG_EDIT="0"
HEALTH_URL=""
NGINX_LOG_DIRS=()
STUB_STATUS_URL=""
ALLOW_PLAINTEXT_LOG_STREAM="0"
POLL_SECONDS="3"
INSTALL_NGINX="0"
FORCE_ENROLL="0"
UPGRADE_MODE="0"
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
  sudo ./deploy/install-agent.sh --upgrade

选项：
  --server <URL>       控制端地址，例如 http://192.0.2.20:8443（必填）
  --node-name <名称>   节点名称，默认当前短主机名
  --node-ip <地址>     本机展示 IP；等价于标签 ha_ip=<地址>
  --profile <类型>     节点类型：nginx、lvs 或 hybrid；默认 nginx
  --labels <键值>      逗号分隔标签；多网卡高可用节点可填写 ha_ip=192.0.2.11
  --ca-file <路径>     自签控制端 CA；公共 CA 证书不需要
  --insecure-skip-tls-verify 不复制 CA，仍使用 HTTPS 但不校验控制端身份（仅可信内网）
  --nginx-binary <路径> Nginx 可执行文件，默认从 PATH 查找
  --nginx-prefix <路径> 按 <路径>/sbin、conf、cert、logs 套用常见源码安装布局
  --nginx-root <路径>  Nginx 配置根；脚本只在其下建立专用托管子目录，默认 /etc/nginx
  --nginx-config <路径> Nginx 主配置，默认 /etc/nginx/nginx.conf
  --managed-config-dir <路径> HTTP 配置入口（直属 *.conf）；可重复指定，第一个为默认入口
  --managed-stream-dir <路径> Stream 配置入口（直属 *.stream）；可重复指定，第一个为默认入口
  --managed-cert-dir <路径> Agent 专用托管证书目录，默认 <nginx-root>/ssl/nginx-manager
  --managed-include-file <路径> 引入托管配置的 include 文件，默认 <nginx-root>/conf.d/00-nginx-manager.conf
  --managed-config-already-included 托管目录已由现有 nginx.conf 加载；不创建额外 include 文件
  --manage-stream      配合 --nginx-prefix 管理 conf/conf.d 下的直属 *.stream
  --allow-main-config-edit 允许平台编辑 nginx.conf；默认仅查看且始终禁止删除/迁移
  --nginx-service <单元> Nginx systemd 单元，默认 nginx.service
  --keepalived-binary <路径> Keepalived 可执行文件；自定义安装目录时指定
  --keepalived-config <路径> Keepalived 主配置，默认 /etc/keepalived/keepalived.conf
  --keepalived-service <单元> Keepalived systemd 单元，默认 keepalived.service
  --keepalived-vip <地址> 本节点组的 Keepalived VIP，例如 10.165.0.110
  --enable-lvs-observer 只读观测宿主机 IPVS 表；不执行 ipvsadm，也不修改转发规则
  --enable-lvs-management 允许 Web 使用结构化对象新增、替换或删除 LVS 虚拟服务
  --managed-lvs-file <路径> LVS 专用 Keepalived 片段，默认 <keepalived-dir>/nginx-manager.d/50-lvs-managed.conf
  --health-url <URL>   发布后的节点本地健康检查 URL
  --nginx-log-dir <路径> 允许实时查看的 Nginx 日志目录；可重复指定
  --stub-status-url <URL> 本机 Nginx stub_status 地址，例如 http://127.0.0.1:18080/nginx_status
  --allow-plaintext-log-stream 允许在 HTTP 管理网传输实时日志；不会改变 HTTPS 连接
  --poll-seconds <秒>  任务轮询周期，默认 3
  --install-nginx      节点未安装 Nginx 时由脚本安装
  --force-enroll       请求管理员批准并替换现有 Agent 身份
  --upgrade            仅升级 Agent 程序；保留现有配置、身份和 systemd 设置
  -h, --help           显示帮助

安装后 Agent 会出现在 Web 的“待审批接入”列表；管理员批准后自动上线。
脚本不会在节点开放端口，也不会修改防火墙。
  --lvs-topology <vrrp|standalone> VRRP uses vrrp; a single LVS node without a VIP uses standalone
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

apply_nginx_prefix_defaults() {
  if [[ -z "${NGINX_PREFIX}" ]]; then
    [[ -n "${NGINX_ROOT}" ]] || NGINX_ROOT="/etc/nginx"
    [[ -n "${NGINX_CONFIG}" ]] || NGINX_CONFIG="${NGINX_ROOT}/nginx.conf"
    [[ "${MANAGE_STREAM}" != "1" ]] || die "--manage-stream 需要同时使用 --nginx-prefix"
    return
  fi
  [[ "${NGINX_PREFIX}" = /* && ! "${NGINX_PREFIX}" =~ [[:space:]] ]] || \
    die "--nginx-prefix 必须是不含空白的绝对路径"
  NGINX_PREFIX="${NGINX_PREFIX%/}"
  [[ -n "${NGINX_BINARY}" ]] || NGINX_BINARY="${NGINX_PREFIX}/sbin/nginx"
  [[ -n "${NGINX_ROOT}" ]] || NGINX_ROOT="${NGINX_PREFIX}"
  [[ -n "${NGINX_CONFIG}" ]] || NGINX_CONFIG="${NGINX_PREFIX}/conf/nginx.conf"
  if [[ -z "${MANAGED_CONFIG_DIRS[@]+x}" ]]; then
    MANAGED_CONFIG_DIRS+=("${NGINX_PREFIX}/conf/conf.d")
  fi
  if [[ "${MANAGE_STREAM}" == "1" && -z "${MANAGED_STREAM_DIRS[@]+x}" ]]; then
    MANAGED_STREAM_DIRS+=("${NGINX_PREFIX}/conf/conf.d")
  fi
  if [[ -z "${MANAGED_CERT_DIR}" ]]; then
    if [[ -d "${NGINX_PREFIX}/cert" ]]; then
      MANAGED_CERT_DIR="${NGINX_PREFIX}/cert"
    elif [[ -d "${NGINX_PREFIX}/certs" ]]; then
      MANAGED_CERT_DIR="${NGINX_PREFIX}/certs"
    else
      MANAGED_CERT_DIR="${NGINX_PREFIX}/cert"
    fi
  fi
  if [[ -z "${NGINX_LOG_DIRS[@]+x}" && -d "${NGINX_PREFIX}/logs" ]]; then
    NGINX_LOG_DIRS+=("${NGINX_PREFIX}/logs")
  fi
  MANAGED_CONFIG_ALREADY_INCLUDED="1"
}

apply_node_ip_label() {
  [[ -n "${NODE_IP}" ]] || return
  "${PYTHON_BIN}" - "${NODE_IP}" <<'PY'
import ipaddress
import sys
try:
    ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit("错误：--node-ip 必须是 IP 地址")
PY
  [[ ! "${LABELS}" =~ (^|,)ha_ip= ]] || die "--node-ip 不能与 --labels 中的 ha_ip 同时使用"
  LABELS="${LABELS:+${LABELS},}ha_ip=${NODE_IP}"
}

validate_existing_identity_binding() {
  local existing_node
  [[ -s "${STATE_DIR}/identity.json" && -f "${CONFIG_FILE}" ]] || return 0
  existing_node="$("${PYTHON_BIN}" - "${CONFIG_FILE}" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        value = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(1)
node_name = value.get("node_name") if isinstance(value, dict) else None
if not isinstance(node_name, str):
    raise SystemExit(1)
print(node_name)
PY
)" || die "无法读取现有 Agent 节点名称；请先检查 ${CONFIG_FILE}"
  if [[ "${existing_node}" != "${NODE_NAME}" && "${FORCE_ENROLL}" != "1" ]]; then
    die "现有 Agent 身份属于 ${existing_node}，不能静默改为 ${NODE_NAME}；确认更名或纠正重名时请添加 --force-enroll，并在 Web 重新批准"
  fi
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
  [[ -n "${source}" ]] || return 0
  if [[ -e "${source}" || -L "${source}" ]]; then
    cp -a -- "${source}" "${INSTALL_BACKUP_DIR}/${name}"
    : >"${INSTALL_BACKUP_DIR}/${name}.present"
  fi
}

restore_install_file() {
  local target="$1" name="$2" parent_mode="0755"
  [[ -n "${target}" ]] || return 0
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
  [[ -n "${target}" ]] || return 0
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
  local -a required_files=(
    "${APP_DIR}/nginx_agent.py" "${APP_DIR}/lvs_control.py" "${CONFIG_FILE}" "${STATE_DIR}/identity.json"
    "${AGENT_SERVICE}" "${HELPER_SERVICE}" "${RECOVERY_SERVICE}"
  )
  [[ -z "${NGINX_DROPIN}" ]] || required_files+=("${NGINX_DROPIN}")
  log "保留待审批接入申请并启动后台轮询"
  for required in "${required_files[@]}"; do
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
  backup_install_file "${APP_DIR}/lvs_control.py" lvs_control.py
  backup_install_file "${CONFIG_FILE}" config.json
  backup_install_file "${ETC_DIR}/ca.crt" ca.crt
  backup_install_file "${AGENT_SERVICE}" agent.service
  backup_install_file "${HELPER_SERVICE}" helper.service
  backup_install_file "${RECOVERY_SERVICE}" recovery.service
  backup_install_file "${NGINX_DROPIN}" nginx.dropin
  backup_install_file "${STATE_DIR}/identity.json" identity.json
  if [[ "${ENABLE_LVS_MANAGEMENT}" == "1" ]]; then
    backup_install_file "${KEEPALIVED_CONFIG}" keepalived.conf
    backup_install_file "${LVS_MANAGED_FILE}" lvs-managed.conf
  fi
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
    restore_install_file "${APP_DIR}/lvs_control.py" lvs_control.py || failed="1"
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
  if [[ "${ENABLE_LVS_MANAGEMENT}" == "1" ]]; then
    restore_install_file "${KEEPALIVED_CONFIG}" keepalived.conf || failed="1"
    restore_install_file "${LVS_MANAGED_FILE}" lvs-managed.conf || failed="1"
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
    verify_restored_file "${APP_DIR}/lvs_control.py" lvs_control.py || failed="1"
  fi
  if [[ "${PRESERVE_NEW_CONNECTION}" != "1" ]]; then
    verify_restored_file "${CONFIG_FILE}" config.json || failed="1"
    verify_restored_file "${ETC_DIR}/ca.crt" ca.crt || failed="1"
  fi
  verify_restored_file "${AGENT_SERVICE}" agent.service || failed="1"
  verify_restored_file "${HELPER_SERVICE}" helper.service || failed="1"
  verify_restored_file "${RECOVERY_SERVICE}" recovery.service || failed="1"
  verify_restored_file "${NGINX_DROPIN}" nginx.dropin || failed="1"
  if [[ "${ENABLE_LVS_MANAGEMENT}" == "1" ]]; then
    verify_restored_file "${KEEPALIVED_CONFIG}" keepalived.conf || failed="1"
    verify_restored_file "${LVS_MANAGED_FILE}" lvs-managed.conf || failed="1"
  fi
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
  local created dump temporary include_dir probe directory suffix context expected_file
  local resolved_root resolved_cert resolved_config resolved_main
  local -a all_managed_dirs=()
  if [[ -z "${MANAGED_CONFIG_DIRS[@]+x}" && -z "${MANAGED_STREAM_DIRS[@]+x}" ]]; then
    MANAGED_CONFIG_DIRS+=("${NGINX_ROOT}/nginx-manager.d")
  fi
  [[ -n "${MANAGED_CERT_DIR}" ]] || MANAGED_CERT_DIR="${NGINX_ROOT}/ssl/nginx-manager"
  # Bash 4.2 treats an empty array as unset under `set -u`.  The `+` form
  # expands to no arguments when the array is empty, while preserving every
  # element when it has values.
  all_managed_dirs=(
    "${MANAGED_CONFIG_DIRS[@]+"${MANAGED_CONFIG_DIRS[@]}"}"
    "${MANAGED_STREAM_DIRS[@]+"${MANAGED_STREAM_DIRS[@]}"}"
  )
  resolved_root="$(readlink -m -- "${NGINX_ROOT}")"
  resolved_cert="$(readlink -m -- "${MANAGED_CERT_DIR}")"
  [[ "${resolved_cert}" == "${resolved_root}/"* ]] || \
    die "托管证书目录必须位于 nginx-root 的严格子目录中：${MANAGED_CERT_DIR}"
  resolved_main="$(readlink -m -- "${NGINX_CONFIG}")"
  for directory in "${all_managed_dirs[@]+"${all_managed_dirs[@]}"}"; do
    resolved_config="$(readlink -m -- "${directory}")"
    [[ "${resolved_config}" == "${resolved_root}/"* ]] || \
      die "托管配置入口必须位于 nginx-root 的严格子目录中：${directory}"
    [[ "${resolved_main}" != "${resolved_config}" && "${resolved_main}" != "${resolved_config}/"* ]] || \
      die "nginx 主配置不能位于托管配置入口中：${directory}"
    [[
      "${resolved_config}" != "${resolved_cert}"
      && "${resolved_config}" != "${resolved_cert}/"*
      && "${resolved_cert}" != "${resolved_config}/"*
    ]] || die "托管配置入口与证书目录不能重叠：${directory}"
  done
  for directory in "${all_managed_dirs[@]+"${all_managed_dirs[@]}"}"; do
    if [[ -e "${directory}" ]]; then
      [[ -d "${directory}" && ! -L "${directory}" ]] || \
        die "托管配置路径必须是普通目录：${directory}"
    else
      install -d -m 0750 -o root -g root "${directory}"
    fi
  done
  if [[ -e "${MANAGED_CERT_DIR}" ]]; then
    [[ -d "${MANAGED_CERT_DIR}" && ! -L "${MANAGED_CERT_DIR}" ]] || \
      die "托管证书路径必须是普通目录：${MANAGED_CERT_DIR}"
  else
    install -d -m 0700 -o root -g root "${MANAGED_CERT_DIR}"
  fi

  # The privileged helper validates paths before using them.  Root-owned,
  # non-writable directory chains prevent a local nginx/Agent account from
  # swapping an allowed parent for a symlink between validation and replace.
  for directory in "${all_managed_dirs[@]+"${all_managed_dirs[@]}"}"; do
    harden_managed_path_chain "${directory}"
  done
  harden_managed_path_chain "${MANAGED_CERT_DIR}"

  if [[ "${MANAGED_CONFIG_ALREADY_INCLUDED}" == "1" ]]; then
    [[ -z "${MANAGED_INCLUDE_FILE}" ]] || die "--managed-config-already-included 不能与 --managed-include-file 同时使用"
    for context in http stream; do
      if [[ "${context}" == "http" ]]; then
        suffix=".conf"
        all_managed_dirs=("${MANAGED_CONFIG_DIRS[@]+"${MANAGED_CONFIG_DIRS[@]}"}")
      else
        suffix=".stream"
        all_managed_dirs=("${MANAGED_STREAM_DIRS[@]+"${MANAGED_STREAM_DIRS[@]}"}")
      fi
      for directory in "${all_managed_dirs[@]+"${all_managed_dirs[@]}"}"; do
        probe="$(mktemp "${directory}/zz-nginx-manager-probe.XXXXXX${suffix}")"
        printf '%s\n' '# nginx-manager managed directory probe' >"${probe}"
        chmod 0644 "${probe}"
        if ! dump="$("${NGINX_BINARY}" -T -c "${NGINX_CONFIG}" 2>&1)"; then
          rm -f -- "${probe}"
          printf '%s\n' "${dump}" >&2
          die "nginx validation failed while checking ${context} entry ${directory}"
        fi
        rm -f -- "${probe}"
        if ! grep -Fq -- "# configuration file ${probe}:" <<<"${dump}"; then
          die "${context} entry ${directory} (*${suffix}) is not loaded by nginx"
        fi
        log "已确认 Nginx 加载 ${context} 入口 ${directory}/*${suffix}"
      done
    done
    "${NGINX_BINARY}" -t -c "${NGINX_CONFIG}" >/dev/null 2>&1 || \
      die "nginx validation failed after removing the managed-directory probe"
    return
  fi

  [[ -z "${MANAGED_STREAM_DIRS[@]+x}" ]] || \
    die "Stream 入口不会由安装器自动改写 nginx.conf；请先配置 stream include，并添加 --managed-config-already-included"
  [[ -n "${MANAGED_INCLUDE_FILE}" ]] || MANAGED_INCLUDE_FILE="${NGINX_ROOT}/conf.d/00-nginx-manager.conf"
  include_dir="$(dirname -- "${MANAGED_INCLUDE_FILE}")"
  [[ -d "${include_dir}" ]] || die "托管 include 文件的父目录不存在：${include_dir}"
  for directory in "${MANAGED_CONFIG_DIRS[@]+"${MANAGED_CONFIG_DIRS[@]}"}"; do
    [[ "$(readlink -f -- "${directory}")" != "$(readlink -f -- "${include_dir}")" ]] || \
      die "HTTP 入口已是 include 文件所在目录；请移除 --managed-include-file 并添加 --managed-config-already-included"
  done
  expected_file="$(mktemp)"
  for directory in "${MANAGED_CONFIG_DIRS[@]+"${MANAGED_CONFIG_DIRS[@]}"}"; do
    printf 'include %s/*.conf;\n' "${directory}" >>"${expected_file}"
  done
  created="0"
  if [[ -e "${MANAGED_INCLUDE_FILE}" ]]; then
    [[ -f "${MANAGED_INCLUDE_FILE}" && ! -L "${MANAGED_INCLUDE_FILE}" ]] || die "managed include must be a regular file: ${MANAGED_INCLUDE_FILE}"
    cmp -s -- "${expected_file}" "${MANAGED_INCLUDE_FILE}" || {
      rm -f -- "${expected_file}"
      die "existing ${MANAGED_INCLUDE_FILE} has unexpected content; inspect it manually"
    }
  else
    temporary="$(mktemp "${include_dir}/.nginx-manager.XXXXXX")"
    cp -- "${expected_file}" "${temporary}"
    chmod 0644 "${temporary}"
    mv -f -- "${temporary}" "${MANAGED_INCLUDE_FILE}"
    created="1"
    MANAGED_INCLUDE_CREATED="1"
  fi

  if ! dump="$("${NGINX_BINARY}" -T -c "${NGINX_CONFIG}" 2>&1)"; then
    rm -f -- "${expected_file}"
    [[ "${created}" != "1" ]] || rm -f -- "${MANAGED_INCLUDE_FILE}"
    printf '%s\n' "${dump}" >&2
    die "nginx validation failed after adding the managed include"
  fi
  if ! grep -Fq -- "# configuration file ${MANAGED_INCLUDE_FILE}:" <<<"${dump}"; then
    rm -f -- "${expected_file}"
    [[ "${created}" != "1" ]] || rm -f -- "${MANAGED_INCLUDE_FILE}"
    die "${MANAGED_INCLUDE_FILE} is not loaded by nginx; verify the conf.d include rule"
  fi
  rm -f -- "${expected_file}"
}

harden_managed_path_chain() {
  local target current resolved_root owner mode
  target="$1"
  resolved_root="$(readlink -f -- "${NGINX_ROOT}")"
  current="$(readlink -f -- "${target}")"
  while [[ "${current}" == "${resolved_root}" || "${current}" == "${resolved_root}/"* ]]; do
    [[ -d "${current}" && ! -L "${current}" ]] || die "托管路径链包含非目录或符号链接：${current}"
    owner="$(stat -c '%u' -- "${current}")"
    mode="$(stat -c '%a' -- "${current}")"
    if [[ "${owner}" != "0" ]]; then
      log "加固托管路径所有者：${current}（原 UID ${owner}）"
      chown root:root -- "${current}"
    fi
    chmod go-w -- "${current}"
    [[ "${current}" != "${resolved_root}" ]] || break
    current="$(dirname -- "${current}")"
  done
  current="$(dirname -- "${resolved_root}")"
  while [[ "${current}" != "/" ]]; do
    owner="$(stat -c '%u' -- "${current}")"
    mode="$(stat -c '%a' -- "${current}")"
    [[ "${owner}" == "0" ]] || die "Nginx 根目录的上级路径不是 root 所有：${current}"
    (( (8#${mode} & 0022) == 0 )) || die "Nginx 根目录的上级路径可被组或其他用户写入：${current}"
    current="$(dirname -- "${current}")"
  done
}

recover_existing_transactions() {
  if [[ ! -x "${APP_DIR}/nginx_agent.py" || ! -f "${CONFIG_FILE}" ]]; then
    return
  fi
  log "安装前检查并恢复未完成的发布事务"
  if ! "${PYTHON_BIN}" "${APP_DIR}/nginx_agent.py" --config "${CONFIG_FILE}" recover; then
    echo "错误：现有 Agent 存在无法自动恢复的发布事务；安装尚未修改任何文件" >&2
    echo "请先执行：${PYTHON_BIN} ${APP_DIR}/nginx_agent.py --config ${CONFIG_FILE} recover" >&2
    return 1
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

prepare_monitoring_options() {
  local candidate parsed_status
  if [[ -z "${NGINX_LOG_DIRS[@]+x}" ]]; then
    for candidate in "${NGINX_ROOT}/logs" /var/log/nginx; do
      if [[ -d "${candidate}" && ! -L "${candidate}" ]]; then
        NGINX_LOG_DIRS+=("${candidate}")
      fi
    done
  fi
  for candidate in "${NGINX_LOG_DIRS[@]+"${NGINX_LOG_DIRS[@]}"}"; do
    [[ "${candidate}" = /* ]] || die "--nginx-log-dir 必须是绝对路径：${candidate}"
    [[ ! "${candidate}" =~ [[:space:]] ]] || die "日志目录不能包含空白字符：${candidate}"
    [[ -d "${candidate}" && ! -L "${candidate}" ]] || die "日志目录必须是现有普通目录：${candidate}"
  done
  if [[ -n "${STUB_STATUS_URL}" ]]; then
    "${PYTHON_BIN}" - "${STUB_STATUS_URL}" <<'PY'
import sys
from urllib.parse import urlparse
value = urlparse(sys.argv[1])
if value.scheme not in ("http", "https") or value.hostname not in ("127.0.0.1", "::1", "localhost"):
    raise SystemExit("错误：--stub-status-url 必须使用本机回环地址")
if value.username or value.password or value.fragment:
    raise SystemExit("错误：--stub-status-url 不能包含凭据或 fragment")
PY
    if command -v curl >/dev/null 2>&1 && parsed_status="$(curl -fsS --max-time 5 "${STUB_STATUS_URL}" 2>/dev/null)"; then
      if grep -Eq 'Active connections:[[:space:]]*[0-9]+' <<<"${parsed_status}"; then
        log "Stub Status 校验通过：${STUB_STATUS_URL}"
      else
        log "警告：Stub Status 返回格式不正确；Agent 将继续自动重试"
      fi
    else
      log "警告：暂时无法访问 Stub Status；不阻断安装，Agent 将继续自动重试"
    fi
  fi
  if [[ -z "${NGINX_LOG_DIRS[@]+x}" ]]; then
    log "警告：未探测到日志目录；可重新安装并添加 --nginx-log-dir"
  else
    log "实时日志白名单：${NGINX_LOG_DIRS[*]}"
  fi
}

prepare_keepalived_options() {
  if [[ -z "${KEEPALIVED_BINARY}${KEEPALIVED_CONFIG}${KEEPALIVED_SERVICE}${KEEPALIVED_VIP}${LVS_TOPOLOGY:-}" ]]; then
    [[ "${NODE_PROFILE:-nginx}" != "lvs" ]] || die "--profile lvs requires --lvs-topology vrrp or standalone"
    return 0
  fi
  if [[ -z "${LVS_TOPOLOGY:-}" && -n "${KEEPALIVED_VIP}" ]]; then
    LVS_TOPOLOGY="vrrp"
  fi
  [[ "${LVS_TOPOLOGY:-}" =~ ^(vrrp|standalone)$ ]] || \
    die "--lvs-topology must be vrrp or standalone"
  if [[ -n "${KEEPALIVED_VIP}" ]]; then
    [[ -n "${KEEPALIVED_CONFIG}" ]] || KEEPALIVED_CONFIG="${DEFAULT_KEEPALIVED_CONFIG}"
    [[ -n "${KEEPALIVED_SERVICE}" ]] || KEEPALIVED_SERVICE="${DEFAULT_KEEPALIVED_SERVICE}"
  fi
  if [[ "${LVS_TOPOLOGY}" == "standalone" ]]; then
    [[ "${NODE_PROFILE:-nginx}" == "lvs" ]] || die "standalone LVS requires --profile lvs"
    [[ -z "${KEEPALIVED_VIP}" ]] || die "standalone LVS must not use --keepalived-vip"
    [[ -n "${KEEPALIVED_CONFIG}" ]] || KEEPALIVED_CONFIG="${DEFAULT_KEEPALIVED_CONFIG}"
    [[ -n "${KEEPALIVED_SERVICE}" ]] || KEEPALIVED_SERVICE="${DEFAULT_KEEPALIVED_SERVICE}"
  else
    [[ -n "${KEEPALIVED_VIP}" ]] || die "VRRP LVS requires --keepalived-vip"
  fi
  [[ -n "${KEEPALIVED_CONFIG}" && -n "${KEEPALIVED_SERVICE}" ]] || \
    die "--keepalived-config and --keepalived-service must be configured together"
  [[ "${KEEPALIVED_CONFIG}" = /* && ! "${KEEPALIVED_CONFIG}" =~ [[:space:]] ]] || \
    die "--keepalived-config 必须是不含空白的绝对路径"
  [[ -f "${KEEPALIVED_CONFIG}" && ! -L "${KEEPALIVED_CONFIG}" ]] || \
    die "--keepalived-config 必须是现有普通文件"
  [[ "${KEEPALIVED_SERVICE}" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || \
    die "--keepalived-service 必须是合法的 .service 单元名"
  "${PYTHON_BIN}" - "${KEEPALIVED_CONFIG}" "${KEEPALIVED_VIP}" "${LVS_TOPOLOGY}" <<'PY'
import glob
import ipaddress
import os
import re
import sys

config_path, expected, topology = sys.argv[1:]
try:
    expected_ip = ipaddress.ip_address(expected) if topology == "vrrp" else None
except ValueError:
    raise SystemExit("--keepalived-vip must be an IP address")
config_path = os.path.realpath(config_path)
config_root = os.path.dirname(config_path)
include_directives = {"include", "includer", "includem", "includew", "includeb", "includea"}
max_include_depth = 16
max_config_files = 256
max_config_bytes = 8 * 1024 * 1024
state = {"files": 0, "bytes": 0}
active = set()


def inside_config_root(path):
    try:
        return os.path.normcase(os.path.commonpath((config_root, path))) == os.path.normcase(config_root)
    except ValueError:
        return False


def expanded_tokens(path, depth):
    if depth > max_include_depth:
        raise SystemExit("错误：Keepalived include 嵌套超过 {} 层".format(max_include_depth))
    real_path = os.path.realpath(path)
    if not inside_config_root(real_path):
        raise SystemExit("错误：Keepalived include 超出主配置目录：{}".format(path))
    if os.path.islink(path) or not os.path.isfile(path):
        raise SystemExit("错误：Keepalived include 必须是普通文件且不能是符号链接：{}".format(path))
    if real_path in active:
        raise SystemExit("错误：Keepalived include 存在循环：{}".format(path))
    state["files"] += 1
    if state["files"] > max_config_files:
        raise SystemExit("错误：Keepalived include 文件超过 {} 个".format(max_config_files))
    remaining = max_config_bytes - state["bytes"]
    if remaining < 0:
        raise SystemExit("错误：Keepalived 配置总大小超过 {} 字节".format(max_config_bytes))
    with open(real_path, "rb") as handle:
        data = handle.read(remaining + 1)
    state["bytes"] += len(data)
    if state["bytes"] > max_config_bytes:
        raise SystemExit("错误：Keepalived 配置总大小超过 {} 字节".format(max_config_bytes))
    text = data.decode("utf-8", errors="replace")
    lines = [re.split(r"[#\!]", raw_line, maxsplit=1)[0] for raw_line in text.splitlines()]
    source_tokens = re.findall(r"\{|\}|[^\s{}]+", "\n".join(lines))
    tokens = []
    active.add(real_path)
    try:
        index = 0
        while index < len(source_tokens):
            token = source_tokens[index]
            if token.lower() not in include_directives:
                tokens.append(token)
                index += 1
                continue
            if index + 1 >= len(source_tokens):
                raise SystemExit("错误：Keepalived include 缺少文件名：{}".format(path))
            include_name = source_tokens[index + 1].strip("'\"")
            if not include_name or "\x00" in include_name or len(include_name) > 4096:
                raise SystemExit("错误：Keepalived include 文件名无效：{}".format(path))
            include_pattern = include_name
            if not os.path.isabs(include_pattern):
                include_pattern = os.path.join(os.path.dirname(real_path), include_pattern)
            include_pattern = os.path.abspath(os.path.normpath(include_pattern))
            if not inside_config_root(include_pattern):
                raise SystemExit("错误：Keepalived include 超出主配置目录：{}".format(include_name))
            matches = []
            for matched in glob.iglob(include_pattern):
                matches.append(matched)
                if len(matches) > max_config_files - state["files"]:
                    raise SystemExit("错误：Keepalived include 文件超过 {} 个".format(max_config_files))
            for matched in sorted(matches):
                tokens.extend(expanded_tokens(matched, depth + 1))
            index += 2
    finally:
        active.remove(real_path)
    return tokens


tokens = expanded_tokens(config_path, 0)
has_vrrp = any(token.lower() == "vrrp_instance" for token in tokens)
if topology == "standalone":
    if has_vrrp:
        raise SystemExit("standalone LVS configuration must not contain vrrp_instance")
    raise SystemExit(0)
depth = 0
pending_virtual_ipaddress = False
virtual_depth = None
found = False
for token in tokens:
    if token == "virtual_ipaddress":
        pending_virtual_ipaddress = True
        continue
    if token == "{":
        depth += 1
        if pending_virtual_ipaddress:
            virtual_depth = depth
            pending_virtual_ipaddress = False
        continue
    if token == "}":
        if virtual_depth == depth:
            virtual_depth = None
        depth = max(0, depth - 1)
        pending_virtual_ipaddress = False
        continue
    if virtual_depth is None:
        pending_virtual_ipaddress = False
        continue
    candidate = token.split("/", 1)[0]
    try:
        if ipaddress.ip_address(candidate) == expected_ip:
            found = True
            break
    except ValueError:
        pass
if not found:
    raise SystemExit("错误：--keepalived-vip {} 未出现在 {} 的 virtual_ipaddress 中".format(expected, config_path))
PY
  if [[ -z "${KEEPALIVED_BINARY}" ]]; then
    KEEPALIVED_BINARY="$(command -v keepalived || true)"
    if [[ -z "${KEEPALIVED_BINARY}" ]]; then
      for candidate in /usr/sbin/keepalived /sbin/keepalived /apps/keepalived/sbin/keepalived; do
        if [[ -x "${candidate}" && -f "${candidate}" ]]; then
          KEEPALIVED_BINARY="${candidate}"
          break
        fi
      done
    fi
  fi
  [[ "${KEEPALIVED_BINARY}" = /* && ! "${KEEPALIVED_BINARY}" =~ [[:space:]] ]] || \
    die "--keepalived-binary 必须是不含空白的绝对路径"
  [[ -x "${KEEPALIVED_BINARY}" && -f "${KEEPALIVED_BINARY}" ]] || \
    die "Keepalived 可执行文件不可用；自定义安装请使用 --keepalived-binary"
  systemctl cat "${KEEPALIVED_SERVICE}" >/dev/null 2>&1 || \
    die "找不到 Keepalived 单元 ${KEEPALIVED_SERVICE}"
}

prepare_lvs_management() {
  [[ "${ENABLE_LVS_MANAGEMENT}" == "1" ]] || return 0
  [[ -n "${KEEPALIVED_CONFIG}" && -n "${KEEPALIVED_SERVICE}" ]] || \
    die "--enable-lvs-management requires Keepalived integration"
  [[ "${NODE_PROFILE}" == "lvs" || "${NODE_PROFILE}" == "hybrid" ]] || \
    die "--enable-lvs-management requires --profile lvs or hybrid"
  [[ -n "${LVS_MANAGED_FILE}" ]] || \
    LVS_MANAGED_FILE="$(dirname -- "${KEEPALIVED_CONFIG}")/nginx-manager.d/50-lvs-managed.conf"
  [[ "${LVS_MANAGED_FILE}" = /* && ! "${LVS_MANAGED_FILE}" =~ [[:space:]] ]] || \
    die "--managed-lvs-file must be an absolute path without whitespace"

  "${PYTHON_BIN}" - "${KEEPALIVED_CONFIG}" "${LVS_MANAGED_FILE}" <<'PY'
import fnmatch
import glob
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

main = Path(sys.argv[1]).resolve()
managed = Path(sys.argv[2]).resolve()
root = main.parent
try:
    managed.relative_to(root)
except ValueError:
    raise SystemExit("managed LVS file must remain below the Keepalived configuration directory")
if managed == main:
    raise SystemExit("managed LVS file must differ from the Keepalived main configuration")

def include_patterns(path):
    text = path.read_text(encoding="utf-8", errors="strict")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].split("!", 1)[0].strip().rstrip(";")
        match = re.match(r"^(?:include|includer|includem|includew|includeb|includea)\s+([^\s]+)$", line, re.I)
        if match:
            value = match.group(1).strip("\"'")
            yield value if os.path.isabs(value) else str((path.parent / value).resolve())

seen = set()
covered = False
queue = [main]
def path_matches(path, pattern):
    path_parts = os.path.normpath(path).split(os.sep)
    pattern_parts = os.path.normpath(pattern).split(os.sep)
    return len(path_parts) == len(pattern_parts) and all(
        fnmatch.fnmatchcase(value, expected)
        for value, expected in zip(path_parts, pattern_parts)
    )

while queue:
    current = queue.pop()
    if current in seen:
        continue
    seen.add(current)
    if current.is_symlink() or not current.is_file():
        raise SystemExit("Keepalived include graph contains an unsafe path")
    try:
        current.relative_to(root)
    except ValueError:
        raise SystemExit("Keepalived include leaves the configured directory")
    for pattern in include_patterns(current):
        try:
            Path(os.path.abspath(pattern)).relative_to(root)
        except ValueError:
            raise SystemExit("Keepalived include leaves the configured directory")
        if path_matches(str(managed), pattern):
            covered = True
        for name in sorted(glob.glob(pattern)):
            candidate = Path(name).resolve()
            if candidate != managed:
                queue.append(candidate)

if not managed.parent.exists():
    managed.parent.mkdir(mode=0o750, parents=True)
if not managed.exists():
    descriptor = os.open(str(managed), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
elif managed.is_symlink() or not managed.is_file():
    raise SystemExit("managed LVS path must be a regular file")

if not covered:
    status = main.stat()
    data = main.read_bytes()
    addition = (b"" if data.endswith(b"\n") else b"\n") + ("include {}\n".format(managed)).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + main.name + ".", dir=str(main.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data + addition)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, stat.S_IMODE(status.st_mode))
        if hasattr(os, "chown"):
            os.chown(temporary_name, status.st_uid, status.st_gid)
        os.replace(temporary_name, str(main))
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
PY

  local validation_flag=""
  if "${KEEPALIVED_BINARY}" --help 2>&1 | grep -q -- '--config-test'; then
    validation_flag="--config-test"
  elif "${KEEPALIVED_BINARY}" --help 2>&1 | grep -Eq -- '(^|[[:space:],])-t([[:space:],]|$)'; then
    validation_flag="-t"
  else
    die "configured Keepalived does not support safe configuration validation"
  fi
  "${KEEPALIVED_BINARY}" -f "${KEEPALIVED_CONFIG}" "${validation_flag}" >/dev/null 2>&1 || \
    die "Keepalived configuration validation failed after adding the managed LVS include"
  log "LVS managed include ready: ${LVS_MANAGED_FILE}"
}

ensure_identity_user() {
  getent group "${APP_GROUP}" >/dev/null 2>&1 || groupadd --system "${APP_GROUP}"
  if ! id "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --gid "${APP_GROUP}" --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
  fi
}

write_config() {
  local ca_target log_dirs_text config_dirs_text stream_dirs_text
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

  printf -v log_dirs_text '%s\n' "${NGINX_LOG_DIRS[@]+"${NGINX_LOG_DIRS[@]}"}"
  printf -v config_dirs_text '%s\n' "${MANAGED_CONFIG_DIRS[@]+"${MANAGED_CONFIG_DIRS[@]}"}"
  printf -v stream_dirs_text '%s\n' "${MANAGED_STREAM_DIRS[@]+"${MANAGED_STREAM_DIRS[@]}"}"
  "${PYTHON_BIN}" - "${CONFIG_FILE}" "${SERVER_URL}" "${NODE_NAME}" "${NODE_PROFILE}" "${LABELS}" \
    "${ca_target}" "${TLS_SKIP_VERIFY}" "${ALLOW_INSECURE_HTTP}" "${POLL_SECONDS}" "${NGINX_BINARY}" "$(command -v openssl)" "${NGINX_CONFIG}" "${NGINX_ROOT}" \
    "${config_dirs_text}" "${stream_dirs_text}" "${ALLOW_MAIN_CONFIG_EDIT}" "${MANAGED_CERT_DIR}" "${STATE_DIR}" "${HELPER_STATE_DIR}" "${HEALTH_URL}" "${log_dirs_text}" "${STUB_STATUS_URL}" "${ALLOW_PLAINTEXT_LOG_STREAM}" \
    "${KEEPALIVED_BINARY}" "${KEEPALIVED_CONFIG}" "${KEEPALIVED_SERVICE}" "${KEEPALIVED_VIP}" "${LVS_TOPOLOGY}" "${ENABLE_LVS_OBSERVER}" \
    "${ENABLE_LVS_MANAGEMENT}" "${LVS_MANAGED_FILE}" <<'PY'
import hashlib
import json
import os
import socket
import sys
from urllib.parse import urlparse

(
    config_path, server_url, node_name, node_profile, raw_labels, ca_file,
    tls_skip_verify, allow_insecure_http, poll_seconds, nginx_binary, openssl_binary, nginx_config, nginx_root,
    raw_config_dirs, raw_stream_dirs, allow_main_config_edit, managed_cert_dir, state_dir, helper_state_dir, health_url,
    raw_log_dirs, stub_status_url, allow_plaintext_log_stream,
    keepalived_binary, keepalived_config, keepalived_service, keepalived_vip, lvs_topology, enable_lvs_observer,
    enable_lvs_management, lvs_managed_file,
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

config_entries = []
all_roots = []
for context, suffix, raw_directories in (
    ("http", ".conf", raw_config_dirs),
    ("stream", ".stream", raw_stream_dirs),
):
    directories = []
    for item in raw_directories.splitlines():
        if item and item not in directories:
            directories.append(item)
    for index, directory in enumerate(directories):
        entry_id = "{}-{}".format(
            context,
            hashlib.sha256(
                (context + "\0" + os.path.abspath(directory) + "\0" + suffix).encode("utf-8")
            ).hexdigest()[:12],
        )
        config_entries.append({
            "id": entry_id,
            "context": context,
            "directory": directory,
            "suffix": suffix,
            "default": index == 0,
            "label": "{} · {}".format(context.upper(), directory),
        })
        if directory not in all_roots:
            all_roots.append(directory)

value = {
    "server_url": server_url.rstrip("/"),
    "node_name": node_name,
    "node_profile": node_profile,
    "hostname": socket.gethostname(),
    "labels": labels,
    "ca_file": ca_file or None,
    "tls_skip_verify": tls_skip_verify == "1",
    "allow_insecure_http": allow_insecure_http == "1",
    "poll_interval": float(poll_seconds),
    "heartbeat_interval": 15,
    "api_timeout": 30,
    "command_timeout": 30,
    "nginx_binary": nginx_binary,
    "openssl_binary": openssl_binary,
    "nginx_config": nginx_config,
    "nginx_root": nginx_root,
    "allowed_config_roots": all_roots,
    "config_entries": config_entries,
    "allow_main_config_edit": allow_main_config_edit == "1",
    "verify_config_entries_loaded": True,
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
    "allowed_log_roots": [item for item in raw_log_dirs.splitlines() if item],
    "stub_status_url": stub_status_url or None,
    "allow_plaintext_log_stream": allow_plaintext_log_stream == "1",
    "keepalived_binary": keepalived_binary or None,
    "keepalived_config": keepalived_config or None,
    "keepalived_service": keepalived_service or None,
    "keepalived_vip": keepalived_vip or None,
    "lvs_topology": lvs_topology or None,
    "ipvs_observer_enabled": enable_lvs_observer == "1",
    "lvs_management_enabled": enable_lvs_management == "1",
    "lvs_managed_file": lvs_managed_file or None,
}

if node_profile == "lvs":
    value.update({
        "nginx_binary": "",
        "openssl_binary": "",
        "nginx_config": "",
        "nginx_root": "",
        "allowed_config_roots": [],
        "config_entries": [],
        "allow_main_config_edit": False,
        "verify_config_entries_loaded": False,
        "allowed_certificate_roots": [],
        "health_check": None,
        "allowed_log_roots": [],
        "stub_status_url": None,
        "allow_plaintext_log_stream": False,
    })

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
  local python_path systemd_version protect_system write_access_key recovery_before helper_write_paths
  local modern_hardening="" runtime_preserve="" nginx_write_paths="" managed_write_paths="" directory
  python_path="$(command -v "${PYTHON_BIN}")"
  recovery_before="${NGINX_SERVICE}"
  if [[ "${NODE_PROFILE}" == "lvs" ]]; then
    recovery_before="${KEEPALIVED_SERVICE}"
  elif [[ "${ENABLE_LVS_MANAGEMENT}" == "1" ]]; then
    recovery_before="${NGINX_SERVICE} ${KEEPALIVED_SERVICE}"
  fi
  systemd_version="$(systemctl --version 2>/dev/null | awk 'NR == 1 {print $2}')"
  [[ "${systemd_version}" =~ ^[0-9]+$ ]] || die "无法识别 systemd 版本"
  if [[ "${NODE_PROFILE}" != "lvs" ]]; then
    [[ ! -d /var/log/nginx ]] || nginx_write_paths+=" /var/log/nginx"
    [[ ! -d /var/cache/nginx ]] || nginx_write_paths+=" /var/cache/nginx"
    for directory in \
      "${MANAGED_CONFIG_DIRS[@]+"${MANAGED_CONFIG_DIRS[@]}"}" \
      "${MANAGED_STREAM_DIRS[@]+"${MANAGED_STREAM_DIRS[@]}"}"; do
      [[ " ${managed_write_paths} " == *" ${directory} "* ]] || managed_write_paths+=" ${directory}"
    done
    [[ "${ALLOW_MAIN_CONFIG_EDIT}" != "1" ]] || managed_write_paths+=" ${NGINX_CONFIG}"
    managed_write_paths+=" ${MANAGED_CERT_DIR}"
  fi
  if [[ "${ENABLE_LVS_MANAGEMENT}" == "1" ]]; then
    managed_write_paths+=" $(dirname -- "${KEEPALIVED_CONFIG}") ${LVS_MANAGED_FILE}"
  fi
  helper_write_paths="${managed_write_paths} ${HELPER_STATE_DIR}"
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
Description=Recover interrupted Nginx Manager publications before the managed service starts
After=local-fs.target
Before=${recovery_before} ${APP_NAME}-helper.service

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
CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_FOWNER CAP_CHOWN CAP_KILL CAP_NET_BIND_SERVICE
${write_access_key}=${helper_write_paths}${nginx_write_paths}
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
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_FOWNER CAP_CHOWN CAP_KILL CAP_NET_BIND_SERVICE
${write_access_key}=${helper_write_paths} /run/${APP_NAME}${nginx_write_paths}
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

  if [[ "${NODE_PROFILE}" != "lvs" ]]; then
    install -d -m 0755 -o root -g root "$(dirname -- "${NGINX_DROPIN}")"
    cat >"${NGINX_DROPIN}" <<EOF
[Unit]
Requires=${APP_NAME}-recover.service
After=${APP_NAME}-recover.service
EOF
  fi

  systemd-analyze verify "${RECOVERY_SERVICE}" "${HELPER_SERVICE}" "${AGENT_SERVICE}" >/dev/null
  systemctl daemon-reload
}

run_as_agent() {
  command -v runuser >/dev/null 2>&1 || die "系统缺少 runuser（通常由 util-linux 提供）"
  runuser -u "${APP_USER}" -- "$@"
}

upgrade_agent_binary() {
  local backup backup_lvs temporary helper_was_active="0" agent_was_active="0" failed="0" lvs_was_present="0"
  [[ -f "${AGENT_SOURCE}" && ! -L "${AGENT_SOURCE}" ]] || die "找不到新版本 agent/nginx_agent.py"
  [[ -f "${LVS_CONTROL_SOURCE}" && ! -L "${LVS_CONTROL_SOURCE}" ]] || die "cannot find agent/lvs_control.py"
  [[ -f "${APP_DIR}/nginx_agent.py" && ! -L "${APP_DIR}/nginx_agent.py" ]] || \
    die "尚未安装 Agent；首次安装不能使用 --upgrade"
  [[ -f "${CONFIG_FILE}" && ! -L "${CONFIG_FILE}" ]] || \
    die "找不到现有 Agent 配置；请使用完整安装命令修复"
  [[ -f "${AGENT_SERVICE}" && -f "${HELPER_SERVICE}" ]] || \
    die "找不到现有 Agent systemd 单元；请使用完整安装命令修复"
  id "${APP_USER}" >/dev/null 2>&1 || die "找不到现有 Agent 系统用户"

  install_base_dependencies
  systemctl is-active --quiet "${APP_NAME}-helper.service" && helper_was_active="1" || true
  systemctl is-active --quiet "${APP_NAME}.service" && agent_was_active="1" || true
  systemctl stop "${APP_NAME}.service" "${APP_NAME}-helper.service" >/dev/null 2>&1 || true
  if ! recover_existing_transactions; then
    [[ "${helper_was_active}" == "1" ]] && systemctl start "${APP_NAME}-helper.service" >/dev/null 2>&1 || true
    [[ "${agent_was_active}" == "1" ]] && systemctl start "${APP_NAME}.service" >/dev/null 2>&1 || true
    return 1
  fi
  backup_lvs="$(mktemp "${APP_DIR}/.lvs_control.py.backup.XXXXXX")"
  if [[ -f "${APP_DIR}/lvs_control.py" && ! -L "${APP_DIR}/lvs_control.py" ]]; then
    cp -a -- "${APP_DIR}/lvs_control.py" "${backup_lvs}"
    lvs_was_present="1"
  fi
  install -m 0755 -o root -g root "${LVS_CONTROL_SOURCE}" "${APP_DIR}/lvs_control.py"
  temporary="$(mktemp "${APP_DIR}/.nginx_agent.py.upgrade.XXXXXX")"
  install -m 0755 -o root -g root "${AGENT_SOURCE}" "${temporary}"
  if ! run_as_agent "${PYTHON_BIN}" "${temporary}" --config "${CONFIG_FILE}" validate-config; then
    rm -f -- "${temporary}"
    if [[ "${lvs_was_present}" == "1" ]]; then
      install -m 0755 -o root -g root "${backup_lvs}" "${APP_DIR}/lvs_control.py"
    else
      rm -f -- "${APP_DIR}/lvs_control.py"
    fi
    rm -f -- "${backup_lvs}"
    [[ "${helper_was_active}" == "1" ]] && systemctl start "${APP_NAME}-helper.service" >/dev/null 2>&1 || true
    [[ "${agent_was_active}" == "1" ]] && systemctl start "${APP_NAME}.service" >/dev/null 2>&1 || true
    die "新版本 Agent 无法读取现有配置，尚未替换程序"
  fi
  backup="$(mktemp "${APP_DIR}/.nginx_agent.py.backup.XXXXXX")"
  cp -a -- "${APP_DIR}/nginx_agent.py" "${backup}"
  mv -f -- "${temporary}" "${APP_DIR}/nginx_agent.py"
  run_as_agent "${PYTHON_BIN}" "${APP_DIR}/nginx_agent.py" --config "${CONFIG_FILE}" validate-config || failed="1"
  if [[ "${failed}" == "0" && "${helper_was_active}" == "1" ]]; then
    systemctl restart "${APP_NAME}-helper.service" || failed="1"
  fi
  if [[ "${failed}" == "0" && "${agent_was_active}" == "1" ]]; then
    systemctl restart "${APP_NAME}.service" || failed="1"
  fi
  [[ "${failed}" != "0" ]] || sleep 2
  if [[ "${failed}" == "0" && "${helper_was_active}" == "1" ]]; then
    systemctl is-active --quiet "${APP_NAME}-helper.service" || failed="1"
  fi
  if [[ "${failed}" == "0" && "${agent_was_active}" == "1" ]]; then
    systemctl is-active --quiet "${APP_NAME}.service" || failed="1"
  fi

  if [[ "${failed}" != "0" ]]; then
    local rollback_failed="0"
    log "升级启动检查失败，恢复上一版 Agent 程序"
    temporary="$(mktemp "${APP_DIR}/.nginx_agent.py.rollback.XXXXXX")"
    install -m 0755 -o root -g root "${backup}" "${temporary}"
    mv -f -- "${temporary}" "${APP_DIR}/nginx_agent.py"
    if [[ "${lvs_was_present}" == "1" ]]; then
      install -m 0755 -o root -g root "${backup_lvs}" "${APP_DIR}/lvs_control.py"
    else
      rm -f -- "${APP_DIR}/lvs_control.py"
    fi
    [[ "${helper_was_active}" != "1" ]] || systemctl restart "${APP_NAME}-helper.service" >/dev/null 2>&1 || rollback_failed="1"
    [[ "${agent_was_active}" != "1" ]] || systemctl restart "${APP_NAME}.service" >/dev/null 2>&1 || rollback_failed="1"
    sleep 2
    [[ "${helper_was_active}" != "1" ]] || systemctl is-active --quiet "${APP_NAME}-helper.service" || rollback_failed="1"
    [[ "${agent_was_active}" != "1" ]] || systemctl is-active --quiet "${APP_NAME}.service" || rollback_failed="1"
    rm -f -- "${backup}" "${backup_lvs}"
    [[ "${rollback_failed}" == "0" ]] || \
      die "Agent 程序已恢复，但旧服务未恢复运行；请检查 journalctl -u ${APP_NAME} -u ${APP_NAME}-helper"
    die "Agent 升级失败，已恢复上一版本"
  fi

  rm -f -- "${backup}" "${backup_lvs}"
  log "Agent 程序升级完成；现有配置、身份和 systemd 设置均已保留"
  echo "服务状态：systemctl status ${APP_NAME} ${APP_NAME}-helper"
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

if [[ "$#" -eq 1 && "$1" == "--upgrade" ]]; then
  UPGRADE_MODE="1"
  shift
else
  for argument in "$@"; do
    [[ "${argument}" != "--upgrade" ]] || die "--upgrade 必须单独使用，修改路径或能力请运行完整安装命令"
  done
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) [[ $# -ge 2 ]] || die "--server 缺少值"; SERVER_URL="$2"; shift 2 ;;
    --node-name) [[ $# -ge 2 ]] || die "--node-name 缺少值"; NODE_NAME="$2"; shift 2 ;;
    --node-ip) [[ $# -ge 2 ]] || die "--node-ip 缺少值"; NODE_IP="$2"; shift 2 ;;
    --profile) [[ $# -ge 2 ]] || die "--profile 缺少值"; NODE_PROFILE="$2"; shift 2 ;;
    --labels) [[ $# -ge 2 ]] || die "--labels 缺少值"; LABELS="$2"; shift 2 ;;
    --ca-file) [[ $# -ge 2 ]] || die "--ca-file 缺少值"; CA_SOURCE="$2"; shift 2 ;;
    --insecure-skip-tls-verify) TLS_SKIP_VERIFY="1"; shift ;;
    --nginx-prefix) [[ $# -ge 2 ]] || die "--nginx-prefix 缺少值"; NGINX_PREFIX="$2"; shift 2 ;;
    --nginx-binary) [[ $# -ge 2 ]] || die "--nginx-binary 缺少值"; NGINX_BINARY="$2"; shift 2 ;;
    --nginx-root) [[ $# -ge 2 ]] || die "--nginx-root 缺少值"; NGINX_ROOT="$2"; shift 2 ;;
    --nginx-config) [[ $# -ge 2 ]] || die "--nginx-config 缺少值"; NGINX_CONFIG="$2"; shift 2 ;;
    --managed-config-dir) [[ $# -ge 2 ]] || die "--managed-config-dir 缺少值"; MANAGED_CONFIG_DIRS+=("$2"); shift 2 ;;
    --managed-stream-dir) [[ $# -ge 2 ]] || die "--managed-stream-dir 缺少值"; MANAGED_STREAM_DIRS+=("$2"); shift 2 ;;
    --managed-cert-dir) [[ $# -ge 2 ]] || die "--managed-cert-dir 缺少值"; MANAGED_CERT_DIR="$2"; shift 2 ;;
    --managed-include-file) [[ $# -ge 2 ]] || die "--managed-include-file 缺少值"; MANAGED_INCLUDE_FILE="$2"; shift 2 ;;
    --managed-config-already-included) MANAGED_CONFIG_ALREADY_INCLUDED="1"; shift ;;
    --manage-stream) MANAGE_STREAM="1"; shift ;;
    --allow-main-config-edit) ALLOW_MAIN_CONFIG_EDIT="1"; shift ;;
    --nginx-service) [[ $# -ge 2 ]] || die "--nginx-service 缺少值"; NGINX_SERVICE="$2"; shift 2 ;;
    --keepalived-binary) [[ $# -ge 2 ]] || die "--keepalived-binary 缺少值"; KEEPALIVED_BINARY="$2"; shift 2 ;;
    --keepalived-config) [[ $# -ge 2 ]] || die "--keepalived-config 缺少值"; KEEPALIVED_CONFIG="$2"; shift 2 ;;
    --keepalived-service) [[ $# -ge 2 ]] || die "--keepalived-service 缺少值"; KEEPALIVED_SERVICE="$2"; shift 2 ;;
    --keepalived-vip) [[ $# -ge 2 ]] || die "--keepalived-vip 缺少值"; KEEPALIVED_VIP="$2"; shift 2 ;;
    --enable-lvs-observer) ENABLE_LVS_OBSERVER="1"; shift ;;
    --enable-lvs-management) ENABLE_LVS_MANAGEMENT="1"; shift ;;
    --lvs-topology) [[ $# -ge 2 ]] || die "--lvs-topology requires a value"; LVS_TOPOLOGY="$2"; shift 2 ;;
    --managed-lvs-file) [[ $# -ge 2 ]] || die "--managed-lvs-file requires a value"; LVS_MANAGED_FILE="$2"; shift 2 ;;
    --health-url) [[ $# -ge 2 ]] || die "--health-url 缺少值"; HEALTH_URL="$2"; shift 2 ;;
    --nginx-log-dir) [[ $# -ge 2 ]] || die "--nginx-log-dir 缺少值"; NGINX_LOG_DIRS+=("$2"); shift 2 ;;
    --stub-status-url) [[ $# -ge 2 ]] || die "--stub-status-url 缺少值"; STUB_STATUS_URL="$2"; shift 2 ;;
    --allow-plaintext-log-stream) ALLOW_PLAINTEXT_LOG_STREAM="1"; shift ;;
    --poll-seconds) [[ $# -ge 2 ]] || die "--poll-seconds 缺少值"; POLL_SECONDS="$2"; shift 2 ;;
    --install-nginx) INSTALL_NGINX="1"; shift ;;
    --force-enroll) FORCE_ENROLL="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

require_root
install -d -m 0755 /run/lock
exec 9>/run/lock/nginx-manager-agent-install.lock
flock -n 9 || die "另一个 Agent 安装或升级进程正在运行"
refuse_unresolved_transactions

if [[ "${UPGRADE_MODE}" == "1" ]]; then
  upgrade_agent_binary
  exit 0
fi

[[ "${NODE_PROFILE}" =~ ^(nginx|lvs|hybrid)$ ]] || die "--profile must be nginx, lvs, or hybrid"
if [[ "${NODE_PROFILE}" == "lvs" || "${ENABLE_LVS_MANAGEMENT}" == "1" ]]; then
  ENABLE_LVS_OBSERVER="1"
fi
if [[ "${NODE_PROFILE}" != "lvs" ]]; then
  apply_nginx_prefix_defaults
fi
[[ -n "${SERVER_URL}" ]] || { usage; die "必须指定 --server"; }
[[ -n "${NODE_NAME}" && "${NODE_NAME}" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || die "节点名称只允许字母、数字、点、下划线和短横线"
[[ "${TLS_SKIP_VERIFY}" != "1" || -z "${CA_SOURCE}" ]] || die "--ca-file 与 --insecure-skip-tls-verify 不能同时使用"
if [[ "${NODE_PROFILE}" != "lvs" ]]; then
  [[ "${MANAGED_CONFIG_ALREADY_INCLUDED}" != "1" || -z "${MANAGED_INCLUDE_FILE}" ]] || die "--managed-config-already-included 不能与 --managed-include-file 同时使用"
  [[ "${NGINX_ROOT}" = /* && "${NGINX_CONFIG}" = /* ]] || die "Nginx 路径必须是绝对路径"
  for optional_path in \
    "${NGINX_BINARY}" \
    "${MANAGED_CONFIG_DIRS[@]+"${MANAGED_CONFIG_DIRS[@]}"}" \
    "${MANAGED_STREAM_DIRS[@]+"${MANAGED_STREAM_DIRS[@]}"}" \
    "${MANAGED_CERT_DIR}" \
    "${MANAGED_INCLUDE_FILE}"; do
    [[ -z "${optional_path}" || "${optional_path}" = /* ]] || die "自定义 Nginx 路径必须是绝对路径：${optional_path}"
  done
  for nginx_path in \
    "${NGINX_ROOT}" \
    "${NGINX_CONFIG}" \
    "${NGINX_BINARY}" \
    "${MANAGED_CONFIG_DIRS[@]+"${MANAGED_CONFIG_DIRS[@]}"}" \
    "${MANAGED_STREAM_DIRS[@]+"${MANAGED_STREAM_DIRS[@]}"}" \
    "${MANAGED_CERT_DIR}" \
    "${MANAGED_INCLUDE_FILE}"; do
    [[ ! "${nginx_path}" =~ [[:space:]] ]] || die "Nginx 路径不能包含空白字符：${nginx_path}"
  done
  [[ "${NGINX_SERVICE}" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || die "--nginx-service 必须是合法的 .service 单元名"
  NGINX_DROPIN="/etc/systemd/system/${NGINX_SERVICE}.d/nginx-manager-agent-recovery.conf"
fi

install_base_dependencies
apply_node_ip_label
validate_server_url
validate_existing_identity_binding
if [[ "${NODE_PROFILE}" != "lvs" ]]; then
  install_nginx_if_requested
fi
prepare_keepalived_options
if [[ "${NODE_PROFILE}" == "lvs" ]]; then
  [[ -n "${KEEPALIVED_CONFIG}" && -n "${KEEPALIVED_SERVICE}" && -n "${LVS_TOPOLOGY}" ]] || \
    die "--profile lvs requires Keepalived integration"
else
  prepare_monitoring_options
  systemctl cat "${NGINX_SERVICE}" >/dev/null 2>&1 || die "找不到 ${NGINX_SERVICE}；无法建立 Nginx 启动前恢复屏障"
  [[ -x "${NGINX_BINARY}" ]] || die "Nginx 二进制不可执行"
  [[ -f "${NGINX_CONFIG}" ]] || die "找不到 Nginx 主配置 ${NGINX_CONFIG}"
  "${NGINX_BINARY}" -t -c "${NGINX_CONFIG}" || die "现有 Nginx 配置校验失败，Agent 未安装"
fi
[[ -f "${AGENT_SOURCE}" ]] || die "找不到 agent/nginx_agent.py，请从完整发布包内运行"
[[ -f "${LVS_CONTROL_SOURCE}" && ! -L "${LVS_CONTROL_SOURCE}" ]] || die "cannot find agent/lvs_control.py"
if [[ "${ENABLE_LVS_MANAGEMENT}" == "1" ]]; then
  [[ "${NODE_PROFILE}" == "lvs" || "${NODE_PROFILE}" == "hybrid" ]] || \
    die "--enable-lvs-management requires --profile lvs or hybrid"
  [[ -n "${KEEPALIVED_CONFIG}" ]] || die "--enable-lvs-management requires Keepalived integration"
  [[ -n "${LVS_MANAGED_FILE}" ]] || \
    LVS_MANAGED_FILE="$(dirname -- "${KEEPALIVED_CONFIG}")/nginx-manager.d/50-lvs-managed.conf"
fi

ensure_identity_user
# Stop the network poller and privileged helper before taking the rollback
# snapshot.  Otherwise a job may commit between the snapshot and replacement,
# and an installer rollback could silently overwrite that committed change.
systemctl is-active --quiet "${APP_NAME}.service" && OLD_AGENT_ACTIVE="1" || true
systemctl is-active --quiet "${APP_NAME}-helper.service" && OLD_HELPER_ACTIVE="1" || true
systemctl stop "${APP_NAME}.service" "${APP_NAME}-helper.service" >/dev/null 2>&1 || true
if ! recover_existing_transactions; then
  [[ "${OLD_HELPER_ACTIVE}" == "1" ]] && systemctl start "${APP_NAME}-helper.service" >/dev/null 2>&1 || true
  [[ "${OLD_AGENT_ACTIVE}" == "1" ]] && systemctl start "${APP_NAME}.service" >/dev/null 2>&1 || true
  exit 1
fi
begin_install_transaction
prepare_lvs_management
if [[ "${NODE_PROFILE}" != "lvs" ]]; then
  prepare_managed_directories
fi
install -d -m 0755 -o root -g root "${APP_DIR}"
install -d -m 0750 -o root -g "${APP_GROUP}" "${ETC_DIR}"
install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${STATE_DIR}"
install -d -m 0700 -o root -g root "${HELPER_STATE_DIR}"
install -m 0755 -o root -g root "${AGENT_SOURCE}" "${APP_DIR}/nginx_agent.py"
install -m 0755 -o root -g root "${LVS_CONTROL_SOURCE}" "${APP_DIR}/lvs_control.py"
write_config
run_as_agent "${PYTHON_BIN}" "${APP_DIR}/nginx_agent.py" --config "${CONFIG_FILE}" validate-config
write_services
systemctl enable "${APP_NAME}-helper.service" "${APP_NAME}.service"
enroll_if_needed
systemctl reset-failed "${APP_NAME}-recover.service" "${APP_NAME}-helper.service" "${APP_NAME}.service" >/dev/null 2>&1 || true
if ! systemctl restart "${APP_NAME}-helper.service"; then
  journalctl -u "${APP_NAME}-recover.service" -u "${APP_NAME}-helper.service" -n 120 --no-pager >&2 || true
  die "root helper 或其恢复依赖启动失败"
fi
if ! systemctl restart "${APP_NAME}.service"; then
  journalctl -u "${APP_NAME}.service" -n 120 --no-pager >&2 || true
  die "Agent 启动失败"
fi
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
