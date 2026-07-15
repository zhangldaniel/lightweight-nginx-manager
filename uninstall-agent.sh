#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY="zhangldaniel/lightweight-nginx-manager"
REF="${NGINX_MANAGER_REF:-main}"

die() {
  echo "[nginx-manager-agent-uninstall] 错误：$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || die "请通过 sudo 或 root 运行"
[[ -n "${REF}" && ! "${REF}" =~ [^A-Za-z0-9._/-] ]] || die "NGINX_MANAGER_REF 包含非法字符"
command -v curl >/dev/null 2>&1 || die "系统缺少 curl"
command -v tar >/dev/null 2>&1 || die "系统缺少 tar"

WORK_DIR="$(mktemp -d /tmp/nginx-manager-agent-uninstall.XXXXXX)"
trap 'rm -rf -- "${WORK_DIR}"' EXIT
umask 077

ARCHIVE="${WORK_DIR}/source.tar.gz"
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
  --output "${ARCHIVE}" "https://codeload.github.com/${REPOSITORY}/tar.gz/${REF}"
TOP_LEVEL="$(tar -tzf "${ARCHIVE}" | sed -n '1{s#/.*##;p}')"
[[ -n "${TOP_LEVEL}" && "${TOP_LEVEL}" != "." && "${TOP_LEVEL}" != ".." && "${TOP_LEVEL}" != */* ]] \
  || die "GitHub 归档结构异常"
tar -xzf "${ARCHIVE}" -C "${WORK_DIR}" --no-same-owner

UNINSTALLER="${WORK_DIR}/${TOP_LEVEL}/deploy/uninstall-agent.sh"
[[ -f "${UNINSTALLER}" ]] || die "归档中缺少 deploy/uninstall-agent.sh"
bash "${UNINSTALLER}" "$@"
