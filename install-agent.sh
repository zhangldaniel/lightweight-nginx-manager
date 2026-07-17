#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY="zhangldaniel/lightweight-nginx-manager"
REF="${NGINX_MANAGER_REF:-main}"
ARCHIVE_SHA256="${NGINX_MANAGER_ARCHIVE_SHA256:-}"
REQUIRE_PINNED_REF="${NGINX_MANAGER_REQUIRE_PINNED_REF:-0}"

die() {
  echo "[nginx-manager-agent-bootstrap] 错误：$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || die "请通过 sudo 或 root 运行"
[[ -n "${REF}" && ! "${REF}" =~ [^A-Za-z0-9._/-] ]] || die "NGINX_MANAGER_REF 包含非法字符"
command -v curl >/dev/null 2>&1 || die "系统缺少 curl"
command -v tar >/dev/null 2>&1 || die "系统缺少 tar"
if [[ "${REQUIRE_PINNED_REF}" == "1" && ! "${REF}" =~ ^[0-9a-f]{40}$ && -z "${ARCHIVE_SHA256}" ]]; then
  die "生产安全模式要求 40 位 commit 或 NGINX_MANAGER_ARCHIVE_SHA256"
fi

WORK_DIR="$(mktemp -d /tmp/nginx-manager-agent-bootstrap.XXXXXX)"
trap 'rm -rf -- "${WORK_DIR}"' EXIT
umask 077

ARCHIVE="${WORK_DIR}/source.tar.gz"
DOWNLOAD_URL="https://codeload.github.com/${REPOSITORY}/tar.gz/${REF}"
echo "[nginx-manager-agent-bootstrap] 正在从 GitHub 下载 ${REPOSITORY}@${REF}"
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
  --output "${ARCHIVE}" "${DOWNLOAD_URL}"
if [[ -n "${ARCHIVE_SHA256}" ]]; then
  [[ "${ARCHIVE_SHA256}" =~ ^[0-9a-fA-F]{64}$ ]] || die "NGINX_MANAGER_ARCHIVE_SHA256 格式无效"
  command -v sha256sum >/dev/null 2>&1 || die "系统缺少 sha256sum"
  printf '%s  %s\n' "${ARCHIVE_SHA256,,}" "${ARCHIVE}" | sha256sum -c - >/dev/null \
    || die "GitHub 归档 SHA-256 校验失败"
elif [[ "${REF}" == "main" ]]; then
  echo "[nginx-manager-agent-bootstrap] 警告：正在使用可变 main；生产环境请固定 commit 并启用校验" >&2
fi

TOP_LEVEL="$(tar -tzf "${ARCHIVE}" | sed -n '1{s#/.*##;p}')"
[[ -n "${TOP_LEVEL}" && "${TOP_LEVEL}" != "." && "${TOP_LEVEL}" != ".." && "${TOP_LEVEL}" != */* ]] \
  || die "GitHub 归档结构异常"
tar -xzf "${ARCHIVE}" -C "${WORK_DIR}" --no-same-owner

INSTALLER="${WORK_DIR}/${TOP_LEVEL}/deploy/install-agent.sh"
[[ -f "${INSTALLER}" ]] || die "归档中缺少 deploy/install-agent.sh"
bash "${INSTALLER}" "$@"
