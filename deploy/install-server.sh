#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

APP_NAME="nginx-manager"
APP_USER="nginx-manager"
APP_GROUP="nginx-manager"
APP_ROOT="/opt/${APP_NAME}"
RELEASES_DIR="${APP_ROOT}/releases"
CURRENT_LINK="${APP_ROOT}/current"
ETC_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
DB_FILE="${DATA_DIR}/manager.db"
ENV_FILE="${ETC_DIR}/server.env"
TLS_DIR="${ETC_DIR}/tls"
LDAP_PASSWORD_FILE="${ETC_DIR}/ldap-bind-password"
LDAP_CA_FILE="${ETC_DIR}/ldap-ca.crt"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
CREDENTIALS_FILE="/root/${APP_NAME}-credentials.txt"
DEFAULT_PORT="8443"
PYTHON_BIN="python3"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PACKAGE_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
SERVER_SOURCE="${PACKAGE_DIR}/server"
UI_SOURCE="${PACKAGE_DIR}/nginx-cluster-console.html"
[[ -f "${UI_SOURCE}" ]] || UI_SOURCE="${PACKAGE_DIR}/../nginx-cluster-console.html"

MANAGER_HOST=""
LISTEN_PORT="${DEFAULT_PORT}"
CERT_FILE=""
KEY_FILE=""
SELF_SIGNED="0"
BEHIND_NGINX="0"
ALLOW_DIRECT_HTTP="0"
PUBLIC_URL=""
OPEN_FIREWALL="0"
FIREWALL_CIDR=""
LDAP_CONFIG_PROVIDED="0"
LDAP_DISABLED="0"
LDAP_URL=""
LDAP_BASE_DN=""
LDAP_BIND_DN=""
LDAP_BIND_PASSWORD_SOURCE=""
LDAP_USER_FILTER="(|(sAMAccountName={username})(userPrincipalName={username})(uid={username}))"
LDAP_GROUP_ATTRIBUTE="memberOf"
LDAP_GROUP_SEARCH_BASE=""
LDAP_GROUP_FILTER="(member={user_dn})"
LDAP_ADMIN_GROUP="nginx-admin"
LDAP_OPERATOR_GROUP="nginx-operator"
LDAP_AUDITOR_GROUP="nginx-auditor"
LDAP_START_TLS="0"
LDAP_CA_SOURCE=""

WORK_DIR=""
STAGING_DIR=""
NEW_RELEASE=""
NEW_RELEASE_CREATED="0"
ROLLBACK_DIR=""
TRANSACTION_ACTIVE="0"
PRESERVE_WORK_DIR="0"
OLD_CURRENT_TARGET=""
OLD_CURRENT_PRESENT="0"
OLD_SERVICE_PRESENT="0"
OLD_ENV_PRESENT="0"
OLD_TLS_PRESENT="0"
OLD_LDAP_PASSWORD_PRESENT="0"
OLD_LDAP_CA_PRESENT="0"
OLD_CREDENTIALS_PRESENT="0"
OLD_SERVICE_ACTIVE="0"
OLD_SERVICE_ENABLED="0"
DB_SNAPSHOT_STATE="not_started"
ADMIN_USERNAME="admin"
ADMIN_PASSWORD=""
ADMIN_CREATED="0"

usage() {
  cat <<'USAGE'
用法：
  sudo ./deploy/install-server.sh --host <域名或服务器IP> [选项]

选项：
  --host <值>        Agent 和浏览器访问的域名或 IPv4 地址（必填）
  --port <端口>      直连模式为 HTTPS 端口；反代/HTTP 模式为后端端口，默认 8443
  --cert <路径>      使用现有 PEM 服务端证书（应包含完整链）
  --key <路径>       使用现有无口令 PEM 服务端私钥
  --self-signed      明确使用脚本生成的本地 CA 与服务端证书
  --behind-nginx     仅监听 127.0.0.1 HTTP，由本机 Nginx 终止 HTTPS；不生成控制端证书
  --allow-direct-http 与 --behind-nginx 配合，同时监听所有网卡并允许 http://主机:端口 直连
  --public-url <URL> 对外管理地址；默认按部署模式自动生成
  --ldap-url <URL>   启用 LDAP/AD，例如 ldap://192.0.2.10:389 或 ldaps://ldap.example.com
  --ldap-base-dn <DN>       用户搜索根 DN
  --ldap-bind-dn <DN>       只读查询账号 DN
  --ldap-bind-password-file <路径>  查询账号密码文件（安装后复制并设为 0640）
  --ldap-user-filter <过滤器>       默认匹配 sAMAccountName、UPN 和 uid
  --ldap-group-attribute <属性>     用户对象上的组属性，默认 memberOf
  --ldap-group-search-base <DN>     OpenLDAP 无 memberOf 时用于反查组，可选
  --ldap-group-filter <过滤器>      反查组过滤器，默认 (member={user_dn})
  --ldap-admin-group <组>           默认 nginx-admin
  --ldap-operator-group <组>        默认 nginx-operator
  --ldap-auditor-group <组>         默认 nginx-auditor
  --ldap-start-tls                  对 ldap:// 先执行 StartTLS
  --ldap-ca-file <路径>             LDAPS/StartTLS 的可信 CA，可选（否则使用系统信任库）
  --disable-ldap                    升级时关闭 LDAP 并移除其运行配置
  --open-firewall    在已启用的 ufw/firewalld 中放行管理端口
  --allow-cidr <网段> 与 --open-firewall 配合，仅允许指定 IPv4 CIDR
  -h, --help         显示帮助

直连 TLS 模式必须选择 --cert/--key 或 --self-signed。使用 --behind-nginx 时控制端
不持有服务端证书，外层 Nginx 提供 HTTPS。只有显式增加 --allow-direct-http 才会
同时暴露未加密 HTTP；该入口只建议在可信内网使用。
USAGE
}

die() {
  echo "错误：$*" >&2
  exit 1
}

log() {
  echo "[nginx-manager] $*"
}

warn() {
  echo "[nginx-manager] 警告：$*" >&2
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "请使用 root 或 sudo 运行"
}

valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && (( "$1" >= 1024 && "$1" <= 65535 ))
}

environment_quote() {
  "${PYTHON_BIN}" - "$1" <<'PY'
import sys

value = sys.argv[1]
if any(character in value for character in ("\0", "\n", "\r")):
    raise SystemExit("environment value contains a forbidden character")
print('"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"')
PY
}

append_environment() {
  local key="$1" value="$2"
  printf '%s=%s\n' "${key}" "$(environment_quote "${value}")" >>"${WORK_DIR}/server.env"
}

refuse_incomplete_transaction() {
  local path found="0"
  for path in /var/tmp/nginx-manager-install.*/rollback; do
    [[ -d "${path}" ]] || continue
    if [[ "${found}" == "0" ]]; then
      echo "错误：发现未完成或未确认的历史安装事务：" >&2
    fi
    echo "  ${path}" >&2
    found="1"
  done
  [[ "${found}" == "0" ]] || die "请先人工恢复或确认并移走上述备份，再重新运行安装脚本"
}

cleanup_files() {
  if [[ -n "${STAGING_DIR}" && -e "${STAGING_DIR}" ]]; then
    if ! rm -rf -- "${STAGING_DIR}"; then
      warn "无法清理暂存目录 ${STAGING_DIR}"
    fi
  fi
  if [[ "${PRESERVE_WORK_DIR}" != "1" && -n "${WORK_DIR}" && -e "${WORK_DIR}" ]]; then
    if ! rm -rf -- "${WORK_DIR}"; then
      warn "无法清理临时目录 ${WORK_DIR}"
    fi
  fi
}

atomic_set_current() {
  local target="$1"
  local temporary_link="${APP_ROOT}/.current.$$.tmp"
  rm -f -- "${temporary_link}"
  ln -s -- "${target}" "${temporary_link}"
  mv -Tf -- "${temporary_link}" "${CURRENT_LINK}"
}

restore_path() {
  local present="$1"
  local backup="$2"
  local destination="$3"
  rm -rf -- "${destination}" || return 1
  if [[ "${present}" == "1" ]]; then
    cp -a -- "${backup}" "${destination}" || return 1
    if [[ -d "${backup}" ]]; then
      diff -qr -- "${backup}" "${destination}" >/dev/null || return 1
    else
      cmp -s -- "${backup}" "${destination}" || return 1
    fi
    [[ "$(stat -c '%a:%u:%g:%F' -- "${backup}")" == "$(stat -c '%a:%u:%g:%F' -- "${destination}")" ]] || return 1
  else
    [[ ! -e "${destination}" && ! -L "${destination}" ]] || return 1
  fi
}

sanitize_environment_file() {
  local path="$1"
  "${PYTHON_BIN}" - "${path}" <<'PY'
import re
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    lines = handle.readlines()
filtered = [
    line for line in lines
    if re.match(r"^\s*(?:export\s+)?NGINX_MANAGER_ENROLLMENT_TOKEN\s*=", line) is None
]
with open(path, "w", encoding="utf-8") as handle:
    handle.writelines(filtered)
PY
}

snapshot_database() {
  local snapshot="${ROLLBACK_DIR}/manager.db"
  if [[ ! -f "${DB_FILE}" ]]; then
    DB_SNAPSHOT_STATE="absent"
    return
  fi
  "${PYTHON_BIN}" - "${DB_FILE}" "${snapshot}" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1], timeout=30)
target = sqlite3.connect(sys.argv[2], timeout=30)
try:
    check = source.execute("PRAGMA quick_check").fetchone()
    if check is None or check[0] != "ok":
        raise RuntimeError("source SQLite quick_check failed: {!r}".format(check))
    source.backup(target)
finally:
    target.close()
    source.close()
PY
  chown --reference="${DB_FILE}" "${snapshot}"
  chmod --reference="${DB_FILE}" "${snapshot}"
  DB_SNAPSHOT_STATE="present"
}

restore_database() {
  case "${DB_SNAPSHOT_STATE}" in
    present)
      rm -f -- "${DB_FILE}-journal" "${DB_FILE}-wal" "${DB_FILE}-shm" || return 1
      restore_path "1" "${ROLLBACK_DIR}/manager.db" "${DB_FILE}" || return 1
      rm -f -- "${DB_FILE}-journal" "${DB_FILE}-wal" "${DB_FILE}-shm" || return 1
      [[ ! -e "${DB_FILE}-journal" && ! -e "${DB_FILE}-wal" && ! -e "${DB_FILE}-shm" ]]
      ;;
    absent)
      rm -f -- "${DB_FILE}" "${DB_FILE}-journal" "${DB_FILE}-wal" "${DB_FILE}-shm" || return 1
      [[ ! -e "${DB_FILE}" && ! -e "${DB_FILE}-journal" && ! -e "${DB_FILE}-wal" && ! -e "${DB_FILE}-shm" ]]
      ;;
    not_started)
      # No new release could have touched the database before this state.
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

rollback_install() {
  local rollback_failed="0"
  warn "新版本未通过启动自检，开始恢复安装前版本"
  systemctl stop "${APP_NAME}.service" >/dev/null 2>&1 || true

  if [[ "${OLD_CURRENT_PRESENT}" == "1" ]]; then
    if ! atomic_set_current "${OLD_CURRENT_TARGET}" || \
       [[ ! -L "${CURRENT_LINK}" || "$(readlink "${CURRENT_LINK}" 2>/dev/null)" != "${OLD_CURRENT_TARGET}" ]]; then
      rollback_failed="1"
      warn "current 链接未能恢复到 ${OLD_CURRENT_TARGET}"
    fi
  else
    if ! rm -f -- "${CURRENT_LINK}" || [[ -e "${CURRENT_LINK}" || -L "${CURRENT_LINK}" ]]; then
      rollback_failed="1"
      warn "首次安装的 current 链接未能撤销"
    fi
  fi

  if ! restore_path "${OLD_SERVICE_PRESENT}" "${ROLLBACK_DIR}/service" "${SERVICE_FILE}"; then
    rollback_failed="1"
    warn "systemd 服务文件恢复或校验失败"
  fi
  if ! restore_path "${OLD_ENV_PRESENT}" "${ROLLBACK_DIR}/environment" "${ENV_FILE}"; then
    rollback_failed="1"
    warn "服务端环境文件恢复或校验失败"
  fi
  if ! restore_path "${OLD_TLS_PRESENT}" "${ROLLBACK_DIR}/tls" "${TLS_DIR}"; then
    rollback_failed="1"
    warn "TLS 目录恢复或校验失败"
  fi
  if ! restore_path "${OLD_LDAP_PASSWORD_PRESENT}" "${ROLLBACK_DIR}/ldap-bind-password" "${LDAP_PASSWORD_FILE}"; then
    rollback_failed="1"
    warn "LDAP 查询账号密码文件恢复或校验失败"
  fi
  if ! restore_path "${OLD_LDAP_CA_PRESENT}" "${ROLLBACK_DIR}/ldap-ca.crt" "${LDAP_CA_FILE}"; then
    rollback_failed="1"
    warn "LDAP CA 文件恢复或校验失败"
  fi
  if ! restore_path "${OLD_CREDENTIALS_PRESENT}" "${ROLLBACK_DIR}/credentials" "${CREDENTIALS_FILE}"; then
    rollback_failed="1"
    warn "root 凭据文件恢复或校验失败"
  fi
  if ! restore_database; then
    rollback_failed="1"
    warn "SQLite 数据库快照恢复或校验失败"
  fi

  if ! systemctl daemon-reload >/dev/null 2>&1; then
    rollback_failed="1"
    warn "systemd daemon-reload 失败"
  fi
  if [[ "${OLD_SERVICE_ENABLED}" == "1" ]]; then
    systemctl enable "${APP_NAME}.service" >/dev/null 2>&1 || true
    if ! systemctl is-enabled --quiet "${APP_NAME}.service" 2>/dev/null; then
      rollback_failed="1"
      warn "旧服务的开机启用状态未恢复"
    fi
  else
    systemctl disable "${APP_NAME}.service" >/dev/null 2>&1 || true
    if systemctl is-enabled --quiet "${APP_NAME}.service" 2>/dev/null; then
      rollback_failed="1"
      warn "新服务仍处于开机启用状态"
    fi
  fi
  if [[ "${OLD_SERVICE_ACTIVE}" == "1" ]]; then
    if systemctl restart "${APP_NAME}.service" >/dev/null 2>&1 && \
       systemctl is-active --quiet "${APP_NAME}.service"; then
      warn "已切回旧版本并恢复服务"
    else
      rollback_failed="1"
      warn "旧版本文件已恢复，但服务未能重新启动；请立即查看 journalctl -u ${APP_NAME}"
    fi
  else
    systemctl stop "${APP_NAME}.service" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "${APP_NAME}.service"; then
      rollback_failed="1"
      warn "安装前服务为停止状态，但回滚后仍处于运行状态"
    fi
  fi

  if [[ "${rollback_failed}" == "0" && "${NEW_RELEASE_CREATED}" == "1" && -n "${NEW_RELEASE}" ]]; then
    if rm -rf -- "${NEW_RELEASE}" && [[ ! -e "${NEW_RELEASE}" ]]; then
      NEW_RELEASE_CREATED="0"
    else
      rollback_failed="1"
      warn "无法删除失败的新 release ${NEW_RELEASE}"
    fi
  fi
  ADMIN_PASSWORD=""
  TRANSACTION_ACTIVE="0"
  [[ "${rollback_failed}" == "0" ]]
}

on_exit() {
  local status=$?
  trap - EXIT
  if [[ "${status}" -ne 0 && "${TRANSACTION_ACTIVE}" == "1" ]]; then
    if ! rollback_install; then
      PRESERVE_WORK_DIR="1"
      rm -f -- "${WORK_DIR}/credentials" "${WORK_DIR}/server.env" >/dev/null 2>&1 || true
      warn "自动回滚不完整；唯一安装前备份已保留在 ${ROLLBACK_DIR}（目录权限 0700）"
      warn "请勿再次运行安装脚本，先按上述路径人工恢复并检查服务"
    fi
  elif [[ "${status}" -ne 0 && "${NEW_RELEASE_CREATED}" == "1" && -n "${NEW_RELEASE}" ]]; then
    if ! rm -rf -- "${NEW_RELEASE}"; then
      warn "无法清理未激活的 release ${NEW_RELEASE}"
    fi
  fi
  cleanup_files
  exit "${status}"
}

trap on_exit EXIT

detect_package_manager() {
  if command -v apt-get >/dev/null 2>&1; then
    echo "apt"
  elif command -v dnf >/dev/null 2>&1; then
    echo "dnf"
  elif command -v yum >/dev/null 2>&1; then
    echo "yum"
  else
    die "仅支持使用 apt、dnf 或 yum 的 Linux 发行版"
  fi
}

preflight_static() {
  [[ -f "${SERVER_SOURCE}/app.py" ]] || die "找不到 ${SERVER_SOURCE}/app.py，请从完整发布包内运行脚本"
  [[ -f "${SERVER_SOURCE}/requirements.txt" ]] || die "找不到 server/requirements.txt"
  [[ -f "${UI_SOURCE}" ]] || die "找不到 nginx-cluster-console.html，请保留发布包目录结构"
  [[ -r "${SERVER_SOURCE}/app.py" && -r "${SERVER_SOURCE}/requirements.txt" && -r "${UI_SOURCE}" ]] || \
    die "发布包文件不可读"

  [[ -z "${CERT_FILE}" && -z "${KEY_FILE}" ]] || \
    [[ -f "${CERT_FILE}" && -r "${CERT_FILE}" && -f "${KEY_FILE}" && -r "${KEY_FILE}" ]] || \
    die "--cert 和 --key 必须是 root 可读的普通文件"
  if [[ "${LDAP_CONFIG_PROVIDED}" == "1" ]]; then
    [[ -f "${LDAP_BIND_PASSWORD_SOURCE}" && ! -L "${LDAP_BIND_PASSWORD_SOURCE}" && -r "${LDAP_BIND_PASSWORD_SOURCE}" ]] || \
      die "--ldap-bind-password-file 必须是 root 可读且非符号链接的普通文件"
    [[ -z "${LDAP_CA_SOURCE}" || ( -f "${LDAP_CA_SOURCE}" && ! -L "${LDAP_CA_SOURCE}" && -r "${LDAP_CA_SOURCE}" ) ]] || \
      die "--ldap-ca-file 必须是 root 可读且非符号链接的普通文件"
  fi

  command -v systemctl >/dev/null 2>&1 || die "系统必须使用 systemd"
  command -v systemd-analyze >/dev/null 2>&1 || die "缺少 systemd-analyze"
  command -v flock >/dev/null 2>&1 || die "缺少 flock（通常由 util-linux 提供）"
  command -v runuser >/dev/null 2>&1 || die "缺少 runuser（通常由 util-linux 提供）"
  command -v install >/dev/null 2>&1 || die "缺少 install（通常由 coreutils 提供）"
  command -v sed >/dev/null 2>&1 || die "缺少 sed"
  command -v sha256sum >/dev/null 2>&1 || die "缺少 sha256sum（通常由 coreutils 提供）"
  detect_package_manager >/dev/null

  if [[ -e "${CURRENT_LINK}" && ! -L "${CURRENT_LINK}" ]]; then
    die "${CURRENT_LINK} 已存在但不是符号链接，请先人工确认旧目录"
  fi
  if [[ -L "${CURRENT_LINK}" && ! -d "${CURRENT_LINK}" ]]; then
    die "${CURRENT_LINK} 是失效链接，无法保证升级失败后安全回切"
  fi
  if [[ ( -e "${DB_FILE}" || -L "${DB_FILE}" ) && ( ! -f "${DB_FILE}" || -L "${DB_FILE}" ) ]]; then
    die "${DB_FILE} 必须是普通文件且不能是符号链接"
  fi
}

install_dependencies() {
  local manager
  manager="$(detect_package_manager)"
  log "检查并安装 Python、OpenSSL 和系统依赖"
  case "${manager}" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -y
      apt-get install -y python3 python3-venv python3-pip ca-certificates openssl diffutils
      ;;
    dnf)
      dnf install -y python3 python3-pip ca-certificates openssl diffutils
      ;;
    yum)
      yum install -y python3 python3-pip ca-certificates openssl diffutils
      ;;
  esac
}

check_runtime() {
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then
      dnf install -y python39 python39-pip >/dev/null 2>&1 || true
    elif command -v yum >/dev/null 2>&1; then
      yum install -y python39 python39-pip >/dev/null 2>&1 || true
    fi
    command -v python3.9 >/dev/null 2>&1 && PYTHON_BIN="python3.9"
  fi
  "${PYTHON_BIN}" - <<'PY' || die "需要 Python 3.9 或更高版本"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
}

validate_manager_host() {
  "${PYTHON_BIN}" - "${MANAGER_HOST}" <<'PY' || die "--host 必须是 IPv4 地址或合法 DNS 名称"
import ipaddress
import re
import sys

value = sys.argv[1]
try:
    address = ipaddress.ip_address(value)
    if address.version != 4:
        raise ValueError("IPv6 is not supported by this installer yet")
except ValueError:
    labels = value.split(".")
    if len(value) > 253 or not all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels
    ):
        raise SystemExit(1)
PY
}

validate_public_url() {
  "${PYTHON_BIN}" - "${PUBLIC_URL}" "${ALLOW_DIRECT_HTTP}" <<'PY' || \
    die "--public-url 必须是无账号、无查询参数的 HTTPS 地址；直连 HTTP 模式也可使用 HTTP 地址"
import sys
from urllib.parse import urlparse

value = urlparse(sys.argv[1])
allow_direct_http = sys.argv[2] == "1"
if value.scheme not in ({"http", "https"} if allow_direct_http else {"https"}):
    raise SystemExit(1)
if not value.hostname or value.username or value.password:
    raise SystemExit(1)
if value.query or value.fragment or value.path not in {"", "/"}:
    raise SystemExit(1)
PY
}

is_ip_address() {
  "${PYTHON_BIN}" - "$1" <<'PY'
import ipaddress
import sys
try:
    ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
PY
}

certificate_matches_host() {
  local certificate="$1"
  if is_ip_address "${MANAGER_HOST}"; then
    openssl x509 -in "${certificate}" -noout -checkip "${MANAGER_HOST}" >/dev/null 2>&1
  else
    openssl x509 -in "${certificate}" -noout -checkhost "${MANAGER_HOST}" >/dev/null 2>&1
  fi
}

validate_ldap_config() {
  [[ "${LDAP_CONFIG_PROVIDED}" == "1" ]] || return 0
  "${PYTHON_BIN}" - \
    "${LDAP_URL}" "${LDAP_BASE_DN}" "${LDAP_BIND_DN}" "${LDAP_USER_FILTER}" \
    "${LDAP_GROUP_ATTRIBUTE}" "${LDAP_GROUP_SEARCH_BASE}" "${LDAP_GROUP_FILTER}" \
    "${LDAP_ADMIN_GROUP}" "${LDAP_OPERATOR_GROUP}" "${LDAP_AUDITOR_GROUP}" "${LDAP_START_TLS}" <<'PY' || \
    die "LDAP 参数无效"
import re
import sys
from urllib.parse import urlparse

(url, base_dn, bind_dn, user_filter, group_attribute, group_search_base,
 group_filter, admin_group, operator_group, auditor_group, start_tls) = sys.argv[1:]
parsed = urlparse(url)
if parsed.scheme not in {"ldap", "ldaps"} or not parsed.hostname:
    raise SystemExit(1)
if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
    raise SystemExit(1)
if start_tls == "1" and parsed.scheme != "ldap":
    raise SystemExit(1)
if not base_dn.strip() or not bind_dn.strip() or "{username}" not in user_filter or len(user_filter) > 1024:
    raise SystemExit(1)
if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", group_attribute) is None:
    raise SystemExit(1)
if group_search_base and ("{user_dn}" not in group_filter or len(group_filter) > 1024):
    raise SystemExit(1)
if not any((admin_group, operator_group, auditor_group)):
    raise SystemExit(1)
PY
  "${PYTHON_BIN}" - "${LDAP_BIND_PASSWORD_SOURCE}" <<'PY' || die "LDAP 查询账号密码文件必须只包含一行非空密码"
import pathlib
import sys

value = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").rstrip("\r\n")
if not value or len(value) > 1024 or any(character in value for character in ("\0", "\n", "\r")):
    raise SystemExit(1)
PY
  if [[ -n "${LDAP_CA_SOURCE}" ]]; then
    openssl x509 -in "${LDAP_CA_SOURCE}" -noout >/dev/null 2>&1 || die "LDAP CA 不是有效的 PEM 证书"
  fi
}

preflight_runtime() {
  command -v openssl >/dev/null 2>&1 || die "安装依赖后仍找不到 openssl"
  command -v cmp >/dev/null 2>&1 || die "安装依赖后仍找不到 cmp"
  command -v diff >/dev/null 2>&1 || die "安装依赖后仍找不到 diff"
  command -v stat >/dev/null 2>&1 || die "安装依赖后仍找不到 stat"
  validate_manager_host
  validate_public_url
  validate_ldap_config

  if [[ -n "${CERT_FILE}" ]]; then
    openssl x509 -in "${CERT_FILE}" -noout >/dev/null 2>&1 || die "服务端证书不是有效 PEM 证书"
    openssl x509 -in "${CERT_FILE}" -noout -checkend 0 >/dev/null 2>&1 || die "服务端证书已经过期"
    openssl pkey -in "${KEY_FILE}" -passin pass: -noout >/dev/null 2>&1 || \
      die "服务端私钥无效或带口令；systemd 服务需要无口令 PEM 私钥"
    certificate_matches_host "${CERT_FILE}" || die "服务端证书不匹配 --host ${MANAGER_HOST}"

    local cert_key_digest private_key_digest
    cert_key_digest="$(openssl x509 -in "${CERT_FILE}" -pubkey -noout | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)"
    private_key_digest="$(openssl pkey -in "${KEY_FILE}" -passin pass: -pubout -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)"
    [[ -n "${cert_key_digest}" && "${cert_key_digest}" == "${private_key_digest}" ]] || \
      die "服务端证书与私钥不匹配"
  fi

  if ! systemctl is-active --quiet "${APP_NAME}.service"; then
    local bind_host="0.0.0.0"
    [[ "${BEHIND_NGINX}" == "1" && "${ALLOW_DIRECT_HTTP}" != "1" ]] && bind_host="127.0.0.1"
    "${PYTHON_BIN}" - "${LISTEN_PORT}" "${bind_host}" <<'PY' || die "端口 ${LISTEN_PORT} 已被其他进程占用"
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind((sys.argv[2], int(sys.argv[1])))
PY
  fi
}

ensure_identity() {
  getent group "${APP_GROUP}" >/dev/null 2>&1 || groupadd --system "${APP_GROUP}"
  if ! id "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --gid "${APP_GROUP}" --home-dir "${DATA_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
  fi
  install -d -m 0755 -o root -g root "${APP_ROOT}" "${RELEASES_DIR}"
  install -d -m 0750 -o "${APP_USER}" -g "${APP_GROUP}" "${DATA_DIR}"
  install -d -m 0750 -o root -g "${APP_GROUP}" "${ETC_DIR}"
}

prepare_release() {
  local source_digest release_id import_db
  source_digest="$("${PYTHON_BIN}" - "${SERVER_SOURCE}" "${UI_SOURCE}" <<'PY'
import hashlib
import pathlib
import sys

server = pathlib.Path(sys.argv[1])
files = sorted(path for path in server.iterdir() if path.suffix == ".py" or path.name == "requirements.txt")
files.append(pathlib.Path(sys.argv[2]))
digest = hashlib.sha256()
for path in files:
    digest.update(path.name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest()[:12])
PY
)"
  release_id="$(date -u +'%Y%m%dT%H%M%SZ')-${source_digest}"
  NEW_RELEASE="${RELEASES_DIR}/${release_id}"
  if [[ -e "${NEW_RELEASE}" ]]; then
    NEW_RELEASE="${NEW_RELEASE}-$$"
  fi
  STAGING_DIR="${RELEASES_DIR}/.${release_id}.$$.staging"
  install -d -m 0755 "${STAGING_DIR}/server" "${STAGING_DIR}/ui"

  find "${SERVER_SOURCE}" -maxdepth 1 -type f \( -name '*.py' -o -name 'requirements.txt' \) \
    -exec install -m 0644 {} "${STAGING_DIR}/server/" \;
  install -m 0644 "${UI_SOURCE}" "${STAGING_DIR}/ui/index.html"

  "${PYTHON_BIN}" -m venv "${STAGING_DIR}/venv"
  "${STAGING_DIR}/venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip wheel
  "${STAGING_DIR}/venv/bin/python" -m pip install --disable-pip-version-check \
    -r "${STAGING_DIR}/server/requirements.txt"
  "${STAGING_DIR}/venv/bin/python" -m pip check
  "${STAGING_DIR}/venv/bin/python" -m compileall -q "${STAGING_DIR}/server"

  import_db="${WORK_DIR}/release-import.db"
  NGINX_MANAGER_DB_PATH="${import_db}" \
  NGINX_MANAGER_UI_PATH="${STAGING_DIR}/ui/index.html" \
  PYTHONPATH="${STAGING_DIR}/server" \
    "${STAGING_DIR}/venv/bin/python" -c 'import app; assert app.app is not None' \
    </dev/null
  rm -f -- "${import_db}" "${import_db}-wal" "${import_db}-shm"

  # `umask 027` makes directories created by `python -m venv` mode 0750.
  # They must therefore belong to the service group; root:root would prevent
  # nginx-manager from traversing the venv and systemd would fail with 203/EXEC.
  chown -R root:"${APP_GROUP}" "${STAGING_DIR}"
  find "${STAGING_DIR}" -type d -exec chmod 0750 {} +
  find "${STAGING_DIR}" -type f -perm /111 -exec chmod 0750 {} +
  find "${STAGING_DIR}" -type f ! -perm /111 -exec chmod 0640 {} +
  mv -- "${STAGING_DIR}" "${NEW_RELEASE}"
  STAGING_DIR=""
  NEW_RELEASE_CREATED="1"

  # Validate the same executable and read path as the systemd service, under
  # the actual unprivileged identity, before any live symlink or unit is changed.
  runuser -u "${APP_USER}" -- "${NEW_RELEASE}/venv/bin/python" \
    - "${NEW_RELEASE}/server/app.py" <<'PY'
import sys
import uvicorn

with open(sys.argv[1], "rb") as handle:
    handle.read(1)
PY
  log "新版本已预装到 ${NEW_RELEASE}"
}

prepare_candidates() {
  local verify_dir verify_service exec_start ldap_status="未启用"
  ADMIN_PASSWORD="$(openssl rand -hex 24)"

  cat >"${WORK_DIR}/server.env" <<EOF
NGINX_MANAGER_DB_PATH=${DATA_DIR}/manager.db
NGINX_MANAGER_UI_PATH=${CURRENT_LINK}/ui/index.html
NGINX_MANAGER_SESSION_TTL_SECONDS=28800
NGINX_MANAGER_ENROLLMENT_PENDING_TTL_SECONDS=86400
EOF
  if [[ "${LDAP_DISABLED}" != "1" && "${LDAP_CONFIG_PROVIDED}" == "1" ]]; then
    append_environment "NGINX_MANAGER_LDAP_ENABLED" "true"
    append_environment "NGINX_MANAGER_LDAP_URL" "${LDAP_URL}"
    append_environment "NGINX_MANAGER_LDAP_BASE_DN" "${LDAP_BASE_DN}"
    append_environment "NGINX_MANAGER_LDAP_BIND_DN" "${LDAP_BIND_DN}"
    append_environment "NGINX_MANAGER_LDAP_BIND_PASSWORD_FILE" "${LDAP_PASSWORD_FILE}"
    append_environment "NGINX_MANAGER_LDAP_USER_FILTER" "${LDAP_USER_FILTER}"
    append_environment "NGINX_MANAGER_LDAP_GROUP_ATTRIBUTE" "${LDAP_GROUP_ATTRIBUTE}"
    append_environment "NGINX_MANAGER_LDAP_GROUP_FILTER" "${LDAP_GROUP_FILTER}"
    append_environment "NGINX_MANAGER_LDAP_ADMIN_GROUP" "${LDAP_ADMIN_GROUP}"
    append_environment "NGINX_MANAGER_LDAP_OPERATOR_GROUP" "${LDAP_OPERATOR_GROUP}"
    append_environment "NGINX_MANAGER_LDAP_AUDITOR_GROUP" "${LDAP_AUDITOR_GROUP}"
    append_environment "NGINX_MANAGER_LDAP_START_TLS" "${LDAP_START_TLS}"
    if [[ -n "${LDAP_GROUP_SEARCH_BASE}" ]]; then
      append_environment "NGINX_MANAGER_LDAP_GROUP_SEARCH_BASE" "${LDAP_GROUP_SEARCH_BASE}"
    fi
    if [[ -n "${LDAP_CA_SOURCE}" ]]; then
      append_environment "NGINX_MANAGER_LDAP_CA_FILE" "${LDAP_CA_FILE}"
      install -m 0600 "${LDAP_CA_SOURCE}" "${WORK_DIR}/ldap-ca.crt"
    fi
    install -m 0600 "${LDAP_BIND_PASSWORD_SOURCE}" "${WORK_DIR}/ldap-bind-password"
    ldap_status="已启用"
  elif [[ "${LDAP_DISABLED}" != "1" && -f "${ENV_FILE}" ]]; then
    if grep -E '^NGINX_MANAGER_LDAP_[A-Z0-9_]+=' "${ENV_FILE}" >>"${WORK_DIR}/server.env"; then
      ldap_status="保留现有配置"
    fi
  fi
  chmod 0640 "${WORK_DIR}/server.env"

  if [[ "${BEHIND_NGINX}" == "1" ]]; then
    local bind_host="127.0.0.1"
    [[ "${ALLOW_DIRECT_HTTP}" == "1" ]] && bind_host="0.0.0.0"
    exec_start="${CURRENT_LINK}/venv/bin/python -m uvicorn app:app --host ${bind_host} --port ${LISTEN_PORT} --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-server-header"
  else
    exec_start="${CURRENT_LINK}/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port ${LISTEN_PORT} --ssl-keyfile ${TLS_DIR}/server.key --ssl-certfile ${TLS_DIR}/server.crt --no-server-header"
  fi

  cat >"${WORK_DIR}/${APP_NAME}.service" <<EOF
[Unit]
Description=Lightweight Nginx Manager Control Plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${CURRENT_LINK}/server
EnvironmentFile=${ENV_FILE}
# The release directory is renamed after its venv is built, so console-script
# shebangs still contain the temporary staging path. Starting the module through
# the venv interpreter is relocatable and also works on hardened script policies.
ExecStart=${exec_start}
Restart=on-failure
RestartSec=3s
TimeoutStopSec=15s
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=${DATA_DIR}
ReadOnlyPaths=${APP_ROOT} ${ETC_DIR}
TasksMax=128
LimitNOFILE=4096
MemoryMax=512M

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "${WORK_DIR}/${APP_NAME}.service"

  local tls_mode="控制端直连 TLS"
  if [[ "${BEHIND_NGINX}" == "1" ]]; then
    tls_mode="由本机 Nginx 反向代理提供"
    [[ "${ALLOW_DIRECT_HTTP}" == "1" ]] && tls_mode="本机 Nginx HTTPS + 可信内网直连 HTTP（未加密）"
  fi
  cat >"${WORK_DIR}/credentials" <<EOF
管理地址=${PUBLIC_URL}
管理员账号=${ADMIN_USERNAME}
管理员密码=${ADMIN_PASSWORD}
LDAP状态=${ldap_status}
Agent 接入方式=安装后在 Web 页面审批，无需注册令牌
TLS模式=${tls_mode}
EOF
  if [[ "${ALLOW_DIRECT_HTTP}" == "1" ]]; then
    echo "直连HTTP地址=http://${MANAGER_HOST}:${LISTEN_PORT}" >>"${WORK_DIR}/credentials"
  fi
  chmod 0600 "${WORK_DIR}/credentials"

  # On a first install, /opt/nginx-manager/current intentionally does not exist
  # until the transaction is activated. Verify an otherwise identical unit
  # against the already-prepared immutable release instead of rejecting the
  # valid service because its future `current` target is not live yet.
  verify_dir="${WORK_DIR}/verify-unit"
  verify_service="${verify_dir}/${APP_NAME}.service"
  install -d -m 0700 "${verify_dir}"
  sed "s|${CURRENT_LINK}|${NEW_RELEASE}|g" \
    "${WORK_DIR}/${APP_NAME}.service" >"${verify_service}"
  chmod 0600 "${verify_service}"
  systemd-analyze verify "${verify_service}" >/dev/null
  rm -rf -- "${verify_dir}"
}

begin_transaction() {
  ROLLBACK_DIR="${WORK_DIR}/rollback"
  install -d -m 0700 "${ROLLBACK_DIR}"

  if [[ -L "${CURRENT_LINK}" ]]; then
    OLD_CURRENT_PRESENT="1"
    OLD_CURRENT_TARGET="$(readlink "${CURRENT_LINK}")"
  fi
  if [[ -f "${SERVICE_FILE}" ]]; then
    OLD_SERVICE_PRESENT="1"
    cp -a -- "${SERVICE_FILE}" "${ROLLBACK_DIR}/service"
  fi
  if [[ -f "${ENV_FILE}" ]]; then
    OLD_ENV_PRESENT="1"
    cp -a -- "${ENV_FILE}" "${ROLLBACK_DIR}/environment"
    # Never resurrect the deprecated plaintext bootstrap token when an older
    # installation is restored. The control plane does not need it at runtime.
    sanitize_environment_file "${ROLLBACK_DIR}/environment"
  fi
  if [[ -d "${TLS_DIR}" ]]; then
    OLD_TLS_PRESENT="1"
    cp -a -- "${TLS_DIR}" "${ROLLBACK_DIR}/tls"
  fi
  if [[ -f "${LDAP_PASSWORD_FILE}" ]]; then
    OLD_LDAP_PASSWORD_PRESENT="1"
    cp -a -- "${LDAP_PASSWORD_FILE}" "${ROLLBACK_DIR}/ldap-bind-password"
  fi
  if [[ -f "${LDAP_CA_FILE}" ]]; then
    OLD_LDAP_CA_PRESENT="1"
    cp -a -- "${LDAP_CA_FILE}" "${ROLLBACK_DIR}/ldap-ca.crt"
  fi
  if [[ -f "${CREDENTIALS_FILE}" ]]; then
    OLD_CREDENTIALS_PRESENT="1"
    cp -a -- "${CREDENTIALS_FILE}" "${ROLLBACK_DIR}/credentials"
  fi
  if systemctl is-active --quiet "${APP_NAME}.service"; then
    OLD_SERVICE_ACTIVE="1"
  fi
  if systemctl is-enabled --quiet "${APP_NAME}.service" 2>/dev/null; then
    OLD_SERVICE_ENABLED="1"
  fi
  TRANSACTION_ACTIVE="1"
  if [[ "${OLD_SERVICE_ACTIVE}" == "1" ]]; then
    log "release 已预装完成，短暂停止旧服务并创建一致性数据库快照"
    systemctl stop "${APP_NAME}.service"
    systemctl is-active --quiet "${APP_NAME}.service" && die "旧服务未能停止，拒绝继续升级"
  fi
  snapshot_database
}

install_tls() {
  if [[ "${BEHIND_NGINX}" == "1" ]]; then
    rm -rf -- "${TLS_DIR}"
    return
  fi
  install -d -m 0750 -o root -g "${APP_GROUP}" "${TLS_DIR}"

  if [[ -n "${CERT_FILE}" ]]; then
    install -m 0640 -o root -g "${APP_GROUP}" "${CERT_FILE}" "${TLS_DIR}/server.crt"
    install -m 0640 -o root -g "${APP_GROUP}" "${KEY_FILE}" "${TLS_DIR}/server.key"
    rm -f -- "${TLS_DIR}/ca.key" "${TLS_DIR}/ca.crt" "${TLS_DIR}/ca.srl"
    return
  fi

  if [[ -s "${TLS_DIR}/server.crt" && -s "${TLS_DIR}/server.key" && \
        -s "${TLS_DIR}/ca.crt" && -s "${TLS_DIR}/ca.key" ]] && \
     openssl x509 -in "${TLS_DIR}/server.crt" -noout -checkend 86400 >/dev/null 2>&1 && \
     openssl pkey -in "${TLS_DIR}/server.key" -passin pass: -noout >/dev/null 2>&1 && \
     certificate_matches_host "${TLS_DIR}/server.crt"; then
    log "保留现有本地 CA 与 TLS 证书"
    return
  fi

  log "生成本地 CA 与服务端证书"
  local san ext_file
  if is_ip_address "${MANAGER_HOST}"; then
    san="IP:${MANAGER_HOST}"
  else
    san="DNS:${MANAGER_HOST}"
  fi
  ext_file="${WORK_DIR}/server-cert.ext"
  cat >"${ext_file}" <<EOF
subjectAltName=${san}
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF

  local ca_cert_digest="" ca_key_digest=""
  if [[ -s "${TLS_DIR}/ca.crt" && -s "${TLS_DIR}/ca.key" ]] && \
     openssl x509 -in "${TLS_DIR}/ca.crt" -noout -checkend 86400 >/dev/null 2>&1 && \
     openssl pkey -in "${TLS_DIR}/ca.key" -passin pass: -noout >/dev/null 2>&1; then
    ca_cert_digest="$(openssl x509 -in "${TLS_DIR}/ca.crt" -pubkey -noout | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)"
    ca_key_digest="$(openssl pkey -in "${TLS_DIR}/ca.key" -passin pass: -pubout -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)"
  fi
  if [[ -z "${ca_cert_digest}" || "${ca_cert_digest}" != "${ca_key_digest}" ]]; then
    rm -f -- "${TLS_DIR}/ca.key" "${TLS_DIR}/ca.crt"
    openssl genrsa -out "${TLS_DIR}/ca.key" 3072 >/dev/null 2>&1
    openssl req -x509 -new -sha256 -days 3650 \
      -key "${TLS_DIR}/ca.key" \
      -subj "/CN=Nginx Manager Local CA" \
      -out "${TLS_DIR}/ca.crt"
  fi
  openssl genrsa -out "${TLS_DIR}/server.key" 2048 >/dev/null 2>&1
  openssl req -new -sha256 \
    -key "${TLS_DIR}/server.key" \
    -subj "/CN=${MANAGER_HOST}" \
    -out "${WORK_DIR}/server.csr"
  openssl x509 -req -sha256 -days 825 \
    -in "${WORK_DIR}/server.csr" \
    -CA "${TLS_DIR}/ca.crt" \
    -CAkey "${TLS_DIR}/ca.key" \
    -CAcreateserial \
    -extfile "${ext_file}" \
    -out "${TLS_DIR}/server.crt" >/dev/null 2>&1
  rm -f -- "${TLS_DIR}/ca.srl"
  chown root:"${APP_GROUP}" "${TLS_DIR}/server.crt" "${TLS_DIR}/server.key"
  chmod 0640 "${TLS_DIR}/server.crt" "${TLS_DIR}/server.key"
  chown root:root "${TLS_DIR}/ca.crt" "${TLS_DIR}/ca.key"
  chmod 0644 "${TLS_DIR}/ca.crt"
  chmod 0600 "${TLS_DIR}/ca.key"
}

install_ldap_config() {
  if [[ "${LDAP_DISABLED}" == "1" ]]; then
    rm -f -- "${LDAP_PASSWORD_FILE}" "${LDAP_CA_FILE}"
    return
  fi
  [[ "${LDAP_CONFIG_PROVIDED}" == "1" ]] || return 0
  install -m 0640 -o root -g "${APP_GROUP}" "${WORK_DIR}/ldap-bind-password" "${LDAP_PASSWORD_FILE}"
  if [[ -f "${WORK_DIR}/ldap-ca.crt" ]]; then
    install -m 0644 -o root -g root "${WORK_DIR}/ldap-ca.crt" "${LDAP_CA_FILE}"
  else
    rm -f -- "${LDAP_CA_FILE}"
  fi
}

health_check() {
  if [[ "${BEHIND_NGINX}" == "1" ]]; then
    "${PYTHON_BIN}" - "${LISTEN_PORT}" <<'PY'
import socket
import sys
import time

port = int(sys.argv[1])
last_error = None
for _attempt in range(15):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            connection.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            response = connection.recv(512)
        if b" 200 " in response.split(b"\r\n", 1)[0]:
            raise SystemExit(0)
        last_error = RuntimeError("health endpoint did not return HTTP 200")
    except Exception as exc:
        last_error = exc
        time.sleep(1)
print("health check failed: {}".format(last_error), file=sys.stderr)
raise SystemExit(1)
PY
    return
  fi
  "${PYTHON_BIN}" - "${MANAGER_HOST}" "${LISTEN_PORT}" "${TLS_DIR}/server.crt" <<'PY'
import hashlib
import re
import socket
import ssl
import sys
import time

host, port_text, certificate_path = sys.argv[1:]
with open(certificate_path, "r", encoding="ascii") as handle:
    pem = handle.read()
match = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.S)
if not match:
    raise SystemExit("installed TLS certificate is not PEM")
expected_der = ssl.PEM_cert_to_DER_cert(match.group(0))
expected_digest = hashlib.sha256(expected_der).digest()
context = ssl._create_unverified_context()
last_error = None

for _attempt in range(15):
    try:
        with socket.create_connection(("127.0.0.1", int(port_text)), timeout=2) as raw:
            with context.wrap_socket(raw, server_hostname=host) as connection:
                actual_digest = hashlib.sha256(connection.getpeercert(binary_form=True)).digest()
                if actual_digest != expected_digest:
                    raise RuntimeError("listener presented an unexpected TLS certificate")
                connection.sendall(
                    ("GET /healthz HTTP/1.1\r\nHost: " + host + "\r\nConnection: close\r\n\r\n").encode()
                )
                response = connection.recv(512)
        if b" 200 " in response.split(b"\r\n", 1)[0]:
            raise SystemExit(0)
        last_error = RuntimeError("health endpoint did not return HTTP 200")
    except Exception as exc:
        last_error = exc
        time.sleep(1)

print("health check failed: {}".format(last_error), file=sys.stderr)
raise SystemExit(1)
PY
}

bootstrap_admin() {
  local outcome
  outcome="$(
    runuser -u "${APP_USER}" -- env \
      NGINX_MANAGER_DB_PATH="${DATA_DIR}/manager.db" \
      NGINX_MANAGER_BOOTSTRAP_USERNAME="${ADMIN_USERNAME}" \
      NGINX_MANAGER_BOOTSTRAP_PASSWORD="${ADMIN_PASSWORD}" \
      "${CURRENT_LINK}/venv/bin/python" "${CURRENT_LINK}/server/app.py" bootstrap-admin
  )"
  ADMIN_CREATED="$("${PYTHON_BIN}" - "${outcome}" <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
print("1" if value.get("created") else "0")
PY
)"
  if [[ "${ADMIN_CREATED}" == "1" ]]; then
    install -m 0600 -o root -g root "${WORK_DIR}/credentials" "${CREDENTIALS_FILE}"
    log "已创建首个 Web 管理账号 ${ADMIN_USERNAME}"
  else
    log "数据库中已有 Web 管理账号，保留现有账号和密码"
    if [[ ! -f "${CREDENTIALS_FILE}" ]]; then
      warn "数据库已有管理员，但 ${CREDENTIALS_FILE} 不存在；请使用既有密码登录"
    fi
  fi
  ADMIN_PASSWORD=""
}

activate_release() {
  install_tls
  install_ldap_config
  install -m 0640 -o root -g "${APP_GROUP}" "${WORK_DIR}/server.env" "${ENV_FILE}"
  atomic_set_current "${NEW_RELEASE}"
  install -m 0644 -o root -g root "${WORK_DIR}/${APP_NAME}.service" "${SERVICE_FILE}"
  bootstrap_admin

  systemctl daemon-reload
  systemctl enable "${APP_NAME}.service" >/dev/null
  systemctl reset-failed "${APP_NAME}.service" >/dev/null 2>&1 || true
  log "显式重启控制端并验证新版本"
  systemctl restart "${APP_NAME}.service"
  systemctl is-active --quiet "${APP_NAME}.service" || {
    journalctl -u "${APP_NAME}.service" -n 80 --no-pager >&2 || true
    return 1
  }
  health_check || {
    journalctl -u "${APP_NAME}.service" -n 80 --no-pager >&2 || true
    return 1
  }
}

configure_firewall() {
  [[ "${BEHIND_NGINX}" != "1" || "${ALLOW_DIRECT_HTTP}" == "1" ]] || return 0
  [[ "${OPEN_FIREWALL}" == "1" ]] || return 0
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
    if [[ -n "${FIREWALL_CIDR}" ]]; then
      ufw allow from "${FIREWALL_CIDR}" to any port "${LISTEN_PORT}" proto tcp comment 'nginx-manager' >/dev/null
    else
      ufw allow "${LISTEN_PORT}/tcp" comment 'nginx-manager' >/dev/null
    fi
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    if [[ -n "${FIREWALL_CIDR}" ]]; then
      firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${FIREWALL_CIDR} port port=${LISTEN_PORT} protocol=tcp accept" >/dev/null
    else
      firewall-cmd --permanent --add-port="${LISTEN_PORT}/tcp" >/dev/null
    fi
    firewall-cmd --reload >/dev/null
  else
    warn "未发现已启用的 ufw/firewalld，未自动放行端口"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      [[ $# -ge 2 ]] || die "--host 缺少值"
      MANAGER_HOST="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || die "--port 缺少值"
      LISTEN_PORT="$2"
      shift 2
      ;;
    --cert)
      [[ $# -ge 2 ]] || die "--cert 缺少值"
      CERT_FILE="$2"
      shift 2
      ;;
    --key)
      [[ $# -ge 2 ]] || die "--key 缺少值"
      KEY_FILE="$2"
      shift 2
      ;;
    --self-signed)
      SELF_SIGNED="1"
      shift
      ;;
    --behind-nginx)
      BEHIND_NGINX="1"
      shift
      ;;
    --allow-direct-http)
      ALLOW_DIRECT_HTTP="1"
      shift
      ;;
    --public-url)
      [[ $# -ge 2 ]] || die "--public-url 缺少值"
      PUBLIC_URL="$2"
      shift 2
      ;;
    --ldap-url)
      [[ $# -ge 2 ]] || die "--ldap-url 缺少值"
      LDAP_URL="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --ldap-base-dn)
      [[ $# -ge 2 ]] || die "--ldap-base-dn 缺少值"
      LDAP_BASE_DN="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --ldap-bind-dn)
      [[ $# -ge 2 ]] || die "--ldap-bind-dn 缺少值"
      LDAP_BIND_DN="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --ldap-bind-password-file)
      [[ $# -ge 2 ]] || die "--ldap-bind-password-file 缺少值"
      LDAP_BIND_PASSWORD_SOURCE="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --ldap-user-filter)
      [[ $# -ge 2 ]] || die "--ldap-user-filter 缺少值"
      LDAP_USER_FILTER="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --ldap-group-attribute)
      [[ $# -ge 2 ]] || die "--ldap-group-attribute 缺少值"
      LDAP_GROUP_ATTRIBUTE="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --ldap-group-search-base)
      [[ $# -ge 2 ]] || die "--ldap-group-search-base 缺少值"
      LDAP_GROUP_SEARCH_BASE="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --ldap-group-filter)
      [[ $# -ge 2 ]] || die "--ldap-group-filter 缺少值"
      LDAP_GROUP_FILTER="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --ldap-admin-group)
      [[ $# -ge 2 ]] || die "--ldap-admin-group 缺少值"
      LDAP_ADMIN_GROUP="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --ldap-operator-group)
      [[ $# -ge 2 ]] || die "--ldap-operator-group 缺少值"
      LDAP_OPERATOR_GROUP="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --ldap-auditor-group)
      [[ $# -ge 2 ]] || die "--ldap-auditor-group 缺少值"
      LDAP_AUDITOR_GROUP="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --ldap-start-tls)
      LDAP_START_TLS="1"
      LDAP_CONFIG_PROVIDED="1"
      shift
      ;;
    --ldap-ca-file)
      [[ $# -ge 2 ]] || die "--ldap-ca-file 缺少值"
      LDAP_CA_SOURCE="$2"
      LDAP_CONFIG_PROVIDED="1"
      shift 2
      ;;
    --disable-ldap)
      LDAP_DISABLED="1"
      shift
      ;;
    --open-firewall)
      OPEN_FIREWALL="1"
      shift
      ;;
    --allow-cidr)
      [[ $# -ge 2 ]] || die "--allow-cidr 缺少值"
      FIREWALL_CIDR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "未知参数：$1"
      ;;
  esac
done

require_root
[[ -n "${MANAGER_HOST}" ]] || { usage; die "必须指定 --host"; }
valid_port "${LISTEN_PORT}" || die "端口必须是 1024-65535 的整数"
[[ -z "${CERT_FILE}" && -z "${KEY_FILE}" ]] || \
  [[ -n "${CERT_FILE}" && -n "${KEY_FILE}" ]] || die "--cert 和 --key 必须同时指定"
[[ "${SELF_SIGNED}" != "1" || ( -z "${CERT_FILE}" && -z "${KEY_FILE}" ) ]] || \
  die "--self-signed 不能与 --cert/--key 同时使用"
if [[ "${BEHIND_NGINX}" == "1" ]]; then
  [[ "${SELF_SIGNED}" == "0" && -z "${CERT_FILE}" && -z "${KEY_FILE}" ]] || \
    die "--behind-nginx 不能与 --self-signed 或 --cert/--key 同时使用"
  if [[ "${ALLOW_DIRECT_HTTP}" != "1" ]]; then
    [[ "${OPEN_FIREWALL}" == "0" && -z "${FIREWALL_CIDR}" ]] || \
      die "纯反代模式仅监听 127.0.0.1，不能使用 --open-firewall/--allow-cidr"
    [[ -n "${PUBLIC_URL}" ]] || PUBLIC_URL="https://${MANAGER_HOST}"
  else
    [[ -n "${PUBLIC_URL}" ]] || PUBLIC_URL="http://${MANAGER_HOST}:${LISTEN_PORT}"
  fi
else
  [[ "${ALLOW_DIRECT_HTTP}" != "1" ]] || die "--allow-direct-http 必须与 --behind-nginx 配合使用"
  [[ "${SELF_SIGNED}" == "1" || ( -n "${CERT_FILE}" && -n "${KEY_FILE}" ) ]] || \
    die "直连模式请指定 --self-signed，或同时提供 --cert 与 --key"
  [[ -n "${PUBLIC_URL}" ]] || PUBLIC_URL="https://${MANAGER_HOST}:${LISTEN_PORT}"
fi
[[ -z "${FIREWALL_CIDR}" || "${OPEN_FIREWALL}" == "1" ]] || \
  die "--allow-cidr 必须与 --open-firewall 一起使用"
[[ "${LDAP_DISABLED}" != "1" || "${LDAP_CONFIG_PROVIDED}" != "1" ]] || \
  die "--disable-ldap 不能与其他 LDAP 参数同时使用"
if [[ "${LDAP_CONFIG_PROVIDED}" == "1" ]]; then
  [[ -n "${LDAP_URL}" && -n "${LDAP_BASE_DN}" && -n "${LDAP_BIND_DN}" && -n "${LDAP_BIND_PASSWORD_SOURCE}" ]] || \
    die "启用 LDAP 至少需要 --ldap-url、--ldap-base-dn、--ldap-bind-dn 和 --ldap-bind-password-file"
fi

install -d -m 0755 /run/lock
exec 9>/run/lock/nginx-manager-install.lock
flock -n 9 || die "另一个安装或升级进程正在运行"
refuse_incomplete_transaction
WORK_DIR="$(mktemp -d /var/tmp/nginx-manager-install.XXXXXX)"
chmod 0700 "${WORK_DIR}"

log "执行发布包、参数和运行环境预检"
preflight_static
install_dependencies
check_runtime
preflight_runtime
ensure_identity
prepare_release
prepare_candidates

begin_transaction
activate_release

# The health check passed and the database/user migration is now authoritative.
TRANSACTION_ACTIVE="0"
NEW_RELEASE_CREATED="0"
if ! rm -rf -- "${ROLLBACK_DIR}"; then
  warn "服务已成功切换，但安装前临时备份未能清理：${ROLLBACK_DIR}"
fi
ROLLBACK_DIR=""

if ! configure_firewall; then
  warn "控制端已部署成功，但自动配置防火墙失败；请人工放行 ${LISTEN_PORT}/tcp"
fi

log "部署完成；当前版本：$(readlink -f "${CURRENT_LINK}")"
echo
echo "管理地址：${PUBLIC_URL}"
if [[ "${BEHIND_NGINX}" == "1" ]]; then
  if [[ "${ALLOW_DIRECT_HTTP}" == "1" ]]; then
    echo "直连 HTTP：http://${MANAGER_HOST}:${LISTEN_PORT}（未加密，仅建议可信内网使用）"
    echo "Nginx 后端：http://127.0.0.1:${LISTEN_PORT}"
  else
    echo "本机后端：http://127.0.0.1:${LISTEN_PORT}（仅供本机 Nginx 反向代理）"
  fi
  echo "Nginx 示例：${PACKAGE_DIR}/deploy/nginx-manager-proxy.conf.example"
fi
echo "凭据文件：${CREDENTIALS_FILE}（权限 600）"
if [[ -f "${TLS_DIR}/ca.crt" ]]; then
  echo "Agent 需要的 CA：${TLS_DIR}/ca.crt"
  echo -n "CA SHA-256："
  openssl x509 -in "${TLS_DIR}/ca.crt" -noout -fingerprint -sha256 | cut -d= -f2
fi
echo "当前版本：${CURRENT_LINK} -> $(readlink "${CURRENT_LINK}")"
echo "服务状态：systemctl status ${APP_NAME}"
echo "服务日志：journalctl -u ${APP_NAME} -f"
