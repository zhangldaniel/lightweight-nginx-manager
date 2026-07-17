"""Small FastAPI control plane for the Nginx Manager Linux agent.

The service intentionally keeps the protocol narrow.  Agents can only receive
predefined actions, and neither agent tokens nor raw job payloads are exposed
through the administrative read APIs.
"""

from __future__ import annotations

import hashlib
import hmac
import argparse
import json
import os
import re
import secrets
import sqlite3
import ssl
import stat
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing_extensions import Literal


ActionName = Literal[
    "inspect",
    "nginx_test",
    "nginx_reload",
    "config_inventory",
    "certificate_inventory",
    "config_read",
    "config_hash",
    "config_apply",
    "config_delete",
    "certificate_apply",
]

JOB_ACTIONS = {
    "inspect",
    "nginx_test",
    "nginx_reload",
    "config_inventory",
    "certificate_inventory",
    "config_read",
    "config_hash",
    "config_apply",
    "config_delete",
    "certificate_apply",
}
TERMINAL_JOB_STATES = {"succeeded", "failed", "expired"}
ACTIVE_JOB_STATES = {"queued", "running"}
OPERATION_STATES = {"queued", "running", "succeeded", "failed", "partial", "expired"}
WEB_ROLES = {"admin", "operator", "auditor"}
FAILURE_CODES = {
    "job_expired",
    "agent_interrupted",
    "helper_unavailable",
    "permission_denied",
    "path_rejected",
    "concurrent_change",
    "config_policy_rejected",
    "certificate_validation_failed",
    "nginx_config_test_failed",
    "nginx_reload_failed",
    "health_check_failed",
    "rollback_failed",
    "command_timeout",
    "internal_error",
    "publish_failed",
    "job_failed",
}
FAILURE_STAGES = {
    "queue",
    "agent",
    "precheck",
    "prepare",
    "write",
    "nginx_test",
    "reload",
    "health_check",
    "recovery",
    "unknown",
}
ROLLBACK_STATUSES = {"restored", "unverified"}
NGINX_ERROR_CODES = {
    "certificate_file_missing",
    "private_key_file_missing",
    "referenced_file_missing",
    "unknown_directive",
    "duplicate_upstream",
    "permission_denied",
    "invalid_arguments",
    "missing_semicolon",
    "brace_mismatch",
    "duplicate_listen",
    "certificate_key_mismatch",
    "invalid_url_prefix",
}
SENSITIVE_KEY_FRAGMENTS = {
    "private_key",
    "privatekey",
    "key_pem",
    "password",
    "passphrase",
    "secret",
    "token",
}
PRIVATE_KEY_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
)
INVENTORY_MAX_FILES = 200
INVENTORY_MAX_FILE_BYTES = 256 * 1024
INVENTORY_MAX_TOTAL_BYTES = 1024 * 1024


class LDAPAuthenticationError(Exception):
    """The directory rejected the user or did not grant a platform role."""


class LDAPUnavailableError(Exception):
    """The configured directory could not safely complete authentication."""


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("{} must be a boolean".format(name))


def _utc_iso(timestamp: Optional[int]) -> Optional[str]:
    if timestamp is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return _sha256_text(token)


def _derive_agent_credential(enrollment_secret: str, enrollment_id: str, node_id: str) -> str:
    """Derive the durable machine credential from an Agent-owned enrollment secret."""
    message = "nginx-manager-agent-v2\0{}\0{}".format(enrollment_id, node_id).encode("utf-8")
    return hmac.new(enrollment_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _derive_csrf_value(session_value: str) -> str:
    return hmac.new(
        session_value.encode("utf-8"),
        b"nginx-manager-web-csrf-v1",
        hashlib.sha256,
    ).hexdigest()


def _password_hash(password: str, iterations: int, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, salt.hex(), digest.hex())


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        if iterations < 100000 or iterations > 2000000:
            return False
        salt = bytes.fromhex(raw_salt)
        expected = bytes.fromhex(raw_digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def _contains_sensitive_material(value: Any, key_hint: str = "") -> bool:
    normalized_key = key_hint.lower().replace("-", "_")
    if any(fragment in normalized_key for fragment in SENSITIVE_KEY_FRAGMENTS):
        return True
    if isinstance(value, dict):
        return any(_contains_sensitive_material(item, str(key)) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_sensitive_material(item, key_hint) for item in value)
    if isinstance(value, str):
        upper_value = value.upper()
        return any(marker in upper_value for marker in PRIVATE_KEY_MARKERS)
    return False


def _safe_config_inventory(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        return None
    safe_files: List[Dict[str, Any]] = []
    total_bytes = 0
    rejected = 0
    truncated = bool(value.get("truncated", False))
    for item in value["files"]:
        if len(safe_files) >= INVENTORY_MAX_FILES:
            truncated = True
            break
        if not isinstance(item, dict):
            rejected += 1
            continue
        path = item.get("path")
        content = item.get("content")
        digest = item.get("sha256")
        if (
            not isinstance(path, str)
            or not os.path.isabs(path)
            or len(path) > 4096
            or "\0" in path
            or not path.lower().endswith(".conf")
            or not isinstance(content, str)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            rejected += 1
            continue
        encoded = content.encode("utf-8")
        if (
            len(encoded) > INVENTORY_MAX_FILE_BYTES
            or total_bytes + len(encoded) > INVENTORY_MAX_TOTAL_BYTES
            or hashlib.sha256(encoded).hexdigest() != digest
            or any(marker in content.upper() for marker in PRIVATE_KEY_MARKERS)
        ):
            rejected += 1
            if total_bytes + len(encoded) > INVENTORY_MAX_TOTAL_BYTES:
                truncated = True
            continue
        safe_files.append({
            "path": path,
            "content": content,
            "sha256": digest,
            "size": len(encoded),
        })
        total_bytes += len(encoded)
    supplied_skipped = value.get("skipped_count", 0)
    if not isinstance(supplied_skipped, int) or supplied_skipped < 0:
        supplied_skipped = 0
    return {
        "files": safe_files,
        "file_count": len(safe_files),
        "total_bytes": total_bytes,
        "skipped_count": min(supplied_skipped + rejected, 100000),
        "truncated": truncated,
    }


def _safe_certificate_inventory(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("certificates"), list):
        return None
    safe_certificates: List[Dict[str, Any]] = []
    rejected = 0
    truncated = bool(value.get("truncated", False))
    fingerprint_pattern = re.compile(r"^(?:[0-9A-F]{2}:){31}[0-9A-F]{2}$", re.IGNORECASE)
    timestamp_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    domain_pattern = re.compile(r"^[A-Za-z0-9*](?:[A-Za-z0-9*._-]{0,251}[A-Za-z0-9])?$")

    for item in value["certificates"]:
        if len(safe_certificates) >= INVENTORY_MAX_FILES:
            truncated = True
            break
        if not isinstance(item, dict):
            rejected += 1
            continue
        certificate_path = item.get("certificate_path")
        private_key_path = item.get("private_key_path")
        certificate_hash = item.get("certificate_sha256")
        key_hash = item.get("key_material_sha256")
        fingerprint = item.get("fingerprint")
        not_after = item.get("not_after")
        days_remaining = item.get("days_remaining")
        issuer = item.get("issuer")
        subject = item.get("subject")
        domains = item.get("domains")
        valid_paths = (
            isinstance(certificate_path, str)
            and isinstance(private_key_path, str)
            and os.path.isabs(certificate_path)
            and os.path.isabs(private_key_path)
            and certificate_path != private_key_path
            and len(certificate_path) <= 4096
            and len(private_key_path) <= 4096
            and "\0" not in certificate_path
            and "\0" not in private_key_path
            and Path(certificate_path).suffix.lower() in (".pem", ".crt")
            and Path(private_key_path).suffix.lower() in (".pem", ".key")
        )
        valid_scalars = (
            isinstance(certificate_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", certificate_hash) is not None
            and isinstance(key_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", key_hash) is not None
            and isinstance(fingerprint, str)
            and fingerprint_pattern.fullmatch(fingerprint) is not None
            and isinstance(not_after, str)
            and timestamp_pattern.fullmatch(not_after) is not None
            and isinstance(days_remaining, int)
            and -36500 <= days_remaining <= 365000
            and isinstance(issuer, str)
            and 0 < len(issuer) <= 512
            and re.search(r"[\x00-\x1f\x7f]", issuer) is None
            and isinstance(subject, str)
            and 0 < len(subject) <= 512
            and re.search(r"[\x00-\x1f\x7f]", subject) is None
        )
        if not valid_paths or not valid_scalars or not isinstance(domains, list):
            rejected += 1
            continue
        safe_domains: List[str] = []
        invalid_domain = False
        for domain in domains[:100]:
            if not isinstance(domain, str) or len(domain) > 253 or domain_pattern.fullmatch(domain) is None:
                invalid_domain = True
                break
            if domain not in safe_domains:
                safe_domains.append(domain)
        if invalid_domain or not safe_domains:
            rejected += 1
            continue
        safe_certificates.append({
            "certificate_path": certificate_path,
            "private_key_path": private_key_path,
            "certificate_sha256": certificate_hash,
            "key_material_sha256": key_hash,
            "fingerprint": fingerprint.upper(),
            "not_after": not_after,
            "days_remaining": days_remaining,
            "issuer": issuer,
            "subject": subject,
            "domains": safe_domains,
        })

    supplied_skipped = value.get("skipped_count", 0)
    if not isinstance(supplied_skipped, int) or supplied_skipped < 0:
        supplied_skipped = 0
    return {
        "certificates": safe_certificates,
        "certificate_count": len(safe_certificates),
        "skipped_count": min(supplied_skipped + rejected, 100000),
        "truncated": truncated,
    }


def _safe_result_metadata(request: "JobResultRequest") -> Dict[str, Any]:
    """Keep operational facts, never command output or arbitrary error strings."""
    result: Dict[str, Any] = {}
    nested = request.result or {}
    exit_code = request.exit_code if request.exit_code is not None else nested.get("exit_code")
    if isinstance(exit_code, int):
        result["exit_code"] = exit_code
    if request.duration_ms is not None:
        result["duration_ms"] = request.duration_ms

    allowed_detail_keys = {
        "changed",
        "deleted",
        "path",
        "config_hash",
        "previous_config_hash",
        "syntax_ok",
        "health_ok",
        "reloaded",
        "nginx_version",
        "certificate_sha256",
        "key_material_sha256",
        "certificate_fingerprint",
        "certificate_not_after",
        "certificate_issuer",
    }
    for key, value in (request.details or {}).items():
        if key not in allowed_detail_keys or not isinstance(value, (str, int, float, bool, type(None))):
            continue
        if isinstance(value, str):
            value = value[:256]
        result[key] = value
    certificate_domains = (request.details or {}).get("certificate_domains")
    if request.action == "certificate_apply" and isinstance(certificate_domains, list):
        cleaned_domains = []
        for item in certificate_domains[:100]:
            if isinstance(item, str) and re.fullmatch(r"(?:\*\.)?[A-Za-z0-9.-]{1,253}", item):
                cleaned_domains.append(item.lower().rstrip("."))
        if cleaned_domains:
            result["certificate_domains"] = cleaned_domains

    # Persist only bounded enums that are safe to expose to Web administrators.
    # The arbitrary error/output text below remains digest-only.
    if request.status in {"failed", "expired"}:
        failure_code = (request.details or {}).get("failure_code")
        failure_stage = (request.details or {}).get("failure_stage")
        rollback_status = (request.details or {}).get("rollback_status")
        if isinstance(failure_code, str) and failure_code in FAILURE_CODES:
            result["failure_code"] = failure_code
        if isinstance(failure_stage, str) and failure_stage in FAILURE_STAGES:
            result["failure_stage"] = failure_stage
        if isinstance(rollback_status, str) and rollback_status in ROLLBACK_STATUSES:
            result["rollback_status"] = rollback_status
        nginx_error_code = (request.details or {}).get("nginx_error_code")
        nginx_error_line = (request.details or {}).get("nginx_error_line")
        if (
            failure_code == "nginx_config_test_failed"
            and failure_stage == "nginx_test"
            and request.action in {"nginx_test", "config_apply", "config_delete", "certificate_apply"}
        ):
            if isinstance(nginx_error_code, str) and nginx_error_code in NGINX_ERROR_CODES:
                result["nginx_error_code"] = nginx_error_code
            if (
                isinstance(nginx_error_line, int)
                and not isinstance(nginx_error_line, bool)
                and 1 <= nginx_error_line <= 1000000
            ):
                result["nginx_error_line"] = nginx_error_line

    if request.action == "config_inventory":
        inventory = _safe_config_inventory(request.details)
        if inventory is not None:
            result["config_inventory"] = inventory
    elif request.action == "certificate_inventory":
        inventory = _safe_certificate_inventory(request.details)
        if inventory is not None:
            result["certificate_inventory"] = inventory

    nested_scalar_keys = {
        "sha256",
        "config_sha256",
        "size",
        "validated",
        "applied",
        "reloaded",
        "validate_only",
        "backup_count",
        "certificate_sha256",
        "previous_certificate_sha256",
    }
    for key in nested_scalar_keys:
        if key not in nested:
            continue
        value = nested.get(key)
        if isinstance(value, (str, int, float, bool, type(None))):
            result[key] = value[:256] if isinstance(value, str) else value
    if "config_hash" not in result:
        config_hash = nested.get("config_sha256")
        if request.action in {"config_read", "config_hash", "config_apply"}:
            config_hash = nested.get("sha256", config_hash)
        if isinstance(config_hash, str):
            result["config_hash"] = config_hash[:128]

    output_sources = []
    if request.output is not None:
        output_sources.append(("output", request.output))
    for key in ("stdout", "stderr", "content"):
        value = nested.get(key)
        if isinstance(value, str):
            output_sources.append((key, value))
    for key, value in output_sources:
        encoded = value.encode("utf-8", errors="replace")
        result[key + "_bytes"] = len(encoded)
        result[key + "_sha256"] = hashlib.sha256(encoded).hexdigest()
    if request.error is not None:
        encoded = request.error.encode("utf-8", errors="replace")
        result["error_bytes"] = len(encoded)
        result["error_sha256"] = hashlib.sha256(encoded).hexdigest()
    return result


@dataclass(frozen=True)
class Settings:
    db_path: str
    ui_path: Optional[str] = None
    online_after_seconds: int = 90
    default_job_ttl_seconds: int = 300
    max_job_ttl_seconds: int = 86400
    sensitive_job_ttl_seconds: int = 900
    job_lease_seconds: int = 60
    late_result_grace_seconds: int = 30 * 86400
    job_retention_seconds: int = 30 * 86400
    audit_retention_seconds: int = 180 * 86400
    max_payload_bytes: int = 2 * 1024 * 1024
    max_ui_state_bytes: int = 16 * 1024 * 1024
    max_resource_bytes: int = 1024 * 1024
    session_ttl_seconds: int = 28800
    enrollment_pending_ttl_seconds: int = 86400
    password_iterations: int = 310000
    login_window_seconds: int = 300
    login_max_attempts: int = 8
    ldap_enabled: bool = False
    ldap_url: Optional[str] = None
    ldap_base_dn: Optional[str] = None
    ldap_bind_dn: Optional[str] = None
    ldap_bind_password_file: Optional[str] = None
    ldap_user_filter: str = "(|(sAMAccountName={username})(userPrincipalName={username})(uid={username}))"
    ldap_group_attribute: str = "memberOf"
    ldap_group_search_base: Optional[str] = None
    ldap_group_filter: str = "(member={user_dn})"
    ldap_admin_group: str = "nginx-admin"
    ldap_operator_group: str = "nginx-operator"
    ldap_auditor_group: str = "nginx-auditor"
    ldap_start_tls: bool = False
    ldap_ca_file: Optional[str] = None
    ldap_connect_timeout: int = 5
    ldap_session_recheck_seconds: int = 300

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("NGINX_MANAGER_DB_PATH", "./data/nginx-manager.db"),
            ui_path=os.environ.get("NGINX_MANAGER_UI_PATH") or None,
            online_after_seconds=int(os.environ.get("NGINX_MANAGER_ONLINE_AFTER_SECONDS", "90")),
            default_job_ttl_seconds=int(os.environ.get("NGINX_MANAGER_JOB_TTL_SECONDS", "300")),
            max_job_ttl_seconds=int(os.environ.get("NGINX_MANAGER_MAX_JOB_TTL_SECONDS", "86400")),
            sensitive_job_ttl_seconds=int(os.environ.get("NGINX_MANAGER_SENSITIVE_JOB_TTL_SECONDS", "900")),
            job_lease_seconds=int(os.environ.get("NGINX_MANAGER_JOB_LEASE_SECONDS", "60")),
            late_result_grace_seconds=int(
                os.environ.get("NGINX_MANAGER_LATE_RESULT_GRACE_SECONDS", str(30 * 86400))
            ),
            job_retention_seconds=int(os.environ.get("NGINX_MANAGER_JOB_RETENTION_SECONDS", str(30 * 86400))),
            audit_retention_seconds=int(os.environ.get("NGINX_MANAGER_AUDIT_RETENTION_SECONDS", str(180 * 86400))),
            max_payload_bytes=int(os.environ.get("NGINX_MANAGER_MAX_PAYLOAD_BYTES", str(2 * 1024 * 1024))),
            max_ui_state_bytes=int(os.environ.get("NGINX_MANAGER_MAX_UI_STATE_BYTES", str(16 * 1024 * 1024))),
            max_resource_bytes=int(os.environ.get("NGINX_MANAGER_MAX_RESOURCE_BYTES", str(1024 * 1024))),
            session_ttl_seconds=int(os.environ.get("NGINX_MANAGER_SESSION_TTL_SECONDS", "28800")),
            enrollment_pending_ttl_seconds=int(
                os.environ.get("NGINX_MANAGER_ENROLLMENT_PENDING_TTL_SECONDS", "86400")
            ),
            password_iterations=int(os.environ.get("NGINX_MANAGER_PASSWORD_ITERATIONS", "310000")),
            login_window_seconds=int(os.environ.get("NGINX_MANAGER_LOGIN_WINDOW_SECONDS", "300")),
            login_max_attempts=int(os.environ.get("NGINX_MANAGER_LOGIN_MAX_ATTEMPTS", "8")),
            ldap_enabled=_env_bool("NGINX_MANAGER_LDAP_ENABLED", False),
            ldap_url=os.environ.get("NGINX_MANAGER_LDAP_URL") or None,
            ldap_base_dn=os.environ.get("NGINX_MANAGER_LDAP_BASE_DN") or None,
            ldap_bind_dn=os.environ.get("NGINX_MANAGER_LDAP_BIND_DN") or None,
            ldap_bind_password_file=os.environ.get("NGINX_MANAGER_LDAP_BIND_PASSWORD_FILE") or None,
            ldap_user_filter=os.environ.get(
                "NGINX_MANAGER_LDAP_USER_FILTER",
                "(|(sAMAccountName={username})(userPrincipalName={username})(uid={username}))",
            ),
            ldap_group_attribute=os.environ.get("NGINX_MANAGER_LDAP_GROUP_ATTRIBUTE", "memberOf"),
            ldap_group_search_base=os.environ.get("NGINX_MANAGER_LDAP_GROUP_SEARCH_BASE") or None,
            ldap_group_filter=os.environ.get("NGINX_MANAGER_LDAP_GROUP_FILTER", "(member={user_dn})"),
            ldap_admin_group=os.environ.get("NGINX_MANAGER_LDAP_ADMIN_GROUP", "nginx-admin"),
            ldap_operator_group=os.environ.get("NGINX_MANAGER_LDAP_OPERATOR_GROUP", "nginx-operator"),
            ldap_auditor_group=os.environ.get("NGINX_MANAGER_LDAP_AUDITOR_GROUP", "nginx-auditor"),
            ldap_start_tls=_env_bool("NGINX_MANAGER_LDAP_START_TLS", False),
            ldap_ca_file=os.environ.get("NGINX_MANAGER_LDAP_CA_FILE") or None,
            ldap_connect_timeout=int(os.environ.get("NGINX_MANAGER_LDAP_CONNECT_TIMEOUT", "5")),
            ldap_session_recheck_seconds=int(
                os.environ.get("NGINX_MANAGER_LDAP_SESSION_RECHECK_SECONDS", "300")
            ),
        )

    def validate(self) -> None:
        if not 10 <= self.job_lease_seconds <= 3600:
            raise ValueError("NGINX_MANAGER_JOB_LEASE_SECONDS must be between 10 and 3600")
        if not 0 <= self.late_result_grace_seconds <= 90 * 86400:
            raise ValueError("NGINX_MANAGER_LATE_RESULT_GRACE_SECONDS is out of range")
        if self.job_retention_seconds < 86400 or self.audit_retention_seconds < 86400:
            raise ValueError("retention settings must be at least one day")
        if not self.ldap_enabled:
            return
        required = {
            "NGINX_MANAGER_LDAP_URL": self.ldap_url,
            "NGINX_MANAGER_LDAP_BASE_DN": self.ldap_base_dn,
            "NGINX_MANAGER_LDAP_BIND_DN": self.ldap_bind_dn,
            "NGINX_MANAGER_LDAP_BIND_PASSWORD_FILE": self.ldap_bind_password_file,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("LDAP is enabled but required settings are missing: {}".format(", ".join(missing)))
        parsed = urlparse(str(self.ldap_url))
        if parsed.scheme not in {"ldap", "ldaps"} or not parsed.hostname:
            raise ValueError("NGINX_MANAGER_LDAP_URL must use ldap:// or ldaps://")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("NGINX_MANAGER_LDAP_URL must not contain credentials, query, or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("NGINX_MANAGER_LDAP_URL must not contain a path")
        if self.ldap_start_tls and parsed.scheme != "ldap":
            raise ValueError("LDAP StartTLS can only be used with ldap://")
        if "{username}" not in self.ldap_user_filter or len(self.ldap_user_filter) > 1024:
            raise ValueError("NGINX_MANAGER_LDAP_USER_FILTER must contain {username}")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", self.ldap_group_attribute):
            raise ValueError("NGINX_MANAGER_LDAP_GROUP_ATTRIBUTE is invalid")
        if self.ldap_group_search_base and (
            "{user_dn}" not in self.ldap_group_filter or len(self.ldap_group_filter) > 1024
        ):
            raise ValueError("NGINX_MANAGER_LDAP_GROUP_FILTER must contain {user_dn}")
        if not any((self.ldap_admin_group, self.ldap_operator_group, self.ldap_auditor_group)):
            raise ValueError("at least one LDAP role group must be configured")
        if not 1 <= self.ldap_connect_timeout <= 30:
            raise ValueError("NGINX_MANAGER_LDAP_CONNECT_TIMEOUT must be between 1 and 30 seconds")
        if not 60 <= self.ldap_session_recheck_seconds <= 3600:
            raise ValueError("NGINX_MANAGER_LDAP_SESSION_RECHECK_SECONDS must be between 60 and 3600")
        password_path = Path(str(self.ldap_bind_password_file))
        if not password_path.is_absolute():
            raise ValueError("NGINX_MANAGER_LDAP_BIND_PASSWORD_FILE must be an absolute path")
        if self.ldap_ca_file and not Path(self.ldap_ca_file).is_absolute():
            raise ValueError("NGINX_MANAGER_LDAP_CA_FILE must be an absolute path")


def _read_ldap_bind_password(settings: Settings) -> str:
    path = Path(str(settings.ldap_bind_password_file))
    try:
        if path.is_symlink() or not path.is_file():
            raise LDAPUnavailableError("LDAP bind password file is not a regular file")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o027:
            raise LDAPUnavailableError("LDAP bind password file permissions are too broad")
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except LDAPUnavailableError:
        raise
    except OSError as exc:
        raise LDAPUnavailableError("LDAP bind password file is unavailable") from exc
    if not value or len(value) > 1024 or "\0" in value or "\n" in value or "\r" in value:
        raise LDAPUnavailableError("LDAP bind password file is invalid")
    return value


def _ldap_group_aliases(value: Any) -> set[str]:
    text = str(value or "").strip().casefold()
    if not text:
        return set()
    aliases = {text}
    first = text.split(",", 1)[0]
    if "=" in first:
        attribute, name = first.split("=", 1)
        if attribute.strip() in {"cn", "ou"} and name.strip():
            aliases.add(name.strip())
    return aliases


def _ldap_role_for_groups(settings: Settings, groups: List[str]) -> Optional[str]:
    aliases: set[str] = set()
    for group in groups:
        aliases.update(_ldap_group_aliases(group))
    for role, configured in (
        ("admin", settings.ldap_admin_group),
        ("operator", settings.ldap_operator_group),
        ("auditor", settings.ldap_auditor_group),
    ):
        if configured and _ldap_group_aliases(configured) & aliases:
            return role
    return None


def _ldap_search_entries(connection: Any, search_base: str, search_filter: str,
                         attributes: List[str]) -> List[Dict[str, Any]]:
    outcome = connection.search(
        search_base=search_base,
        search_filter=search_filter,
        search_scope="SUBTREE",
        attributes=attributes,
        size_limit=20,
    )
    if not isinstance(outcome, tuple) or len(outcome) < 3:
        raise LDAPUnavailableError("LDAP client returned an unexpected search result")
    succeeded, _result, response = outcome[:3]
    if not succeeded or not isinstance(response, list):
        raise LDAPUnavailableError("LDAP search failed")
    return [item for item in response if isinstance(item, dict) and item.get("type") == "searchResEntry"]


def _lookup_ldap_principal(
    settings: Settings,
    username: str,
    password: Optional[str] = None,
) -> Dict[str, str]:
    try:
        from ldap3 import (
            AUTO_BIND_NO_TLS,
            AUTO_BIND_TLS_BEFORE_BIND,
            Connection,
            NONE,
            SAFE_SYNC,
            Server,
            Tls,
        )
        from ldap3.core.exceptions import LDAPBindError, LDAPException
        from ldap3.utils.conv import escape_filter_chars
    except ImportError as exc:
        raise LDAPUnavailableError("LDAP support is not installed") from exc

    parsed = urlparse(str(settings.ldap_url))
    use_ssl = parsed.scheme == "ldaps"
    port = parsed.port or (636 if use_ssl else 389)
    tls_configuration = None
    if use_ssl or settings.ldap_start_tls:
        tls_configuration = Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=settings.ldap_ca_file or None,
        )
    server = Server(
        parsed.hostname,
        port=port,
        use_ssl=use_ssl,
        tls=tls_configuration,
        get_info=NONE,
        connect_timeout=settings.ldap_connect_timeout,
    )
    auto_bind = AUTO_BIND_TLS_BEFORE_BIND if settings.ldap_start_tls else AUTO_BIND_NO_TLS
    bind_password = _read_ldap_bind_password(settings)
    service_connection = None
    try:
        service_connection = Connection(
            server,
            user=settings.ldap_bind_dn,
            password=bind_password,
            client_strategy=SAFE_SYNC,
            auto_bind=auto_bind,
            raise_exceptions=True,
        )
        escaped_username = escape_filter_chars(username)
        user_filter = settings.ldap_user_filter.replace("{username}", escaped_username)
        entries = _ldap_search_entries(
            service_connection,
            str(settings.ldap_base_dn),
            user_filter,
            [settings.ldap_group_attribute],
        )
        if len(entries) != 1:
            raise LDAPAuthenticationError("directory user was not uniquely identified")
        entry = entries[0]
        user_dn = str(entry.get("dn") or "").strip()
        if not user_dn:
            raise LDAPAuthenticationError("directory user has no DN")
        raw_attributes = entry.get("attributes") if isinstance(entry.get("attributes"), dict) else {}
        raw_groups = raw_attributes.get(settings.ldap_group_attribute, [])
        if isinstance(raw_groups, str):
            groups = [raw_groups]
        elif isinstance(raw_groups, (list, tuple, set)):
            groups = [str(item) for item in raw_groups if item]
        else:
            groups = []

        if password is not None:
            user_connection = None
            try:
                user_connection = Connection(
                    server,
                    user=user_dn,
                    password=password,
                    client_strategy=SAFE_SYNC,
                    auto_bind=auto_bind,
                    raise_exceptions=True,
                )
            except LDAPBindError as exc:
                raise LDAPAuthenticationError("directory rejected the user credentials") from exc
            finally:
                if user_connection is not None:
                    user_connection.unbind()

        if settings.ldap_group_search_base:
            group_filter = settings.ldap_group_filter.replace("{user_dn}", escape_filter_chars(user_dn))
            group_entries = _ldap_search_entries(
                service_connection,
                settings.ldap_group_search_base,
                group_filter,
                ["cn"],
            )
            for group_entry in group_entries:
                if group_entry.get("dn"):
                    groups.append(str(group_entry["dn"]))
                group_attributes = group_entry.get("attributes")
                if isinstance(group_attributes, dict):
                    common_names = group_attributes.get("cn", [])
                    if isinstance(common_names, str):
                        groups.append(common_names)
                    elif isinstance(common_names, (list, tuple, set)):
                        groups.extend(str(item) for item in common_names if item)

        role = _ldap_role_for_groups(settings, groups)
        if role is None:
            raise LDAPAuthenticationError("directory user has no Nginx Manager role")
        return {
            "principal_id": "ldap:" + _sha256_text(user_dn.casefold()),
            "username": username,
            "role": role,
            "auth_source": "ldap",
        }
    except LDAPAuthenticationError:
        raise
    except LDAPException as exc:
        raise LDAPUnavailableError("LDAP service is unavailable") from exc
    finally:
        bind_password = ""
        if service_connection is not None:
            service_connection.unbind()


def _authenticate_ldap(settings: Settings, username: str, password: str) -> Dict[str, str]:
    return _lookup_ldap_principal(settings, username, password)


class EnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enrollment_id: str = Field(..., min_length=16, max_length=128)
    enrollment_secret: str = Field(..., min_length=32, max_length=256)
    node_name: str = Field(..., min_length=1, max_length=128)
    hostname: str = Field(..., min_length=1, max_length=255)
    labels: Dict[str, str] = Field(default_factory=dict)

    @field_validator("enrollment_id")
    def validate_enrollment_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9._-]{16,128}", value) is None:
            raise ValueError("invalid enrollment id")
        return value

    @field_validator("enrollment_secret")
    def validate_enrollment_secret(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value) is None:
            raise ValueError("invalid enrollment secret")
        return value

    @field_validator("node_name", "hostname")
    def strip_identity(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("labels")
    def validate_labels(cls, value: Dict[str, str]) -> Dict[str, str]:
        if len(value) > 64:
            raise ValueError("too many labels")
        cleaned: Dict[str, str] = {}
        for key, item in value.items():
            key = str(key).strip()
            item = str(item).strip()
            if not key or len(key) > 64 or len(item) > 256:
                raise ValueError("invalid label")
            cleaned[key] = item
        return cleaned


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=512)

    @field_validator("username")
    def clean_username(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._@-]{1,128}", value):
            raise ValueError("invalid username")
        return value

class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["online", "degraded", "offline"] = "online"
    agent_version: Optional[str] = Field(None, max_length=64)
    nginx_version: Optional[str] = Field(None, max_length=128)
    config_hash: Optional[str] = Field(None, max_length=128)
    active_job_id: Optional[str] = Field(None, min_length=1, max_length=200)
    capabilities: List[str] = Field(default_factory=list)
    facts: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("capabilities")
    def validate_capabilities(cls, value: List[str]) -> List[str]:
        if len(value) > 32:
            raise ValueError("too many capabilities")
        return [str(item)[:64] for item in value]

    @field_validator("facts")
    def sanitize_facts(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "os", "os_version", "arch", "kernel", "cpu_count", "memory_bytes",
            "nginx_root", "managed_config_root", "managed_certificate_root",
        }
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if key not in allowed or not isinstance(item, (str, int, float, bool, type(None))):
                continue
            result[key] = item[:256] if isinstance(item, str) else item
        return result

class PollRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    limit: int = Field(1, ge=1, le=8)


class JobResultRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["succeeded", "failed", "expired"]
    job_id: Optional[str] = Field(None, max_length=200)
    action: Optional[str] = Field(None, max_length=64)
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = Field(None, ge=0, le=86400000)
    output: Optional[str] = None
    error: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    result: Dict[str, Any] = Field(default_factory=dict)

class AdminJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_ids: List[str] = Field(..., min_length=1, max_length=100)
    action: ActionName
    payload: Dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: Optional[int] = Field(None, ge=5, le=86400)

    @field_validator("node_ids")
    def unique_node_ids(cls, value: List[str]) -> List[str]:
        result: List[str] = []
        seen = set()
        for item in value:
            item = str(item).strip()
            if not item:
                raise ValueError("node id must not be blank")
            if item not in seen:
                result.append(item)
                seen.add(item)
        return result


class OperationJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(..., min_length=1, max_length=200)
    action: ActionName
    payload: Dict[str, Any] = Field(default_factory=dict)


class OperationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_id: str = Field(..., min_length=1, max_length=200)
    request_id: Optional[str] = Field(None, min_length=16, max_length=128)
    kind: Literal["validate", "publish", "delete", "transfer", "certificate", "inventory"]
    base_version: int = Field(0, ge=0, le=1000000000)
    candidate: Dict[str, Any] = Field(default_factory=dict)
    jobs: List[OperationJobRequest] = Field(..., min_length=1, max_length=100)
    ttl_seconds: Optional[int] = Field(None, ge=5, le=86400)

    @field_validator("site_id")
    def validate_site_id(cls, value: str) -> str:
        value = value.strip()
        if not value or re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", value) is None:
            raise ValueError("invalid site id")
        return value

    @field_validator("request_id")
    def validate_request_id(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and re.fullmatch(r"[A-Za-z0-9._:-]{16,128}", value) is None:
            raise ValueError("invalid request id")
        return value

    @field_validator("jobs")
    def unique_operation_nodes(cls, value: List[OperationJobRequest]) -> List[OperationJobRequest]:
        seen = set()
        for item in value:
            if item.node_id in seen:
                raise ValueError("operation jobs must target each node at most once")
            seen.add(item.node_id)
        return value

class UIStatePutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(..., ge=0)
    state: Dict[str, Any]


class Database:
    def __init__(self, path: str):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        # DELETE + secure_delete reduces how long claimed secret payloads linger.
        connection.execute("PRAGMA secure_delete = ON")
        if self.path != ":memory:":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                connection.close()
                raise
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    node_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    hostname TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    enrolled_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_seen_at INTEGER,
                    reported_status TEXT NOT NULL DEFAULT 'offline',
                    agent_version TEXT,
                    nginx_version TEXT,
                    config_hash TEXT,
                    capabilities_json TEXT NOT NULL DEFAULT '[]',
                    facts_json TEXT NOT NULL DEFAULT '{}',
                    revoked_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    node_id TEXT NOT NULL REFERENCES nodes(id),
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    payload_sensitive INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    claimed_at INTEGER,
                    completed_at INTEGER,
                    result_meta_json TEXT,
                    result_sha256 TEXT
                );

                CREATE INDEX IF NOT EXISTS jobs_claim_idx
                    ON jobs(node_id, status, expires_at, created_at);
                CREATE INDEX IF NOT EXISTS jobs_list_idx
                    ON jobs(created_at DESC);

                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT,
                    event TEXT NOT NULL,
                    target_type TEXT,
                    target_id TEXT,
                    detail_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS ui_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    revision INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO ui_state(singleton_id, revision, state_json, updated_at)
                    VALUES(1, 0, '{}', 0);

                CREATE TABLE IF NOT EXISTS site_revisions (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    published_at INTEGER,
                    UNIQUE(site_id, version)
                );
                CREATE INDEX IF NOT EXISTS site_revisions_list_idx
                    ON site_revisions(site_id, version DESC);

                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    candidate_revision_id TEXT,
                    created_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS operations_list_idx
                    ON operations(updated_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS operations_site_idx
                    ON operations(site_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS resources (
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    document_json TEXT NOT NULL,
                    document_sha256 TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    PRIMARY KEY(kind, id)
                );
                CREATE INDEX IF NOT EXISTS resources_order_idx
                    ON resources(kind, position, id);

                CREATE TABLE IF NOT EXISTS admin_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_digest TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_login_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS admin_sessions (
                    session_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS admin_sessions_expiry_idx
                    ON admin_sessions(expires_at);

                CREATE TABLE IF NOT EXISTS web_sessions (
                    session_hash TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    username TEXT NOT NULL COLLATE NOCASE,
                    role TEXT NOT NULL,
                    auth_source TEXT NOT NULL,
                    csrf_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    role_checked_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS web_sessions_expiry_idx
                    ON web_sessions(expires_at);

                CREATE TABLE IF NOT EXISTS agent_enrollments (
                    enrollment_id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL COLLATE NOCASE,
                    hostname TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    secret_hash TEXT NOT NULL,
                    credential_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    requested_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    decided_at INTEGER,
                    decided_by TEXT
                );
                CREATE INDEX IF NOT EXISTS agent_enrollments_status_idx
                    ON agent_enrollments(status, requested_at DESC);
                CREATE INDEX IF NOT EXISTS agent_enrollments_node_name_idx
                    ON agent_enrollments(node_name COLLATE NOCASE, status);
                """
            )
            existing_job_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            job_migrations = {
                "operation_id": "TEXT",
                "lease_expires_at": "INTEGER",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "created_by": "TEXT",
            }
            for column, definition in job_migrations.items():
                if column not in existing_job_columns:
                    connection.execute("ALTER TABLE jobs ADD COLUMN {} {}".format(column, definition))
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_operation_idx ON jobs(operation_id, created_at, id)"
            )
            existing_node_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(nodes)").fetchall()
            }
            if "revoked_at" not in existing_node_columns:
                connection.execute("ALTER TABLE nodes ADD COLUMN revoked_at INTEGER")
            existing_session_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(web_sessions)").fetchall()
            }
            if "role_checked_at" not in existing_session_columns:
                connection.execute("ALTER TABLE web_sessions ADD COLUMN role_checked_at INTEGER NOT NULL DEFAULT 0")

    def bootstrap_admin(self, username: str, password: str, iterations: int) -> Dict[str, Any]:
        """Create the first Web administrator without exposing a runtime bootstrap secret."""
        username = username.strip()
        if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username) is None:
            raise ValueError("administrator username is invalid")
        if len(password) < 12 or len(password) > 512:
            raise ValueError("administrator password must be between 12 and 512 characters")
        now = int(time.time())
        with self.transaction() as connection:
            exists = connection.execute("SELECT id, username FROM admin_users LIMIT 1").fetchone()
            if exists is not None:
                return {"created": False, "id": exists["id"], "username": exists["username"]}
            user_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO admin_users
                   (id, username, password_digest, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?)""",
                (user_id, username, _password_hash(password, iterations), now, now),
            )
            Database.audit(
                connection,
                "system",
                None,
                "initial_admin_created",
                "admin_user",
                user_id,
                {"username": username},
            )
        return {"created": True, "id": user_id, "username": username}

    @staticmethod
    def audit(
        connection: sqlite3.Connection,
        actor_type: str,
        actor_id: Optional[str],
        event: str,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        connection.execute(
            """INSERT INTO audit
               (created_at, actor_type, actor_id, event, target_type, target_id, detail_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (int(time.time()), actor_type, actor_id, event, target_type, target_id, _canonical_json(detail or {})),
        )

    @staticmethod
    def expire_jobs(connection: sqlite3.Connection, now: int) -> int:
        rows = connection.execute(
            "SELECT id, operation_id FROM jobs WHERE status IN ('queued', 'running') AND expires_at <= ?", (now,)
        ).fetchall()
        if not rows:
            return 0
        redacted = _canonical_json({"redacted": True, "reason": "expired"})
        expired_result = _canonical_json(
            {"failure_code": "job_expired", "failure_stage": "queue"}
        )
        connection.execute(
            """UPDATE jobs
               SET status = 'expired', completed_at = ?, payload_json = ?, result_meta_json = ?
               WHERE status IN ('queued', 'running') AND expires_at <= ?""",
            (now, redacted, expired_result, now),
        )
        Database.audit(
            connection,
            "system",
            None,
            "jobs_expired",
            "job",
            None,
            {"count": len(rows)},
        )
        for operation_id in {row["operation_id"] for row in rows if row["operation_id"]}:
            _refresh_operation(connection, operation_id, now)
        return len(rows)

    @staticmethod
    def prune(connection: sqlite3.Connection, now: int, job_retention: int, audit_retention: int) -> Dict[str, int]:
        job_cursor = connection.execute(
            "DELETE FROM jobs WHERE status IN ('succeeded', 'failed', 'expired') "
            "AND completed_at IS NOT NULL AND completed_at < ?",
            (now - job_retention,),
        )
        operation_cursor = connection.execute(
            "DELETE FROM operations WHERE status IN ('succeeded', 'failed', 'partial', 'expired') "
            "AND completed_at IS NOT NULL AND completed_at < ?",
            (now - job_retention,),
        )
        audit_cursor = connection.execute(
            "DELETE FROM audit WHERE created_at < ?", (now - audit_retention,)
        )
        enrollment_cursor = connection.execute(
            "DELETE FROM agent_enrollments WHERE status <> 'pending' AND updated_at < ?",
            (now - job_retention,),
        )
        return {
            "jobs": max(0, job_cursor.rowcount),
            "operations": max(0, operation_cursor.rowcount),
            "audit": max(0, audit_cursor.rowcount),
            "enrollments": max(0, enrollment_cursor.rowcount),
        }


def _node_public(row: sqlite3.Row, now: int, online_after_seconds: int) -> Dict[str, Any]:
    last_seen = row["last_seen_at"]
    effective_online = (
        last_seen is not None
        and now - int(last_seen) <= online_after_seconds
        and row["reported_status"] != "offline"
        and row["revoked_at"] is None
    )
    return {
        "id": row["id"],
        "node_name": row["node_name"],
        "hostname": row["hostname"],
        "labels": json.loads(row["labels_json"]),
        "status": row["reported_status"] if effective_online else "offline",
        "reported_status": row["reported_status"],
        "agent_version": row["agent_version"],
        "nginx_version": row["nginx_version"],
        "config_hash": row["config_hash"],
        "capabilities": json.loads(row["capabilities_json"]),
        "facts": json.loads(row["facts_json"]),
        "enrolled_at": _utc_iso(row["enrolled_at"]),
        "last_seen_at": _utc_iso(last_seen),
        "revoked_at": _utc_iso(row["revoked_at"]),
    }


def _job_public(row: sqlite3.Row) -> Dict[str, Any]:
    result_meta = json.loads(row["result_meta_json"]) if row["result_meta_json"] else None
    return {
        "id": row["id"],
        "batch_id": row["batch_id"],
        "operation_id": row["operation_id"] if "operation_id" in row.keys() else None,
        "node_id": row["node_id"],
        "node_name": row["node_name"] if "node_name" in row.keys() else None,
        "action": row["action"],
        "status": row["status"],
        "payload_sha256": row["payload_sha256"],
        "payload_sensitive": bool(row["payload_sensitive"]),
        "created_at": _utc_iso(row["created_at"]),
        "expires_at": _utc_iso(row["expires_at"]),
        "claimed_at": _utc_iso(row["claimed_at"]),
        "lease_expires_at": _utc_iso(row["lease_expires_at"] if "lease_expires_at" in row.keys() else None),
        "attempt_count": int(row["attempt_count"] or 0) if "attempt_count" in row.keys() else 0,
        "created_by": row["created_by"] if "created_by" in row.keys() else None,
        "completed_at": _utc_iso(row["completed_at"]),
        "result": result_meta,
        "result_sha256": row["result_sha256"],
    }


def _operation_public(row: sqlite3.Row) -> Dict[str, Any]:
    metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
    return {
        "id": row["id"],
        "site_id": row["site_id"],
        "kind": row["kind"],
        "status": row["status"],
        "base_version": int(row["base_version"]),
        "candidate_revision_id": row["candidate_revision_id"],
        "created_by": row["created_by"],
        "created_at": _utc_iso(row["created_at"]),
        "updated_at": _utc_iso(row["updated_at"]),
        "completed_at": _utc_iso(row["completed_at"]),
        "metadata": metadata,
    }


def _revision_public(row: sqlite3.Row, include_snapshot: bool = False) -> Dict[str, Any]:
    result = {
        "id": row["id"],
        "site_id": row["site_id"],
        "version": int(row["version"]),
        "snapshot_sha256": row["snapshot_sha256"],
        "note": row["note"],
        "created_by": row["created_by"],
        "created_at": _utc_iso(row["created_at"]),
        "published_at": _utc_iso(row["published_at"]),
    }
    if include_snapshot:
        result["snapshot"] = json.loads(row["snapshot_json"])
    return result


def _refresh_operation(connection: sqlite3.Connection, operation_id: Optional[str], now: int) -> None:
    if not operation_id:
        return
    operation = connection.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
    if operation is None:
        return
    rows = connection.execute(
        "SELECT status FROM jobs WHERE operation_id = ? ORDER BY created_at, id", (operation_id,)
    ).fetchall()
    if not rows:
        return
    statuses = [row["status"] for row in rows]
    if any(item in ACTIVE_JOB_STATES for item in statuses):
        next_status = "running" if any(item == "running" for item in statuses) else "queued"
        completed_at = None
    elif all(item == "succeeded" for item in statuses):
        next_status = "succeeded"
        completed_at = now
    elif all(item == "expired" for item in statuses):
        next_status = "expired"
        completed_at = now
    elif any(item == "succeeded" for item in statuses):
        next_status = "partial"
        completed_at = now
    else:
        next_status = "failed"
        completed_at = now
    connection.execute(
        "UPDATE operations SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
        (next_status, now, completed_at, operation_id),
    )
    if next_status == "succeeded" and operation["kind"] == "publish" and operation["candidate_revision_id"]:
        connection.execute(
            "UPDATE site_revisions SET published_at = COALESCE(published_at, ?) WHERE id = ?",
            (now, operation["candidate_revision_id"]),
        )


def _enrollment_public(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["enrollment_id"],
        "node_id": row["node_id"],
        "node_name": row["node_name"],
        "hostname": row["hostname"],
        "labels": json.loads(row["labels_json"]),
        "status": row["status"],
        "requested_at": _utc_iso(row["requested_at"]),
        "updated_at": _utc_iso(row["updated_at"]),
        "expires_at": _utc_iso(row["expires_at"]),
        "decided_at": _utc_iso(row["decided_at"]),
        "decided_by": row["decided_by"],
    }


def _load_ui_state_document(connection: sqlite3.Connection) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT revision, state_json FROM ui_state WHERE singleton_id = 1"
    ).fetchone()
    state = json.loads(row["state_json"] or "{}")
    resource_count = connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    if resource_count or state.get("_resources_split_v1") is True:
        for kind, state_key in (("site", "sites"), ("certificate", "certificates")):
            resources = connection.execute(
                "SELECT document_json FROM resources WHERE kind = ? ORDER BY position, id", (kind,)
            ).fetchall()
            state[state_key] = [json.loads(item["document_json"]) for item in resources]
    return {"revision": row["revision"], "state": state}


def create_app(
    settings: Optional[Settings] = None,
    ldap_authenticator: Optional[Callable[[Settings, str, str], Dict[str, str]]] = None,
    ldap_role_checker: Optional[Callable[[Settings, str], Dict[str, str]]] = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    authenticate_ldap = ldap_authenticator or _authenticate_ldap
    check_ldap_role = ldap_role_checker or (lambda current_settings, username: _lookup_ldap_principal(
        current_settings, username, None
    ))
    database = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(_api: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        with database.transaction() as connection:
            now = int(time.time())
            connection.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now,))
            connection.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))
            Database.prune(
                connection,
                now,
                settings.job_retention_seconds,
                settings.audit_retention_seconds,
            )
        yield

    api = FastAPI(
        title="Nginx Manager",
        version="0.4.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    api.state.settings = settings
    api.state.database = database
    secure_session_cookie = "__Host-nginx_manager_session"
    http_session_cookie = "nginx_manager_session"
    login_attempts: Dict[str, List[int]] = {}
    login_attempts_lock = threading.Lock()
    dummy_password_digest = _password_hash("dummy-password-never-valid", settings.password_iterations, b"\0" * 16)

    def session_cookie_for(request: Request) -> Tuple[str, bool]:
        secure = request.url.scheme == "https"
        return (secure_session_cookie if secure else http_session_cookie, secure)

    @api.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
        )
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @api.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default validation response can echo invalid payload values.
        return JSONResponse(status_code=422, content={"detail": "request validation failed"})

    def require_session(request: Request, x_csrf_token: Optional[str] = Header(None)) -> Dict[str, Any]:
        session_cookie, _secure = session_cookie_for(request)
        candidate = request.cookies.get(session_cookie)
        if not candidate:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
        now = int(time.time())
        digest = _token_hash(candidate)
        with database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM web_sessions WHERE session_hash = ?",
                (digest,),
            ).fetchone()
        if row is None or row["expires_at"] <= now or row["role"] not in WEB_ROLES:
            with database.transaction() as connection:
                connection.execute("DELETE FROM web_sessions WHERE session_hash = ?", (digest,))
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            expected_csrf = _derive_csrf_value(candidate)
            if not x_csrf_token or not hmac.compare_digest(x_csrf_token, expected_csrf):
                raise HTTPException(status_code=403, detail="invalid request verification code")

        principal = dict(row)
        if (
            principal["auth_source"] == "ldap"
            and now - int(principal["role_checked_at"] or 0) >= settings.ldap_session_recheck_seconds
        ):
            try:
                refreshed = check_ldap_role(settings, principal["username"])
            except LDAPAuthenticationError:
                with database.transaction() as connection:
                    connection.execute("DELETE FROM web_sessions WHERE session_hash = ?", (digest,))
                    Database.audit(
                        connection,
                        "ldap",
                        principal["username"],
                        "ldap_session_revoked",
                        "web_principal",
                        principal["principal_id"],
                        {},
                    )
                raise HTTPException(status_code=401, detail="directory authorization was revoked")
            except LDAPUnavailableError:
                raise HTTPException(status_code=503, detail="directory authorization recheck is unavailable")
            if (
                refreshed.get("principal_id") != principal["principal_id"]
                or refreshed.get("role") not in WEB_ROLES
            ):
                with database.transaction() as connection:
                    connection.execute("DELETE FROM web_sessions WHERE session_hash = ?", (digest,))
                    Database.audit(
                        connection,
                        "ldap",
                        principal["username"],
                        "ldap_session_revoked",
                        "web_principal",
                        principal["principal_id"],
                        {"reason": "principal or role no longer authorized"},
                    )
                raise HTTPException(status_code=401, detail="directory authorization was revoked")
            principal["role"] = refreshed["role"]
            principal["role_checked_at"] = now

        with database.transaction() as connection:
            if principal["role"] != row["role"] or principal["role_checked_at"] != row["role_checked_at"]:
                connection.execute(
                    "UPDATE web_sessions SET role = ?, role_checked_at = ? WHERE session_hash = ?",
                    (principal["role"], principal["role_checked_at"], digest),
                )
            if now - row["last_seen_at"] >= 60:
                connection.execute(
                    "UPDATE web_sessions SET last_seen_at = ? WHERE session_hash = ?",
                    (now, digest),
                )
        return principal

    def require_operator(principal: Dict[str, Any] = Depends(require_session)) -> Dict[str, Any]:
        if principal["role"] not in {"admin", "operator"}:
            raise HTTPException(status_code=403, detail="operator role required")
        return principal

    def require_superadmin(principal: Dict[str, Any] = Depends(require_session)) -> Dict[str, Any]:
        if principal["role"] != "admin":
            raise HTTPException(status_code=403, detail="administrator role required")
        return principal

    def require_agent(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
        if not authorization:
            raise HTTPException(status_code=401, detail="invalid credentials", headers={"WWW-Authenticate": "Bearer"})
        scheme, separator, value = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not value.strip():
            raise HTTPException(status_code=401, detail="invalid credentials", headers={"WWW-Authenticate": "Bearer"})
        digest = _token_hash(value.strip())
        with database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM nodes WHERE token_hash = ? AND revoked_at IS NULL", (digest,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="invalid credentials", headers={"WWW-Authenticate": "Bearer"})
        return dict(row)

    @api.get("/healthz")
    def healthz() -> Dict[str, Any]:
        try:
            with database.connection() as connection:
                connection.execute("SELECT 1").fetchone()
                configured = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM admin_users WHERE enabled = 1)"
                ).fetchone()[0]
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="database unavailable")
        return {
            "status": "ok",
            "database": "ok",
            "configured": bool(configured),
        }

    @api.get("/", include_in_schema=False)
    def index() -> Any:
        if settings.ui_path:
            candidate = Path(settings.ui_path).expanduser()
            if candidate.is_dir():
                candidate = candidate / "index.html"
            if candidate.is_file():
                return FileResponse(str(candidate), media_type="text/html; charset=utf-8")
        return JSONResponse(
            status_code=503,
            content={"detail": "UI file is not configured; set NGINX_MANAGER_UI_PATH"},
        )

    def login_rate_limited(client_key: str, now: int) -> bool:
        cutoff = now - settings.login_window_seconds
        with login_attempts_lock:
            recent = [item for item in login_attempts.get(client_key, []) if item > cutoff]
            login_attempts[client_key] = recent
            return len(recent) >= settings.login_max_attempts

    def record_login_failure(client_key: str, now: int) -> None:
        with login_attempts_lock:
            login_attempts.setdefault(client_key, []).append(now)

    @api.post("/api/v1/auth/login")
    def login(request: LoginRequest, http_request: Request) -> JSONResponse:
        now = int(time.time())
        client_key = http_request.client.host if http_request.client else "unknown"
        if login_rate_limited(client_key, now):
            raise HTTPException(status_code=429, detail="too many login attempts; try again later")
        with database.connection() as connection:
            user = connection.execute(
                "SELECT * FROM admin_users WHERE username = ? COLLATE NOCASE",
                (request.username,),
            ).fetchone()
        principal: Dict[str, str]
        if user is not None:
            if not user["enabled"] or not _verify_password(request.password, user["password_digest"]):
                record_login_failure(client_key, now)
                raise HTTPException(status_code=401, detail="invalid username or password")
            principal = {
                "principal_id": user["id"],
                "username": user["username"],
                "role": "admin",
                "auth_source": "local",
            }
        elif settings.ldap_enabled:
            try:
                principal = authenticate_ldap(settings, request.username, request.password)
            except LDAPAuthenticationError:
                record_login_failure(client_key, now)
                raise HTTPException(status_code=401, detail="invalid username or password")
            except LDAPUnavailableError:
                raise HTTPException(status_code=503, detail="directory authentication is unavailable")
            if (
                principal.get("role") not in WEB_ROLES
                or principal.get("auth_source") != "ldap"
                or not principal.get("principal_id")
                or not principal.get("username")
            ):
                raise HTTPException(status_code=503, detail="directory authentication returned an invalid identity")
        else:
            _verify_password(request.password, dummy_password_digest)
            record_login_failure(client_key, now)
            raise HTTPException(status_code=401, detail="invalid username or password")

        session_value = secrets.token_urlsafe(48)
        csrf_value = _derive_csrf_value(session_value)
        expires_at = now + settings.session_ttl_seconds
        with database.transaction() as connection:
            connection.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                """INSERT INTO web_sessions
                    (session_hash, principal_id, username, role, auth_source, csrf_hash,
                     created_at, expires_at, last_seen_at, role_checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _token_hash(session_value),
                    principal["principal_id"],
                    principal["username"],
                    principal["role"],
                    principal["auth_source"],
                    _token_hash(csrf_value),
                    now,
                    expires_at,
                    now,
                    now,
                ),
            )
            if principal["auth_source"] == "local":
                connection.execute(
                    "UPDATE admin_users SET last_login_at = ? WHERE id = ?",
                    (now, principal["principal_id"]),
                )
            Database.audit(
                connection,
                principal["auth_source"],
                principal["username"],
                "web_login",
                "web_principal",
                principal["principal_id"],
                {"role": principal["role"]},
            )
        with login_attempts_lock:
            login_attempts.pop(client_key, None)
        response = JSONResponse(
            content={
                "authenticated": True,
                "username": principal["username"],
                "role": principal["role"],
                "auth_source": principal["auth_source"],
                "csrf_token": csrf_value,
                "expires_at": _utc_iso(expires_at),
            }
        )
        session_cookie, secure_cookie = session_cookie_for(http_request)
        response.set_cookie(
            session_cookie,
            session_value,
            max_age=settings.session_ttl_seconds,
            path="/",
            secure=secure_cookie,
            httponly=True,
            samesite="strict",
        )
        return response

    @api.get("/api/v1/auth/session")
    def current_session(
        http_request: Request,
        admin: Dict[str, Any] = Depends(require_session),
    ) -> Dict[str, Any]:
        session_cookie, _secure = session_cookie_for(http_request)
        candidate = http_request.cookies.get(session_cookie, "")
        return {
            "authenticated": True,
            "username": admin["username"],
            "role": admin["role"],
            "auth_source": admin["auth_source"],
            "csrf_token": _derive_csrf_value(candidate),
            "expires_at": _utc_iso(admin["expires_at"]),
        }

    @api.post("/api/v1/auth/logout")
    def logout(http_request: Request, admin: Dict[str, Any] = Depends(require_session)) -> JSONResponse:
        session_cookie, secure_cookie = session_cookie_for(http_request)
        candidate = http_request.cookies.get(session_cookie, "")
        with database.transaction() as connection:
            connection.execute("DELETE FROM web_sessions WHERE session_hash = ?", (_token_hash(candidate),))
            Database.audit(
                connection,
                admin["auth_source"],
                admin["username"],
                "web_logout",
                "web_principal",
                admin["principal_id"],
                {"role": admin["role"]},
            )
        response = JSONResponse(content={"ok": True})
        response.delete_cookie(
            session_cookie,
            path="/",
            secure=secure_cookie,
            httponly=True,
            samesite="strict",
        )
        return response

    @api.post("/api/v1/agent/enroll")
    def enroll(request: EnrollRequest) -> Dict[str, Any]:
        now = int(time.time())
        enrollment_digest = _token_hash(request.enrollment_secret)
        labels_json = _canonical_json(request.labels)
        with database.transaction() as connection:
            existing_request = connection.execute(
                "SELECT * FROM agent_enrollments WHERE enrollment_id = ?",
                (request.enrollment_id,),
            ).fetchone()
            if existing_request is not None:
                if (
                    not hmac.compare_digest(existing_request["secret_hash"], enrollment_digest)
                    or existing_request["node_name"].lower() != request.node_name.lower()
                ):
                    raise HTTPException(status_code=401, detail="invalid enrollment request")
                request_status = existing_request["status"]
                if request_status == "pending" and existing_request["expires_at"] <= now:
                    request_status = "expired"
                    connection.execute(
                        "UPDATE agent_enrollments SET status = 'expired', updated_at = ? WHERE enrollment_id = ?",
                        (now, request.enrollment_id),
                    )
                elif request_status == "pending":
                    connection.execute(
                        """UPDATE agent_enrollments
                           SET hostname = ?, labels_json = ?, updated_at = ?
                           WHERE enrollment_id = ?""",
                        (request.hostname, labels_json, now, request.enrollment_id),
                    )
                response: Dict[str, Any] = {
                    "status": request_status,
                    "enrollment_id": request.enrollment_id,
                    "expires_at": _utc_iso(existing_request["expires_at"]),
                }
                if request_status == "approved":
                    response["agent_id"] = existing_request["node_id"]
                return response

            existing_node = connection.execute(
                "SELECT id FROM nodes WHERE node_name = ? COLLATE NOCASE",
                (request.node_name,),
            ).fetchone()
            node_id = existing_node["id"] if existing_node is not None else str(uuid.uuid4())
            credential = _derive_agent_credential(request.enrollment_secret, request.enrollment_id, node_id)
            expires_at = now + settings.enrollment_pending_ttl_seconds
            try:
                connection.execute(
                    """INSERT INTO agent_enrollments
                       (enrollment_id, node_id, node_name, hostname, labels_json, secret_hash,
                        credential_hash, status, requested_at, updated_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                    (
                        request.enrollment_id,
                        node_id,
                        request.node_name,
                        request.hostname,
                        labels_json,
                        enrollment_digest,
                        _token_hash(credential),
                        now,
                        now,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="enrollment request conflicts with an existing request")
            Database.audit(
                connection,
                "agent",
                request.enrollment_id,
                "agent_enrollment_requested",
                "node",
                node_id,
                {"node_name": request.node_name, "hostname": request.hostname},
            )
        return {
            "status": "pending",
            "enrollment_id": request.enrollment_id,
            "expires_at": _utc_iso(expires_at),
        }

    @api.post("/api/v1/agent/heartbeat")
    def heartbeat(request: HeartbeatRequest, agent: Dict[str, Any] = Depends(require_agent)) -> Dict[str, Any]:
        now = int(time.time())
        lease_renewed = False
        with database.transaction() as connection:
            connection.execute(
                """UPDATE nodes
                   SET last_seen_at = ?, updated_at = ?, reported_status = ?, agent_version = ?,
                       nginx_version = ?, config_hash = ?, capabilities_json = ?, facts_json = ?
                   WHERE id = ?""",
                (
                    now,
                    now,
                    request.status,
                    request.agent_version,
                    request.nginx_version,
                    request.config_hash,
                    _canonical_json(request.capabilities),
                    _canonical_json(request.facts),
                    agent["id"],
                ),
            )
            if request.active_job_id:
                cursor = connection.execute(
                    """UPDATE jobs
                       SET lease_expires_at = MIN(expires_at, ?)
                       WHERE id = ? AND node_id = ? AND status = 'running' AND expires_at > ?""",
                    (now + settings.job_lease_seconds, request.active_job_id, agent["id"], now),
                )
                lease_renewed = cursor.rowcount == 1
        return {
            "ok": True,
            "server_time": _utc_iso(now),
            "poll_interval_seconds": 5,
            "job_lease_renewed": lease_renewed,
        }

    @api.post("/api/v1/agent/poll")
    def poll(
        request: PollRequest = Body(default=PollRequest()),
        agent: Dict[str, Any] = Depends(require_agent),
    ) -> Dict[str, Any]:
        now = int(time.time())
        claimed: List[Dict[str, Any]] = []
        with database.transaction() as connection:
            Database.expire_jobs(connection, now)
            rows = connection.execute(
                """SELECT * FROM jobs
                   WHERE node_id = ?
                     AND expires_at > ?
                     AND (status = 'queued' OR (status = 'running' AND COALESCE(lease_expires_at, 0) <= ?))
                   ORDER BY created_at ASC, id ASC LIMIT ?""",
                (agent["id"], now, now, request.limit),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                cursor = connection.execute(
                    """UPDATE jobs
                       SET status = 'running', claimed_at = COALESCE(claimed_at, ?),
                           lease_expires_at = ?, attempt_count = attempt_count + 1
                       WHERE id = ? AND expires_at > ?
                         AND (status = 'queued' OR (status = 'running' AND COALESCE(lease_expires_at, 0) <= ?))""",
                    (now, now + settings.job_lease_seconds, row["id"], now, now),
                )
                if cursor.rowcount != 1:
                    continue
                claimed.append(
                    {
                        "id": row["id"],
                        "action": row["action"],
                        "payload": payload,
                        "operation_id": row["operation_id"],
                        "created_at": _utc_iso(row["created_at"]),
                        "expires_at": _utc_iso(row["expires_at"]),
                    }
                )
                Database.audit(
                    connection,
                    "agent",
                    agent["id"],
                    "job_claimed",
                    "job",
                    row["id"],
                    {"action": row["action"], "attempt": int(row["attempt_count"] or 0) + 1},
                )
                _refresh_operation(connection, row["operation_id"], now)
        return {
            "job": claimed[0] if claimed else None,
            "jobs": claimed,
            "server_time": _utc_iso(now),
        }

    @api.post("/api/v1/agent/jobs/{job_id}/result")
    def job_result(
        job_id: str,
        request: JobResultRequest,
        agent: Dict[str, Any] = Depends(require_agent),
    ) -> Dict[str, Any]:
        now = int(time.time())
        raw_result = request.model_dump() if hasattr(request, "model_dump") else request.dict()
        result_digest = _sha256_text(_canonical_json(raw_result))
        safe_metadata = _safe_result_metadata(request)
        with database.transaction() as connection:
            Database.expire_jobs(connection, now)
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["node_id"] != agent["id"]:
                raise HTTPException(status_code=404, detail="job not found")
            if request.job_id and request.job_id != job_id:
                raise HTTPException(status_code=409, detail="job id mismatch")
            if request.action and request.action != row["action"]:
                raise HTTPException(status_code=409, detail="job action mismatch")
            if row["status"] in TERMINAL_JOB_STATES:
                if row["status"] in {"succeeded", "failed"} and row["result_sha256"] == result_digest:
                    return {"accepted": True, "idempotent": True, "status": row["status"]}
                if row["status"] == "expired" and request.status == "expired":
                    return {"accepted": True, "idempotent": True, "status": "expired"}
                late_result_allowed = (
                    row["status"] == "expired"
                    and row["claimed_at"] is not None
                    and now <= int(row["expires_at"]) + settings.late_result_grace_seconds
                )
                if not late_result_allowed:
                    raise HTTPException(status_code=409, detail="job is already terminal")
            if row["status"] not in {"running", "expired"}:
                raise HTTPException(status_code=409, detail="job has not been claimed")

            connection.execute(
                """UPDATE jobs
                   SET status = ?, completed_at = ?, result_meta_json = ?, result_sha256 = ?,
                       payload_json = ?
                   WHERE id = ? AND status IN ('running', 'expired')""",
                (
                    request.status,
                    now,
                    _canonical_json(safe_metadata),
                    result_digest,
                    _canonical_json({"redacted": True, "reason": "completed"}),
                    job_id,
                ),
            )
            if safe_metadata.get("config_hash") or safe_metadata.get("nginx_version"):
                connection.execute(
                    """UPDATE nodes SET config_hash = COALESCE(?, config_hash),
                       nginx_version = COALESCE(?, nginx_version), updated_at = ? WHERE id = ?""",
                    (
                        safe_metadata.get("config_hash"),
                        safe_metadata.get("nginx_version"),
                        now,
                        agent["id"],
                    ),
                )
            Database.audit(
                connection,
                "agent",
                agent["id"],
                "late_job_result_received" if row["status"] == "expired" else "job_result_received",
                "job",
                job_id,
                {"status": request.status, "result_sha256": result_digest},
            )
            _refresh_operation(connection, row["operation_id"], now)
        return {"accepted": True, "idempotent": False, "status": request.status}

    @api.get("/api/v1/admin/nodes")
    def admin_nodes(admin: Dict[str, Any] = Depends(require_session)) -> Dict[str, Any]:
        now = int(time.time())
        with database.connection() as connection:
            rows = connection.execute("SELECT * FROM nodes ORDER BY node_name COLLATE NOCASE").fetchall()
        return {"items": [_node_public(row, now, settings.online_after_seconds) for row in rows]}

    @api.post("/api/v1/admin/nodes/{node_id}/revoke")
    def revoke_node(
        node_id: str,
        admin: Dict[str, Any] = Depends(require_superadmin),
    ) -> Dict[str, Any]:
        now = int(time.time())
        with database.transaction() as connection:
            row = connection.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="node not found")
            if row["revoked_at"] is not None:
                return {"revoked": True, "idempotent": True, "node_id": node_id}
            affected = connection.execute(
                "SELECT DISTINCT operation_id FROM jobs WHERE node_id = ? AND status IN ('queued', 'running')",
                (node_id,),
            ).fetchall()
            connection.execute(
                """UPDATE nodes SET token_hash = ?, revoked_at = ?, reported_status = 'offline', updated_at = ?
                   WHERE id = ?""",
                (_token_hash(secrets.token_urlsafe(48)), now, now, node_id),
            )
            connection.execute(
                """UPDATE jobs SET status = 'expired', completed_at = ?, payload_json = ?,
                   result_meta_json = ? WHERE node_id = ? AND status IN ('queued', 'running')""",
                (
                    now,
                    _canonical_json({"redacted": True, "reason": "node revoked"}),
                    _canonical_json({"failure_code": "job_expired", "failure_stage": "queue"}),
                    node_id,
                ),
            )
            for item in affected:
                _refresh_operation(connection, item["operation_id"], now)
            Database.audit(
                connection,
                admin["auth_source"],
                admin["username"],
                "node_revoked",
                "node",
                node_id,
                {"node_name": row["node_name"]},
            )
        return {"revoked": True, "idempotent": False, "node_id": node_id}

    @api.get("/api/v1/admin/jobs")
    def admin_jobs(
        job_status: Optional[str] = Query(None, alias="status"),
        action: Optional[str] = Query(None),
        node_id: Optional[str] = Query(None),
        batch_id: Optional[str] = Query(None),
        operation_id: Optional[str] = Query(None),
        job_ids: Optional[str] = Query(None, alias="ids", max_length=20000),
        limit: int = Query(100, ge=1, le=500),
        admin: Dict[str, Any] = Depends(require_session),
    ) -> Dict[str, Any]:
        if job_status and job_status not in ACTIVE_JOB_STATES | TERMINAL_JOB_STATES:
            raise HTTPException(status_code=400, detail="invalid job status")
        if action and action not in JOB_ACTIONS:
            raise HTTPException(status_code=400, detail="invalid action")
        now = int(time.time())
        conditions: List[str] = []
        parameters: List[Any] = []
        if job_status:
            conditions.append("j.status = ?")
            parameters.append(job_status)
        if action:
            conditions.append("j.action = ?")
            parameters.append(action)
        if node_id:
            conditions.append("j.node_id = ?")
            parameters.append(node_id)
        if batch_id:
            conditions.append("j.batch_id = ?")
            parameters.append(batch_id)
        if operation_id:
            conditions.append("j.operation_id = ?")
            parameters.append(operation_id)
        if job_ids:
            requested_ids = [item.strip() for item in job_ids.split(",") if item.strip()]
            if not requested_ids or len(requested_ids) > 500 or any(len(item) > 200 for item in requested_ids):
                raise HTTPException(status_code=400, detail="invalid job ids")
            conditions.append("j.id IN (" + ",".join("?" for _ in requested_ids) + ")")
            parameters.extend(requested_ids)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(limit)
        with database.transaction() as connection:
            Database.expire_jobs(connection, now)
            rows = connection.execute(
                """SELECT j.*, n.node_name FROM jobs j JOIN nodes n ON n.id = j.node_id"""
                + where
                + " ORDER BY j.created_at DESC, j.id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return {"items": [_job_public(row) for row in rows]}

    @api.post("/api/v1/admin/jobs", status_code=201)
    def create_jobs(request: AdminJobRequest, admin: Dict[str, Any] = Depends(require_operator)) -> Dict[str, Any]:
        payload_json = _canonical_json(request.payload)
        payload_bytes = len(payload_json.encode("utf-8"))
        if payload_bytes > settings.max_payload_bytes:
            raise HTTPException(status_code=413, detail="job payload is too large")
        sensitive = request.action == "certificate_apply" or _contains_sensitive_material(request.payload)
        ttl = request.ttl_seconds or settings.default_job_ttl_seconds
        ttl = min(ttl, settings.max_job_ttl_seconds)
        if sensitive:
            ttl = min(ttl, settings.sensitive_job_ttl_seconds)
        now = int(time.time())
        expires_at = now + ttl
        batch_id = str(uuid.uuid4())
        payload_digest = _sha256_text(payload_json)
        created: List[Dict[str, Any]] = []

        with database.transaction() as connection:
            placeholders = ",".join("?" for _ in request.node_ids)
            existing_rows = connection.execute(
                "SELECT id FROM nodes WHERE id IN (" + placeholders + ")", request.node_ids
            ).fetchall()
            existing_ids = {row["id"] for row in existing_rows}
            missing = [node_id for node_id in request.node_ids if node_id not in existing_ids]
            if missing:
                raise HTTPException(status_code=404, detail={"message": "unknown node ids", "node_ids": missing})
            for node_id in request.node_ids:
                job_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO jobs
                       (id, batch_id, node_id, action, status, payload_json, payload_sha256,
                         payload_sensitive, created_at, expires_at, created_by)
                       VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
                    (
                        job_id,
                        batch_id,
                        node_id,
                        request.action,
                        payload_json,
                        payload_digest,
                        1 if sensitive else 0,
                        now,
                        expires_at,
                        admin["username"],
                    ),
                )
                created.append(
                    {"id": job_id, "node_id": node_id, "status": "queued", "expires_at": _utc_iso(expires_at)}
                )
            Database.audit(
                connection,
                admin["auth_source"],
                admin["username"],
                "jobs_created",
                "job_batch",
                batch_id,
                {
                    "action": request.action,
                    "count": len(created),
                    "node_ids": request.node_ids,
                    "payload_sha256": payload_digest,
                    "payload_sensitive": sensitive,
                    "ttl_seconds": ttl,
                    "role": admin["role"],
                },
            )
        return {"batch_id": batch_id, "jobs": created}

    @api.post("/api/v1/admin/operations", status_code=201)
    def create_operation(
        request: OperationCreateRequest,
        admin: Dict[str, Any] = Depends(require_operator),
    ) -> Dict[str, Any]:
        raw_request = request.model_dump() if hasattr(request, "model_dump") else request.dict()
        request_digest = _sha256_text(_canonical_json(raw_request))
        operation_id = request.request_id or str(uuid.uuid4())
        now = int(time.time())
        ttl = min(request.ttl_seconds or settings.default_job_ttl_seconds, settings.max_job_ttl_seconds)
        candidate_json = _canonical_json(request.candidate)
        if _contains_sensitive_material(request.candidate):
            raise HTTPException(status_code=400, detail="operation candidate must not contain secrets or private keys")
        if len(candidate_json.encode("utf-8")) > settings.max_ui_state_bytes:
            raise HTTPException(status_code=413, detail="operation candidate is too large")
        prepared_jobs: List[Tuple[OperationJobRequest, str, str, bool, int]] = []
        total_payload_bytes = 0
        for spec in request.jobs:
            payload_json = _canonical_json(spec.payload)
            payload_bytes = len(payload_json.encode("utf-8"))
            if payload_bytes > settings.max_payload_bytes:
                raise HTTPException(status_code=413, detail="job payload is too large")
            total_payload_bytes += payload_bytes
            sensitive = spec.action == "certificate_apply" or _contains_sensitive_material(spec.payload)
            job_ttl = min(ttl, settings.sensitive_job_ttl_seconds) if sensitive else ttl
            prepared_jobs.append((spec, payload_json, _sha256_text(payload_json), sensitive, job_ttl))
        if total_payload_bytes > settings.max_payload_bytes * 4:
            raise HTTPException(status_code=413, detail="operation payloads are too large")

        created: List[Dict[str, Any]] = []
        with database.transaction() as connection:
            existing_operation = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if existing_operation is not None:
                metadata = json.loads(existing_operation["metadata_json"] or "{}")
                if metadata.get("request_sha256") != request_digest:
                    raise HTTPException(status_code=409, detail="operation request id was reused with different content")
                rows = connection.execute(
                    "SELECT j.*, n.node_name FROM jobs j JOIN nodes n ON n.id = j.node_id "
                    "WHERE j.operation_id = ? ORDER BY j.created_at, j.id",
                    (operation_id,),
                ).fetchall()
                return {
                    "operation": _operation_public(existing_operation),
                    "jobs": [_job_public(row) for row in rows],
                    "idempotent": True,
                }

            node_ids = [spec.node_id for spec, _payload, _digest, _sensitive, _ttl in prepared_jobs]
            placeholders = ",".join("?" for _ in node_ids)
            existing_nodes = connection.execute(
                "SELECT id FROM nodes WHERE id IN (" + placeholders + ") AND revoked_at IS NULL",
                node_ids,
            ).fetchall()
            existing_ids = {row["id"] for row in existing_nodes}
            missing = [node_id for node_id in node_ids if node_id not in existing_ids]
            if missing:
                raise HTTPException(status_code=404, detail={"message": "unknown or revoked node ids", "node_ids": missing})

            revision_id: Optional[str] = None
            if request.kind == "publish":
                latest = connection.execute(
                    "SELECT MAX(version) AS version FROM site_revisions WHERE site_id = ? AND published_at IS NOT NULL",
                    (request.site_id,),
                ).fetchone()["version"]
                if latest is not None and int(latest) != request.base_version:
                    raise HTTPException(status_code=409, detail="site base version is stale")
                revision_id = operation_id + ":revision"
                revision_version = request.base_version + 1
                snapshot_digest = _sha256_text(candidate_json)
                existing_revision = connection.execute(
                    "SELECT * FROM site_revisions WHERE site_id = ? AND version = ?",
                    (request.site_id, revision_version),
                ).fetchone()
                if existing_revision is not None:
                    active_owner = connection.execute(
                        "SELECT id FROM operations WHERE candidate_revision_id = ? AND status IN ('queued', 'running')",
                        (existing_revision["id"],),
                    ).fetchone()
                    if existing_revision["published_at"] is not None or active_owner is not None:
                        raise HTTPException(status_code=409, detail="candidate site version already exists")
                    connection.execute("DELETE FROM site_revisions WHERE id = ?", (existing_revision["id"],))
                connection.execute(
                    """INSERT INTO site_revisions
                       (id, site_id, version, snapshot_json, snapshot_sha256, note, created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        revision_id,
                        request.site_id,
                        revision_version,
                        candidate_json,
                        snapshot_digest,
                        str(request.candidate.get("changeNote") or request.candidate.get("note") or "")[:1000],
                        admin["username"],
                        now,
                    ),
                )

            metadata = {
                "request_sha256": request_digest,
                "job_count": len(prepared_jobs),
                "node_ids": node_ids,
            }
            connection.execute(
                """INSERT INTO operations
                   (id, site_id, kind, status, base_version, candidate_revision_id, created_by,
                    created_at, updated_at, metadata_json)
                   VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
                (
                    operation_id,
                    request.site_id,
                    request.kind,
                    request.base_version,
                    revision_id,
                    admin["username"],
                    now,
                    now,
                    _canonical_json(metadata),
                ),
            )
            for spec, payload_json, payload_digest, sensitive, job_ttl in prepared_jobs:
                job_id = str(uuid.uuid4())
                expires_at = now + job_ttl
                connection.execute(
                    """INSERT INTO jobs
                       (id, batch_id, operation_id, node_id, action, status, payload_json,
                        payload_sha256, payload_sensitive, created_at, expires_at, created_by)
                       VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
                    (
                        job_id,
                        operation_id,
                        operation_id,
                        spec.node_id,
                        spec.action,
                        payload_json,
                        payload_digest,
                        1 if sensitive else 0,
                        now,
                        expires_at,
                        admin["username"],
                    ),
                )
                created.append({
                    "id": job_id,
                    "node_id": spec.node_id,
                    "status": "queued",
                    "operation_id": operation_id,
                    "expires_at": _utc_iso(expires_at),
                })
            Database.audit(
                connection,
                admin["auth_source"],
                admin["username"],
                "operation_created",
                "operation",
                operation_id,
                {
                    "site_id": request.site_id,
                    "kind": request.kind,
                    "base_version": request.base_version,
                    "job_count": len(created),
                    "request_sha256": request_digest,
                },
            )
            operation = connection.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
        return {"operation": _operation_public(operation), "jobs": created, "idempotent": False}

    @api.get("/api/v1/admin/operations")
    def list_operations(
        operation_status: Optional[str] = Query(None, alias="status"),
        site_id: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
        admin: Dict[str, Any] = Depends(require_session),
    ) -> Dict[str, Any]:
        if operation_status and operation_status not in OPERATION_STATES and operation_status != "active":
            raise HTTPException(status_code=400, detail="invalid operation status")
        conditions: List[str] = []
        parameters: List[Any] = []
        if operation_status == "active":
            conditions.append("status IN ('queued', 'running')")
        elif operation_status:
            conditions.append("status = ?")
            parameters.append(operation_status)
        if site_id:
            conditions.append("site_id = ?")
            parameters.append(site_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(limit)
        with database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM operations" + where + " ORDER BY updated_at DESC, id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return {"items": [_operation_public(row) for row in rows]}

    @api.get("/api/v1/admin/operations/{operation_id}")
    def get_operation(operation_id: str, admin: Dict[str, Any] = Depends(require_session)) -> Dict[str, Any]:
        with database.connection() as connection:
            operation = connection.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
            if operation is None:
                raise HTTPException(status_code=404, detail="operation not found")
            jobs = connection.execute(
                "SELECT j.*, n.node_name FROM jobs j JOIN nodes n ON n.id = j.node_id "
                "WHERE j.operation_id = ? ORDER BY j.created_at, j.id",
                (operation_id,),
            ).fetchall()
        return {"operation": _operation_public(operation), "jobs": [_job_public(row) for row in jobs]}

    @api.get("/api/v1/admin/sites/{site_id}/revisions")
    def list_site_revisions(
        site_id: str,
        limit: int = Query(100, ge=1, le=500),
        admin: Dict[str, Any] = Depends(require_session),
    ) -> Dict[str, Any]:
        with database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM site_revisions WHERE site_id = ? AND published_at IS NOT NULL "
                "ORDER BY version DESC LIMIT ?",
                (site_id, limit),
            ).fetchall()
        return {"items": [_revision_public(row) for row in rows]}

    @api.get("/api/v1/admin/sites/{site_id}/revisions/{version}")
    def get_site_revision(
        site_id: str,
        version: int,
        admin: Dict[str, Any] = Depends(require_session),
    ) -> Dict[str, Any]:
        with database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM site_revisions WHERE site_id = ? AND version = ? AND published_at IS NOT NULL",
                (site_id, version),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="published site revision not found")
        return _revision_public(row, include_snapshot=True)

    @api.get("/api/v1/admin/enrollments")
    def list_enrollments(
        enrollment_status: Optional[str] = Query("pending", alias="status"),
        limit: int = Query(100, ge=1, le=500),
        admin: Dict[str, Any] = Depends(require_session),
    ) -> Dict[str, Any]:
        allowed = {"pending", "approved", "rejected", "expired", "all"}
        if enrollment_status not in allowed:
            raise HTTPException(status_code=400, detail="invalid enrollment status")
        now = int(time.time())
        with database.transaction() as connection:
            connection.execute(
                """UPDATE agent_enrollments SET status = 'expired', updated_at = ?
                   WHERE status = 'pending' AND expires_at <= ?""",
                (now, now),
            )
            if enrollment_status == "all":
                rows = connection.execute(
                    "SELECT * FROM agent_enrollments ORDER BY requested_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM agent_enrollments
                       WHERE status = ? ORDER BY requested_at DESC LIMIT ?""",
                    (enrollment_status, limit),
                ).fetchall()
        return {"items": [_enrollment_public(row) for row in rows]}

    @api.post("/api/v1/admin/enrollments/{enrollment_id}/approve")
    def approve_enrollment(
        enrollment_id: str,
        admin: Dict[str, Any] = Depends(require_superadmin),
    ) -> Dict[str, Any]:
        now = int(time.time())
        with database.transaction() as connection:
            pending = connection.execute(
                "SELECT * FROM agent_enrollments WHERE enrollment_id = ?",
                (enrollment_id,),
            ).fetchone()
            if pending is None:
                raise HTTPException(status_code=404, detail="enrollment request not found")
            if pending["status"] == "approved":
                return {"approved": True, "idempotent": True, "agent_id": pending["node_id"]}
            if pending["status"] != "pending":
                raise HTTPException(status_code=409, detail="enrollment request is no longer pending")
            if pending["expires_at"] <= now:
                connection.execute(
                    "UPDATE agent_enrollments SET status = 'expired', updated_at = ? WHERE enrollment_id = ?",
                    (now, enrollment_id),
                )
                raise HTTPException(status_code=409, detail="enrollment request expired")

            node = connection.execute(
                "SELECT id FROM nodes WHERE node_name = ? COLLATE NOCASE",
                (pending["node_name"],),
            ).fetchone()
            if node is not None and node["id"] != pending["node_id"]:
                raise HTTPException(status_code=409, detail="node identity changed; request the Agent to enroll again")
            if node is None:
                connection.execute(
                    """INSERT INTO nodes
                       (id, node_name, hostname, labels_json, token_hash, enrolled_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pending["node_id"],
                        pending["node_name"],
                        pending["hostname"],
                        pending["labels_json"],
                        pending["credential_hash"],
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE nodes
                       SET hostname = ?, labels_json = ?, token_hash = ?, enrolled_at = ?, updated_at = ?,
                           last_seen_at = NULL, reported_status = 'offline', revoked_at = NULL
                       WHERE id = ?""",
                    (
                        pending["hostname"],
                        pending["labels_json"],
                        pending["credential_hash"],
                        now,
                        now,
                        pending["node_id"],
                    ),
                )
            connection.execute(
                """UPDATE agent_enrollments
                   SET status = 'approved', updated_at = ?, decided_at = ?, decided_by = ?, expires_at = ?
                   WHERE enrollment_id = ?""",
                (now, now, admin["username"], now + 7 * 86400, enrollment_id),
            )
            connection.execute(
                """UPDATE agent_enrollments
                   SET status = 'rejected', updated_at = ?, decided_at = ?, decided_by = ?
                   WHERE node_name = ? COLLATE NOCASE AND status = 'pending' AND enrollment_id <> ?""",
                (now, now, admin["username"], pending["node_name"], enrollment_id),
            )
            Database.audit(
                connection,
                admin["auth_source"],
                admin["username"],
                "agent_enrollment_approved",
                "node",
                pending["node_id"],
                {"enrollment_id": enrollment_id, "node_name": pending["node_name"], "role": admin["role"]},
            )
        return {"approved": True, "idempotent": False, "agent_id": pending["node_id"]}

    @api.post("/api/v1/admin/enrollments/{enrollment_id}/reject")
    def reject_enrollment(
        enrollment_id: str,
        admin: Dict[str, Any] = Depends(require_superadmin),
    ) -> Dict[str, Any]:
        now = int(time.time())
        with database.transaction() as connection:
            pending = connection.execute(
                "SELECT * FROM agent_enrollments WHERE enrollment_id = ?",
                (enrollment_id,),
            ).fetchone()
            if pending is None:
                raise HTTPException(status_code=404, detail="enrollment request not found")
            if pending["status"] == "rejected":
                return {"rejected": True, "idempotent": True}
            if pending["status"] != "pending":
                raise HTTPException(status_code=409, detail="enrollment request is no longer pending")
            connection.execute(
                """UPDATE agent_enrollments
                   SET status = 'rejected', updated_at = ?, decided_at = ?, decided_by = ?
                   WHERE enrollment_id = ?""",
                (now, now, admin["username"], enrollment_id),
            )
            Database.audit(
                connection,
                admin["auth_source"],
                admin["username"],
                "agent_enrollment_rejected",
                "node",
                pending["node_id"],
                {"enrollment_id": enrollment_id, "node_name": pending["node_name"], "role": admin["role"]},
            )
        return {"rejected": True, "idempotent": False}

    @api.get("/api/v1/admin/audit")
    def list_audit(
        before_id: Optional[int] = Query(None, ge=1),
        actor: Optional[str] = Query(None, max_length=128),
        event: Optional[str] = Query(None, max_length=128),
        limit: int = Query(100, ge=1, le=500),
        admin: Dict[str, Any] = Depends(require_session),
    ) -> Dict[str, Any]:
        conditions: List[str] = []
        parameters: List[Any] = []
        if before_id is not None:
            conditions.append("id < ?")
            parameters.append(before_id)
        if actor:
            conditions.append("actor_id = ?")
            parameters.append(actor)
        if event:
            conditions.append("event = ?")
            parameters.append(event)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(limit)
        with database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit" + where + " ORDER BY id DESC LIMIT ?", parameters
            ).fetchall()
        return {
            "items": [
                {
                    "id": row["id"],
                    "created_at": _utc_iso(row["created_at"]),
                    "actor_type": row["actor_type"],
                    "actor_id": row["actor_id"],
                    "event": row["event"],
                    "target_type": row["target_type"],
                    "target_id": row["target_id"],
                    "detail": json.loads(row["detail_json"] or "{}"),
                }
                for row in rows
            ],
            "next_before_id": rows[-1]["id"] if len(rows) == limit else None,
        }

    @api.post("/api/v1/admin/maintenance/prune")
    def prune_data(
        vacuum: bool = Query(False),
        admin: Dict[str, Any] = Depends(require_superadmin),
    ) -> Dict[str, Any]:
        now = int(time.time())
        with database.transaction() as connection:
            removed = Database.prune(
                connection,
                now,
                settings.job_retention_seconds,
                settings.audit_retention_seconds,
            )
            Database.audit(
                connection,
                admin["auth_source"],
                admin["username"],
                "maintenance_pruned",
                "database",
                None,
                {"removed": removed, "vacuum": vacuum},
            )
        if vacuum:
            with database.connection() as connection:
                connection.execute("VACUUM")
        return {"removed": removed, "vacuumed": vacuum}

    @api.get("/api/v1/admin/snapshot")
    def snapshot(admin: Dict[str, Any] = Depends(require_session)) -> Dict[str, Any]:
        now = int(time.time())
        with database.transaction() as connection:
            Database.expire_jobs(connection, now)
            node_rows = connection.execute("SELECT * FROM nodes ORDER BY node_name COLLATE NOCASE").fetchall()
            job_counts = {
                row["status"]: row["count"]
                for row in connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
            }
            pending_enrollments = connection.execute(
                "SELECT COUNT(*) FROM agent_enrollments WHERE status = 'pending' AND expires_at > ?",
                (now,),
            ).fetchone()[0]
        public_nodes = [_node_public(row, now, settings.online_after_seconds) for row in node_rows]
        online = sum(1 for node in public_nodes if node["status"] != "offline")
        return {
            "server_time": _utc_iso(now),
            "nodes": {"total": len(public_nodes), "online": online, "offline": len(public_nodes) - online},
            "jobs": {state: int(job_counts.get(state, 0)) for state in sorted(ACTIVE_JOB_STATES | TERMINAL_JOB_STATES)},
            "enrollments": {"pending": int(pending_enrollments)},
        }

    @api.get("/api/v1/admin/ui-state")
    def get_ui_state(admin: Dict[str, Any] = Depends(require_session)) -> Dict[str, Any]:
        with database.connection() as connection:
            return _load_ui_state_document(connection)

    @api.put("/api/v1/admin/ui-state")
    def put_ui_state(request: UIStatePutRequest, admin: Dict[str, Any] = Depends(require_operator)) -> Any:
        if _contains_sensitive_material(request.state):
            raise HTTPException(status_code=400, detail="UI state must not contain tokens, secrets, passwords, or private keys")
        full_state_json = _canonical_json(request.state)
        if len(full_state_json.encode("utf-8")) > settings.max_ui_state_bytes:
            raise HTTPException(status_code=413, detail="UI state is too large")
        resources: Dict[str, List[Tuple[str, str, str]]] = {"site": [], "certificate": []}
        for kind, state_key in (("site", "sites"), ("certificate", "certificates")):
            documents = request.state.get(state_key, [])
            if not isinstance(documents, list):
                raise HTTPException(status_code=400, detail=state_key + " must be an array")
            seen_ids = set()
            for document in documents:
                if not isinstance(document, dict):
                    raise HTTPException(status_code=400, detail=state_key + " contains an invalid document")
                resource_id = str(document.get("id") or "")
                if not resource_id:
                    resource_id = "legacy-" + _sha256_text(_canonical_json(document))[:24]
                if re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", resource_id) is None or resource_id in seen_ids:
                    raise HTTPException(status_code=400, detail=state_key + " contains an invalid or duplicate id")
                seen_ids.add(resource_id)
                document_json = _canonical_json(document)
                if len(document_json.encode("utf-8")) > settings.max_resource_bytes:
                    raise HTTPException(status_code=413, detail=kind + " resource is too large")
                resources[kind].append((resource_id, document_json, _sha256_text(document_json)))
        compact_state = dict(request.state)
        compact_state.pop("sites", None)
        compact_state.pop("certificates", None)
        compact_state["_resources_split_v1"] = True
        state_json = _canonical_json(compact_state)
        now = int(time.time())
        with database.transaction() as connection:
            current = connection.execute(
                "SELECT revision, state_json FROM ui_state WHERE singleton_id = 1"
            ).fetchone()
            if current["revision"] != request.revision:
                authoritative = _load_ui_state_document(connection)
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": "revision conflict",
                        "revision": authoritative["revision"],
                        "state": authoritative["state"],
                    },
                )
            for kind, documents in resources.items():
                ids = [item[0] for item in documents]
                if ids:
                    connection.execute(
                        "DELETE FROM resources WHERE kind = ? AND id NOT IN ("
                        + ",".join("?" for _ in ids) + ")",
                        [kind] + ids,
                    )
                else:
                    connection.execute("DELETE FROM resources WHERE kind = ?", (kind,))
                for position, (resource_id, document_json, document_digest) in enumerate(documents):
                    existing = connection.execute(
                        "SELECT revision, document_sha256 FROM resources WHERE kind = ? AND id = ?",
                        (kind, resource_id),
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            """INSERT INTO resources
                               (kind, id, revision, position, document_json, document_sha256, updated_at, updated_by)
                               VALUES (?, ?, 1, ?, ?, ?, ?, ?)""",
                            (kind, resource_id, position, document_json, document_digest, now, admin["username"]),
                        )
                    else:
                        next_resource_revision = int(existing["revision"]) + (
                            1 if existing["document_sha256"] != document_digest else 0
                        )
                        connection.execute(
                            """UPDATE resources SET revision = ?, position = ?, document_json = ?,
                               document_sha256 = ?, updated_at = ?, updated_by = ?
                               WHERE kind = ? AND id = ?""",
                            (
                                next_resource_revision,
                                position,
                                document_json,
                                document_digest,
                                now,
                                admin["username"],
                                kind,
                                resource_id,
                            ),
                        )
            next_revision = request.revision + 1
            connection.execute(
                "UPDATE ui_state SET revision = ?, state_json = ?, updated_at = ? WHERE singleton_id = 1",
                (next_revision, state_json, now),
            )
            Database.audit(
                connection,
                admin["auth_source"],
                admin["username"],
                "ui_state_updated",
                "ui_state",
                "1",
                {"revision": next_revision, "state_sha256": _sha256_text(full_state_json), "role": admin["role"]},
            )
        return {"revision": next_revision, "state": request.state}

    return api


def _bootstrap_cli() -> int:
    parser = argparse.ArgumentParser(description="Nginx Manager control-plane utility")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("bootstrap-admin", help="create the first Web administrator if none exists")
    arguments = parser.parse_args()

    if arguments.command == "bootstrap-admin":
        settings = Settings.from_env()
        username = os.environ.get("NGINX_MANAGER_BOOTSTRAP_USERNAME", "admin")
        password = os.environ.get("NGINX_MANAGER_BOOTSTRAP_PASSWORD", "")
        if not password:
            parser.error("NGINX_MANAGER_BOOTSTRAP_PASSWORD must be set only for this bootstrap command")
        database = Database(settings.db_path)
        database.initialize()
        outcome = database.bootstrap_admin(username, password, settings.password_iterations)
        # Never print the plaintext password or its digest.
        print(_canonical_json(outcome))
        return 0
    return 2


app = create_app()


if __name__ == "__main__":
    raise SystemExit(_bootstrap_cli())
