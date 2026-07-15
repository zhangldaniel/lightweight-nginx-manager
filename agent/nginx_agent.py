#!/usr/bin/env python3
"""Lightweight Linux agent for nginx-manager.

The network-facing agent runs as an unprivileged user.  Privileged filesystem and
nginx operations are executed by the separate ``helper`` subcommand over an
authenticated Unix-domain socket.  The helper accepts only the fixed actions in
``CAPABILITIES``; there is deliberately no shell/command action.

Control-plane protocol (all request and response bodies are JSON):

* POST /api/v1/agent/enroll
* POST /api/v1/agent/heartbeat       (machine credential)
* POST /api/v1/agent/poll            (machine credential)
* POST /api/v1/agent/jobs/{id}/result (machine credential)

Python 3.6+ and the standard library are sufficient.
"""

import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import hmac
import http.client
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # Linux process-wide file locking; tests can still run on non-Linux hosts.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows development only
    fcntl = None

try:  # Linux account/group lookups.
    import grp  # type: ignore
    import pwd  # type: ignore
except ImportError:  # pragma: no cover - Windows development only
    grp = None
    pwd = None


VERSION = "0.5.0"
CAPABILITIES = (
    "inspect",
    "nginx_test",
    "nginx_reload",
    "config_inventory",
    "config_read",
    "config_hash",
    "config_apply",
    "config_delete",
    "certificate_apply",
)
INVENTORY_MAX_FILES = 200
INVENTORY_MAX_FILE_BYTES = 256 * 1024
INVENTORY_MAX_TOTAL_BYTES = 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRANSACTION_ID_RE = re.compile(r"^[0-9a-f]{64}$")
TRANSACTION_PHASES = {
    "prepared", "replacing", "replaced", "testing", "validated",
    "reloading", "reloaded", "health_checking", "health_checked",
    "rolling_back", "recovering", "recovery_failed", "recovered", "committed",
}
RECOVERY_RELOAD_PHASES = {"reloading", "reloaded", "health_checking", "health_checked"}
DIRECTIVE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NGINX_TOKEN_RE = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[{};]|[^\s{};]+''')
LOG = logging.getLogger("nginx-manager-agent")
_THREAD_LOCK = threading.RLock()


class AgentError(Exception):
    """An expected and safe-to-return agent error."""


class ApiError(AgentError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class ActionError(AgentError):
    pass


class CommandError(ActionError):
    pass


class Settings:
    """Validated agent settings without a dependency on Python 3.7 dataclasses."""

    def __init__(
        self,
        server_url: str,
        node_name: str,
        hostname: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
        ca_file: Optional[str] = None,
        tls_skip_verify: bool = False,
        allow_insecure_http: bool = False,
        poll_interval: float = 3.0,
        heartbeat_interval: float = 20.0,
        api_timeout: float = 30.0,
        command_timeout: float = 20.0,
        nginx_binary: str = "/usr/sbin/nginx",
        openssl_binary: str = "/usr/bin/openssl",
        nginx_config: str = "/etc/nginx/nginx.conf",
        nginx_root: str = "/etc/nginx",
        allowed_config_roots: Optional[List[str]] = None,
        allowed_certificate_roots: Optional[List[str]] = None,
        state_dir: str = "/var/lib/nginx-manager-agent",
        helper_state_dir: str = "/var/lib/nginx-manager-agent-helper",
        helper_socket: str = "/run/nginx-manager-agent/helper.sock",
        helper_timeout: float = 120.0,
        helper_max_request_bytes: int = 8 * 1024 * 1024,
        max_file_bytes: int = 4 * 1024 * 1024,
        max_command_output_bytes: int = 32 * 1024,
        backup_retention: int = 20,
        health_check: Optional[Dict[str, Any]] = None,
        allowed_health_hosts: Optional[List[str]] = None,
    ):
        self.server_url = server_url
        self.node_name = node_name
        self.hostname = socket.gethostname() if hostname is None else hostname
        self.labels = {} if labels is None else dict(labels)
        self.ca_file = ca_file
        self.tls_skip_verify = tls_skip_verify
        self.allow_insecure_http = allow_insecure_http
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.api_timeout = api_timeout
        self.command_timeout = command_timeout
        self.nginx_binary = nginx_binary
        self.openssl_binary = openssl_binary
        self.nginx_config = nginx_config
        self.nginx_root = nginx_root
        self.allowed_config_roots = (
            ["/etc/nginx/nginx-manager.d"]
            if allowed_config_roots is None
            else list(allowed_config_roots)
        )
        self.allowed_certificate_roots = (
            ["/etc/nginx/ssl/nginx-manager"]
            if allowed_certificate_roots is None
            else list(allowed_certificate_roots)
        )
        self.state_dir = state_dir
        self.helper_state_dir = helper_state_dir
        self.helper_socket = helper_socket
        self.helper_timeout = helper_timeout
        self.helper_max_request_bytes = helper_max_request_bytes
        self.max_file_bytes = max_file_bytes
        self.max_command_output_bytes = max_command_output_bytes
        self.backup_retention = backup_retention
        self.health_check = health_check
        self.allowed_health_hosts = (
            ["127.0.0.1", "::1", "localhost"]
            if allowed_health_hosts is None
            else list(allowed_health_hosts)
        )

    @classmethod
    def load(cls, path: str) -> "Settings":
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            raise AgentError("cannot load config {}: {}".format(path, exc))
        if not isinstance(raw, dict):
            raise AgentError("configuration root must be a JSON object")

        health = raw.get("health_check")
        if health is None and raw.get("health_url"):
            health = {
                "url": raw["health_url"],
                "expected_status": raw.get("health_expected_status", 200),
                "timeout": raw.get("health_timeout", 5),
                "attempts": raw.get("health_attempts", 3),
            }
        nginx_root = str(raw.get("nginx_root", "/etc/nginx"))
        settings = cls(
            server_url=str(raw.get("server_url", "")).rstrip("/"),
            node_name=str(raw.get("node_name", "")).strip(),
            hostname=str(raw.get("hostname") or socket.gethostname()),
            labels=_string_dict(raw.get("labels", {}), "labels"),
            ca_file=_optional_string(raw.get("ca_file")),
            tls_skip_verify=bool(raw.get("tls_skip_verify", False)),
            allow_insecure_http=bool(raw.get("allow_insecure_http", False)),
            poll_interval=float(raw.get("poll_interval", 3)),
            heartbeat_interval=float(raw.get("heartbeat_interval", 20)),
            api_timeout=float(raw.get("api_timeout", 30)),
            command_timeout=float(raw.get("command_timeout", 20)),
            nginx_binary=str(raw.get("nginx_binary", "/usr/sbin/nginx")),
            openssl_binary=str(raw.get("openssl_binary", "/usr/bin/openssl")),
            nginx_config=str(raw.get("nginx_config", "/etc/nginx/nginx.conf")),
            nginx_root=nginx_root,
            allowed_config_roots=[str(item) for item in raw.get(
                "allowed_config_roots", [os.path.join(nginx_root, "nginx-manager.d")]
            )],
            allowed_certificate_roots=[str(item) for item in raw.get(
                "allowed_certificate_roots", [os.path.join(nginx_root, "ssl", "nginx-manager")]
            )],
            state_dir=str(raw.get("state_dir", "/var/lib/nginx-manager-agent")),
            helper_state_dir=str(raw.get("helper_state_dir", "/var/lib/nginx-manager-agent-helper")),
            helper_socket=str(raw.get("helper_socket", "/run/nginx-manager-agent/helper.sock")),
            helper_timeout=float(raw.get("helper_timeout", 120)),
            helper_max_request_bytes=int(raw.get("helper_max_request_bytes", 8 * 1024 * 1024)),
            max_file_bytes=int(raw.get("max_file_bytes", 4 * 1024 * 1024)),
            max_command_output_bytes=int(raw.get("max_command_output_bytes", 32 * 1024)),
            backup_retention=int(raw.get("backup_retention", 20)),
            health_check=health,
            allowed_health_hosts=[str(item).lower() for item in raw.get("allowed_health_hosts", ["127.0.0.1", "::1", "localhost"])],
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        parsed = urllib.parse.urlparse(self.server_url)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise AgentError("server_url must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise AgentError("server_url must not contain credentials, a base path, query, or fragment")
        if parsed.scheme == "http" and not self.allow_insecure_http:
            raise AgentError("plain HTTP is disabled; use HTTPS or explicitly set allow_insecure_http")
        if self.tls_skip_verify and parsed.scheme != "https":
            raise AgentError("tls_skip_verify may only be used with HTTPS")
        if self.tls_skip_verify and self.ca_file:
            raise AgentError("tls_skip_verify and ca_file cannot be used together")
        if not self.node_name:
            raise AgentError("node_name is required")
        if not os.path.isabs(self.nginx_binary) or not os.path.isabs(self.openssl_binary):
            raise AgentError("nginx_binary and openssl_binary must be absolute paths")
        if not os.path.isabs(self.nginx_config) or not os.path.isabs(self.nginx_root):
            raise AgentError("nginx_config and nginx_root must be absolute paths")
        for name, roots in (
            ("allowed_config_roots", self.allowed_config_roots),
            ("allowed_certificate_roots", self.allowed_certificate_roots),
        ):
            if not roots or any(not os.path.isabs(path) for path in roots):
                raise AgentError("{} must contain absolute paths".format(name))
            nginx_root = Path(self.nginx_root).resolve()
            resolved_roots = [Path(path).resolve() for path in roots]
            if any(root == nginx_root or not _is_relative_to(root, nginx_root) for root in resolved_roots):
                raise AgentError("{} must be strict subdirectories of nginx_root".format(name))
        config_roots = [Path(path).resolve() for path in self.allowed_config_roots]
        certificate_roots = [Path(path).resolve() for path in self.allowed_certificate_roots]
        if any(left == right or _is_relative_to(left, right) or _is_relative_to(right, left)
               for left in config_roots for right in certificate_roots):
            raise AgentError("configuration and certificate roots must not overlap")
        main_config = Path(self.nginx_config).resolve()
        if any(_is_relative_to(main_config, root) for root in config_roots):
            raise AgentError("nginx_config must remain outside allowed_config_roots")
        if not os.path.isabs(self.state_dir) or not os.path.isabs(self.helper_state_dir) or not os.path.isabs(self.helper_socket):
            raise AgentError("state_dir, helper_state_dir, and helper_socket must be absolute paths")
        if min(self.poll_interval, self.heartbeat_interval, self.api_timeout, self.command_timeout) <= 0:
            raise AgentError("intervals and timeouts must be positive")
        if self.max_file_bytes < 1024 or self.helper_max_request_bytes < 1024:
            raise AgentError("byte limits are unreasonably small")
        if not self.allowed_health_hosts or any(not item for item in self.allowed_health_hosts):
            raise AgentError("allowed_health_hosts must contain explicit host names or addresses")


def _optional_string(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)


def _string_dict(value: Any, name: str) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise AgentError("{} must be an object".format(name))
    return {str(key): str(item) for key, item in value.items()}


def _strip_nginx_comments(text: str) -> str:
    """Remove comments without treating a # inside a quoted string as a comment."""
    output: List[str] = []
    quote: Optional[str] = None
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            output.append(char)
            escaped = False
        elif char == "\\" and quote:
            output.append(char)
            escaped = True
        elif quote:
            output.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            output.append(char)
            quote = char
        elif char == "#":
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            if index < len(text):
                output.append(text[index])
        else:
            output.append(char)
        index += 1
    if quote:
        raise ActionError("managed configuration contains an unterminated quoted string")
    return "".join(output)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _new_enrollment_secret() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def _derive_machine_credential(enrollment_secret: str, enrollment_id: str, agent_id: str) -> str:
    message = "nginx-manager-agent-v2\0{}\0{}".format(enrollment_id, agent_id).encode("utf-8")
    return hmac.new(enrollment_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _parse_iso8601(value: Any) -> dt.datetime:
    """Parse the protocol timestamp subset on Python 3.6 and newer."""
    matched = re.match(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?$",
        str(value),
    )
    if matched is None:
        raise ValueError("invalid ISO-8601 timestamp")
    parsed = dt.datetime.strptime(matched.group(1), "%Y-%m-%dT%H:%M:%S")
    fraction = matched.group(2)
    if fraction:
        parsed = parsed.replace(microsecond=int((fraction + "000000")[:6]))
    zone = matched.group(3)
    if zone == "Z":
        return parsed.replace(tzinfo=dt.timezone.utc)
    if zone:
        compact = zone.replace(":", "")
        hours = int(compact[1:3])
        minutes = int(compact[3:5])
        if hours > 23 or minutes > 59:
            raise ValueError("invalid ISO-8601 UTC offset")
        offset = dt.timedelta(hours=hours, minutes=minutes)
        if compact[0] == "-":
            offset = -offset
        return parsed.replace(tzinfo=dt.timezone(offset))
    return parsed


def _atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:  # pragma: no cover - Windows development only
            os.chmod(temporary, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
        _fsync_dir(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(128 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, file_pointer: Any, code: int, message: str,
                         headers: Any, new_url: str) -> None:
        return None


class ApiClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        parsed = urllib.parse.urlparse(settings.server_url)
        self.scheme = parsed.scheme
        self.hostname = parsed.hostname or ""
        self.port = parsed.port or (443 if self.scheme == "https" else 80)
        self.ssl_context = None
        if self.scheme == "https":
            if settings.tls_skip_verify:
                self.ssl_context = ssl._create_unverified_context()
            else:
                self.ssl_context = ssl.create_default_context(cafile=settings.ca_file)

    def post(self, path: str, payload: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": "nginx-manager-agent/" + VERSION,
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        connection: Optional[http.client.HTTPConnection] = None
        try:
            if self.scheme == "https":
                connection = http.client.HTTPSConnection(
                    self.hostname,
                    self.port,
                    timeout=self.settings.api_timeout,
                    context=self.ssl_context,
                )
            else:
                connection = http.client.HTTPConnection(
                    self.hostname,
                    self.port,
                    timeout=self.settings.api_timeout,
                )
            connection.request("POST", path, body=data, headers=headers)
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                # Never include the response body: a broken server may echo a token or key.
                raise ApiError("server returned HTTP {} for {}".format(response.status, path), response.status)
            body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise ApiError("server response is too large")
            if not body:
                return {}
            decoded = json.loads(body.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ApiError("server response must be a JSON object")
            return decoded
        except ApiError:
            raise
        except (OSError, http.client.HTTPException, TimeoutError, ssl.SSLError, ValueError) as exc:
            raise ApiError("request to {} failed: {}".format(path, exc))
        finally:
            if connection is not None:
                connection.close()


class JobStore:
    """Crash-safe job-id records used to prevent replay of privileged work."""

    def __init__(self, path: Path, limit: int = 1000):
        self.path = path
        self.limit = limit
        self.records: Dict[str, Dict[str, Any]] = {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                self.records = raw
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            LOG.warning("job record is unreadable; moving it aside")
            with contextlib.suppress(OSError):
                os.replace(str(path), str(path) + ".corrupt-" + str(int(time.time())))

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.records.get(job_id)

    def begin(self, job_id: str, action: str) -> None:
        self.records[job_id] = {"action": action, "state": "running", "started_at": utc_now()}
        self._save()

    def complete(self, job_id: str, action: str, response: Dict[str, Any]) -> None:
        self.records[job_id] = {
            "action": action,
            "state": "complete",
            "finished_at": utc_now(),
            "response": response,
        }
        if len(self.records) > self.limit:
            ordered = sorted(self.records.items(), key=lambda pair: pair[1].get("finished_at", pair[1].get("started_at", "")))
            for old_id, _record in ordered[: len(self.records) - self.limit]:
                self.records.pop(old_id, None)
        self._save()

    def _save(self) -> None:
        _atomic_json(self.path, self.records)


class JobExecutor:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store
        self._allowed_config_roots = [Path(path).resolve() for path in settings.allowed_config_roots]
        self._allowed_certificate_roots = [Path(path).resolve() for path in settings.allowed_certificate_roots]
        self._lock_path = Path(settings.helper_state_dir) / "apply.lock"
        # Recovery material is privileged state and must never live in the
        # unprivileged network Agent's writable state directory.
        self._transaction_dir = Path(settings.helper_state_dir) / "transactions"

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(job.get("id", "")).strip()
        action = str(job.get("action", "")).strip()
        payload = job.get("payload", {})
        if not job_id or len(job_id) > 200:
            return self._response(job_id or "unknown", action, "failed", error="invalid job id")
        if action not in CAPABILITIES:
            return self._response(job_id, action, "failed", error="action is not allowed")
        if not isinstance(payload, dict):
            return self._response(job_id, action, "failed", error="payload must be an object")

        existing = self.store.get(job_id)
        if existing:
            if existing.get("action") != action:
                return self._response(job_id, action, "failed", error="job id was already used for a different action")
            if existing.get("state") == "complete" and isinstance(existing.get("response"), dict):
                return existing["response"]
            interrupted = self._response(
                job_id,
                action,
                "failed",
                error="previous execution was interrupted; action was not replayed; inspect the node and issue a new job",
            )
            self.store.complete(job_id, action, interrupted)
            return interrupted

        if _is_expired(job.get("expires_at")):
            response = self._response(job_id, action, "expired", error="job expired before execution")
            self.store.complete(job_id, action, response)
            return response

        self.store.begin(job_id, action)
        started = utc_now()
        try:
            with self._global_lock():
                # A previous helper may have lost power after an atomic replace.
                # Never execute more work until its on-disk transaction is resolved.
                self._recover_incomplete_transactions_locked()
                handler = getattr(self, "_action_" + action)
                result = handler(payload, job_id)
            response = self._response(job_id, action, "succeeded", result=result, started_at=started)
        except AgentError as exc:
            response = self._response(job_id, action, "failed", error=str(exc), started_at=started)
        except Exception:
            LOG.exception("unexpected failure in job %s action %s", job_id, action)
            response = self._response(job_id, action, "failed", error="unexpected internal agent error", started_at=started)
        self.store.complete(job_id, action, response)
        return response

    @contextlib.contextmanager
    def _global_lock(self) -> Iterable[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _THREAD_LOCK:
            with self._lock_path.open("a+b") as handle:
                with contextlib.suppress(OSError):
                    os.chmod(str(self._lock_path), 0o600)
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def recover_incomplete_transactions(self) -> int:
        """Restore all durable, non-committed publication transactions.

        This is called before the privileged helper creates its listening socket.
        Any ambiguous/tampered state raises and therefore prevents helper startup.
        """
        with self._global_lock():
            return self._recover_incomplete_transactions_locked()

    @staticmethod
    def _response(job_id: str, action: str, status: str, result: Optional[Dict[str, Any]] = None,
                  error: Optional[str] = None, started_at: Optional[str] = None) -> Dict[str, Any]:
        response: Dict[str, Any] = {
            "job_id": job_id,
            "action": action,
            "status": status,
            "started_at": started_at or utc_now(),
            "finished_at": utc_now(),
        }
        if result is not None:
            response["result"] = result
        if error:
            response["error"] = error
        return response

    def _allowed_path(self, raw: Any, roots: List[Path], kind: str) -> Path:
        if not isinstance(raw, str) or not raw or not os.path.isabs(raw):
            raise ActionError("path must be an absolute string")
        candidate = Path(raw)
        if candidate.name in ("", ".", ".."):
            raise ActionError("path must name a file")
        if kind == "configuration" and candidate.suffix.lower() != ".conf":
            raise ActionError("managed configuration paths must end in .conf")
        if kind == "certificate" and candidate.suffix.lower() not in (".pem", ".crt", ".key"):
            raise ActionError("managed certificate paths must end in .pem, .crt, or .key")
        try:
            resolved_parent = candidate.parent.resolve(strict=True)
        except OSError as exc:
            raise ActionError("path parent does not exist: {}".format(exc))
        resolved = resolved_parent / candidate.name
        if candidate.is_symlink():
            raise ActionError("symbolic-link targets are refused")
        if candidate.exists():
            status = candidate.stat()
            if not stat.S_ISREG(status.st_mode):
                raise ActionError("managed paths must name regular files")
            if status.st_nlink > 1:
                raise ActionError("hard-linked targets are refused")
        if not any(_is_relative_to(resolved, root) for root in roots):
            raise ActionError("path is outside allowed_{}_roots".format(kind))
        return resolved

    def _configuration_path(self, raw: Any) -> Path:
        return self._allowed_path(raw, self._allowed_config_roots, "configuration")

    def _certificate_path(self, raw: Any) -> Path:
        return self._allowed_path(raw, self._allowed_certificate_roots, "certificate")

    def _validate_managed_config(self, data: bytes) -> None:
        """Reject directives that can escape the managed http-context boundary."""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ActionError("managed configuration must be valid UTF-8")
        tokens = NGINX_TOKEN_RE.findall(_strip_nginx_comments(text))
        if not tokens:
            raise ActionError("managed configuration cannot be empty")

        allowed_top_blocks = {"server", "upstream"}
        allowed_top_directives = {
            "limit_req_zone", "limit_conn_zone", "log_format",
            "server_names_hash_bucket_size", "variables_hash_bucket_size", "variables_hash_max_size",
        }
        # First-release policy: known directives from the standard HTTP modules only.
        # Unknown third-party directives are rejected because nginx -t runs in the
        # privileged helper and module configuration callbacks are executable code.
        allowed_directives = allowed_top_blocks | allowed_top_directives | {
            "location", "if", "limit_except",
            "listen", "server_name", "return", "rewrite", "set", "break",
            "root", "alias", "index", "try_files", "internal", "autoindex",
            "autoindex_exact_size", "autoindex_localtime", "error_page", "recursive_error_pages",
            "client_max_body_size", "client_body_buffer_size", "client_body_timeout",
            "large_client_header_buffers", "keepalive_timeout", "keepalive_requests", "send_timeout",
            "sendfile", "tcp_nopush", "tcp_nodelay", "default_type", "charset", "override_charset",
            "add_header", "expires", "allow", "deny", "satisfy", "access_log",
            "proxy_pass", "proxy_set_header", "proxy_http_version", "proxy_connect_timeout",
            "proxy_read_timeout", "proxy_send_timeout", "proxy_buffering", "proxy_buffers",
            "proxy_buffer_size", "proxy_busy_buffers_size", "proxy_request_buffering", "proxy_redirect",
            "proxy_intercept_errors", "proxy_next_upstream", "proxy_next_upstream_tries",
            "proxy_hide_header", "proxy_pass_header", "proxy_ignore_headers", "proxy_cookie_domain",
            "proxy_cookie_path", "proxy_cookie_flags", "proxy_pass_request_headers",
            "proxy_pass_request_body", "proxy_set_body", "proxy_method", "proxy_socket_keepalive",
            "proxy_cache", "proxy_cache_key", "proxy_cache_bypass", "proxy_no_cache", "proxy_cache_valid",
            "ssl_certificate", "ssl_certificate_key", "ssl_trusted_certificate", "ssl_client_certificate",
            "ssl_crl", "ssl_dhparam", "ssl_session_ticket_key", "ssl_protocols", "ssl_ciphers",
            "ssl_prefer_server_ciphers", "ssl_session_cache", "ssl_session_timeout", "ssl_session_tickets",
            "ssl_stapling", "ssl_stapling_verify", "ssl_verify_client", "ssl_verify_depth",
            "ssl_ocsp", "ssl_ocsp_cache", "proxy_ssl_certificate", "proxy_ssl_certificate_key",
            "proxy_ssl_trusted_certificate", "proxy_ssl_crl", "proxy_ssl_verify",
            "proxy_ssl_verify_depth", "proxy_ssl_server_name", "proxy_ssl_name", "proxy_ssl_protocols",
            "proxy_ssl_ciphers", "resolver", "resolver_timeout", "gzip", "gzip_types", "gzip_min_length",
            "gzip_comp_level", "gzip_vary", "gzip_proxied", "gzip_disable", "limit_req", "limit_conn",
            "limit_rate", "limit_rate_after", "auth_request", "auth_request_set", "stub_status",
            "real_ip_header", "set_real_ip_from", "real_ip_recursive", "mirror", "mirror_request_body",
            "fastcgi_pass", "fastcgi_param", "fastcgi_index", "fastcgi_split_path_info",
            "fastcgi_connect_timeout", "fastcgi_read_timeout", "fastcgi_send_timeout", "fastcgi_buffering",
            "fastcgi_buffers", "fastcgi_buffer_size", "uwsgi_pass", "uwsgi_param", "scgi_pass", "scgi_param",
            "grpc_pass", "grpc_set_header", "grpc_connect_timeout", "grpc_read_timeout", "grpc_send_timeout",
            "least_conn", "ip_hash", "hash", "random", "keepalive", "zone", "max_conns", "max_fails",
            "fail_timeout", "backup", "down", "weight", "route", "slow_start", "queue",
            "etag", "aio", "directio", "output_buffers", "open_file_cache", "open_file_cache_errors",
            "open_file_cache_min_uses", "open_file_cache_valid", "sub_filter", "sub_filter_once",
            "sub_filter_types",
        }
        forbidden = {
            "include", "load_module", "user", "pid", "daemon", "master_process", "env",
            "working_directory", "error_log", "lock_file", "ssl_engine", "ssl_password_file",
            "client_body_temp_path", "proxy_temp_path", "fastcgi_temp_path", "uwsgi_temp_path",
            "scgi_temp_path",
        }
        restricted_certificate_paths = {
            "ssl_certificate", "ssl_certificate_key", "ssl_trusted_certificate", "ssl_client_certificate",
            "ssl_crl", "ssl_dhparam", "ssl_session_ticket_key", "proxy_ssl_certificate",
            "proxy_ssl_certificate_key", "proxy_ssl_trusted_certificate", "proxy_ssl_crl",
        }
        depth = 0
        statement: List[str] = []

        def unquote(value: str) -> str:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                return value[1:-1]
            return value

        def validate_statement(is_block: bool, statement_depth: int) -> None:
            if not statement:
                raise ActionError("managed configuration contains an empty statement")
            directive = statement[0].lower()
            if not DIRECTIVE_RE.fullmatch(directive):
                raise ActionError("invalid directive name in managed configuration")
            if directive not in allowed_directives:
                raise ActionError("directive '{}' is not in the managed configuration allowlist".format(directive))
            if directive in forbidden or "lua" in directive or "exec" in directive or directive.startswith(
                ("perl", "js_", "njs", "wasm")
            ):
                raise ActionError("directive '{}' is not allowed in managed configuration".format(directive))
            if statement_depth == 0:
                allowed = allowed_top_blocks if is_block else allowed_top_directives
                if directive not in allowed:
                    raise ActionError("top-level directive '{}' is not allowed in managed configuration".format(directive))
            if directive == "access_log":
                args = [unquote(item).lower() for item in statement[1:]]
                if args != ["off"]:
                    raise ActionError("access_log is allowed only with the value 'off'")
            if directive in restricted_certificate_paths:
                if is_block or len(statement) != 2:
                    raise ActionError("directive '{}' must contain one managed certificate path".format(directive))
                certificate_path = unquote(statement[1])
                if "$" in certificate_path:
                    raise ActionError("variables are not allowed in managed certificate paths")
                self._certificate_path(certificate_path)

        for token in tokens:
            if token == "{":
                validate_statement(True, depth)
                statement = []
                depth += 1
            elif token == ";":
                validate_statement(False, depth)
                statement = []
            elif token == "}":
                if statement:
                    raise ActionError("managed configuration has an unterminated directive")
                depth -= 1
                if depth < 0:
                    raise ActionError("managed configuration has an unexpected closing brace")
            else:
                statement.append(token)
        if statement or depth != 0:
            raise ActionError("managed configuration has unbalanced braces or a missing semicolon")

    def _decode_content(self, payload: Dict[str, Any], text_key: str = "content", b64_key: str = "content_base64") -> bytes:
        text_present = text_key in payload
        b64_present = b64_key in payload
        if text_present == b64_present:
            raise ActionError("provide exactly one of {} or {}".format(text_key, b64_key))
        if text_present:
            value = payload[text_key]
            if not isinstance(value, str):
                raise ActionError("{} must be a string".format(text_key))
            data = value.encode("utf-8")
        else:
            value = payload[b64_key]
            if not isinstance(value, str):
                raise ActionError("{} must be a base64 string".format(b64_key))
            try:
                data = base64.b64decode(value, validate=True)
            except (ValueError, base64.binascii.Error):
                raise ActionError("{} is invalid base64".format(b64_key))
        if len(data) > self.settings.max_file_bytes:
            raise ActionError("content exceeds max_file_bytes")
        return data

    def _check_expected(self, path: Path, expected: Any) -> Optional[str]:
        if not isinstance(expected, str):
            raise ActionError("expected_sha256 is required")
        expected = expected.lower()
        actual = _file_sha256(path) if path.exists() else None
        if expected == "missing":
            if actual is not None:
                raise ActionError("concurrent change detected: expected a missing file")
        elif expected == "present":
            if actual is None:
                raise ActionError("replacement refused: expected an existing file")
        elif not SHA256_RE.match(expected):
            raise ActionError("expected_sha256 must be a SHA-256 hex value, 'missing', or 'present'")
        elif actual != expected:
            raise ActionError("concurrent change detected: current SHA-256 does not match expected_sha256")
        return actual

    def _run(self, argv: List[str]) -> Dict[str, Any]:
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.settings.command_timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise CommandError("command timed out after {:.1f}s".format(self.settings.command_timeout))
        except OSError as exc:
            raise CommandError("cannot execute configured nginx binary: {}".format(exc))
        stdout = completed.stdout.decode("utf-8", errors="replace")[-self.settings.max_command_output_bytes :]
        stderr = completed.stderr.decode("utf-8", errors="replace")[-self.settings.max_command_output_bytes :]
        result = {"exit_code": completed.returncode, "stdout": stdout, "stderr": stderr}
        if completed.returncode != 0:
            detail = (stderr or stdout or "no output").strip()
            raise CommandError("nginx command failed (exit {}): {}".format(completed.returncode, detail))
        return result

    def _nginx_test(self) -> Dict[str, Any]:
        return self._run([self.settings.nginx_binary, "-t", "-c", self.settings.nginx_config])

    def _reload_only(self) -> Dict[str, Any]:
        return self._run([self.settings.nginx_binary, "-s", "reload", "-c", self.settings.nginx_config])

    def _nginx_is_running(self) -> bool:
        """Best-effort Linux check used only to decide recovery reload behavior."""
        for pid_path in (Path("/run/nginx.pid"), Path("/var/run/nginx.pid"), Path("/run/nginx/nginx.pid")):
            try:
                raw_pid = pid_path.read_text(encoding="ascii").strip()
                if not raw_pid.isdigit() or int(raw_pid) <= 1:
                    continue
                try:
                    os.kill(int(raw_pid), 0)
                    return True
                except PermissionError:
                    return True
                except ProcessLookupError:
                    continue
            except (OSError, UnicodeError, ValueError):
                continue
        # Some distributions configure a non-standard pid path. Nginx sets the
        # master process title on Linux, so /proc provides a safe fixed fallback.
        proc = Path("/proc")
        if proc.is_dir():
            try:
                processes = proc.iterdir()
            except OSError:
                return False
            for process in processes:
                if not process.name.isdigit():
                    continue
                try:
                    command_line = (process / "cmdline").read_bytes()[:4096]
                    if b"nginx: master process" in command_line:
                        return True
                except OSError:
                    continue
        return False

    def _action_inspect(self, _payload: Dict[str, Any], _job_id: str) -> Dict[str, Any]:
        nginx: Dict[str, Any]
        try:
            nginx = self._run([self.settings.nginx_binary, "-v"])
        except AgentError as exc:
            nginx = {"error": str(exc)}
        configured_path = Path(self.settings.nginx_config)
        if configured_path.is_symlink():
            raise ActionError("configured nginx main file must not be a symbolic link")
        config_path = configured_path.resolve()
        config_hash = _file_sha256(config_path) if config_path.is_file() else None
        return {
            "agent_version": VERSION,
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "nginx": nginx,
            "nginx_config": str(config_path),
            "config_sha256": config_hash,
            "capabilities": list(CAPABILITIES),
        }

    def _action_nginx_test(self, _payload: Dict[str, Any], _job_id: str) -> Dict[str, Any]:
        return self._nginx_test()

    def _action_nginx_reload(self, _payload: Dict[str, Any], _job_id: str) -> Dict[str, Any]:
        tested = self._nginx_test()
        reloaded = self._reload_only()
        return {"test": tested, "reload": reloaded}

    def _action_config_inventory(self, _payload: Dict[str, Any], _job_id: str) -> Dict[str, Any]:
        files: List[Dict[str, Any]] = []
        skipped = 0
        total_bytes = 0
        truncated = False
        candidates: List[Path] = []
        seen = set()
        for root in self._allowed_config_roots:
            if not root.is_dir() or root.is_symlink():
                continue
            for current_root, directory_names, file_names in os.walk(str(root), followlinks=False):
                directory_names[:] = sorted(
                    name for name in directory_names
                    if not (Path(current_root) / name).is_symlink()
                )
                for name in sorted(file_names):
                    if not name.lower().endswith(".conf"):
                        continue
                    candidate = Path(current_root) / name
                    try:
                        resolved = self._configuration_path(str(candidate))
                    except ActionError:
                        skipped += 1
                        continue
                    key = str(resolved)
                    if key not in seen:
                        candidates.append(resolved)
                        seen.add(key)

        for path in sorted(candidates, key=lambda item: str(item)):
            if len(files) >= INVENTORY_MAX_FILES:
                truncated = True
                break
            try:
                size = path.stat().st_size
                if size > min(self.settings.max_file_bytes, INVENTORY_MAX_FILE_BYTES):
                    skipped += 1
                    continue
                data = path.read_bytes()
            except OSError:
                skipped += 1
                continue
            if len(data) != size or b"PRIVATE KEY-----" in data:
                skipped += 1
                continue
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                skipped += 1
                continue
            if total_bytes + len(data) > INVENTORY_MAX_TOTAL_BYTES:
                truncated = True
                break
            files.append({
                "path": str(path),
                "content": content,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            })
            total_bytes += len(data)
        return {
            "files": files,
            "file_count": len(files),
            "total_bytes": total_bytes,
            "skipped_count": skipped,
            "truncated": truncated,
        }

    def _action_config_read(self, payload: Dict[str, Any], _job_id: str) -> Dict[str, Any]:
        path = self._configuration_path(payload.get("path"))
        if not path.is_file():
            raise ActionError("configuration file does not exist")
        size = path.stat().st_size
        if size > self.settings.max_file_bytes:
            raise ActionError("file exceeds max_file_bytes")
        data = path.read_bytes()
        if b"PRIVATE KEY-----" in data:
            raise ActionError("config_read refuses private key material")
        return {"path": str(path), "content": data.decode("utf-8", errors="replace"), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def _action_config_hash(self, payload: Dict[str, Any], _job_id: str) -> Dict[str, Any]:
        path = self._configuration_path(payload.get("path"))
        if not path.is_file():
            raise ActionError("configuration file does not exist")
        return {"path": str(path), "sha256": _file_sha256(path), "size": path.stat().st_size}

    def _action_config_apply(self, payload: Dict[str, Any], job_id: str) -> Dict[str, Any]:
        path = self._configuration_path(payload.get("path"))
        data = self._decode_content(payload)
        self._validate_managed_config(data)
        actual = self._check_expected(path, payload.get("expected_sha256"))
        new_sha = hashlib.sha256(data).hexdigest()
        requested_new_sha = payload.get("new_sha256")
        if requested_new_sha is not None and str(requested_new_sha).lower() != new_sha:
            raise ActionError("candidate content does not match new_sha256")
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        validate_only = bool(payload.get("validate_only", False))
        transaction = self._apply_files(
            [{"path": path, "data": data, "mode": mode}],
            job_id,
            reload_enabled=bool(payload.get("reload", False)) and not validate_only,
            health=_health_from_payload(payload, self.settings.health_check) if not validate_only else None,
            validate_only=validate_only,
        )
        return {
            "path": str(path),
            "previous_sha256": actual,
            "sha256": new_sha,
            "validated": True,
            "applied": not validate_only,
            "reloaded": bool(payload.get("reload", False)) and not validate_only,
            **transaction,
        }

    def _action_config_delete(self, payload: Dict[str, Any], job_id: str) -> Dict[str, Any]:
        path = self._configuration_path(payload.get("path"))
        expected = payload.get("expected_sha256")
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected.lower()):
            raise ActionError("config_delete requires the exact current SHA-256")
        actual = self._check_expected(path, expected)
        if actual is None:
            raise ActionError("configuration file does not exist")
        transaction = self._apply_files(
            [{"path": path, "delete": True, "mode": stat.S_IMODE(path.stat().st_mode)}],
            job_id,
            reload_enabled=True,
            health=_health_from_payload(payload, self.settings.health_check),
            validate_only=False,
        )
        return {
            "path": str(path),
            "previous_sha256": actual,
            "deleted": True,
            "validated": True,
            "reloaded": True,
            **transaction,
        }

    def _action_certificate_apply(self, payload: Dict[str, Any], job_id: str) -> Dict[str, Any]:
        cert_spec = payload.get("certificate") if isinstance(payload.get("certificate"), dict) else payload
        key_spec = payload.get("private_key") if isinstance(payload.get("private_key"), dict) else payload
        cert_path = self._certificate_path(cert_spec.get("path", payload.get("cert_path")))
        key_path = self._certificate_path(key_spec.get("path", payload.get("key_path")))
        if cert_path == key_path:
            raise ActionError("certificate and private key paths must differ")
        cert_data = self._certificate_content(cert_spec, "pem", "pem_base64", "certificate_pem", "certificate_base64")
        key_data = self._certificate_content(key_spec, "pem", "pem_base64", "private_key_pem", "private_key_base64")
        if b"-----BEGIN CERTIFICATE-----" not in cert_data:
            raise ActionError("certificate_pem does not contain a PEM certificate")
        if b"PRIVATE KEY-----" not in key_data:
            raise ActionError("private_key_pem does not contain a PEM private key")
        certificate_fingerprint = self._verify_certificate_pair(cert_data, key_data)
        expected_cert = cert_spec.get("expected_sha256", payload.get("expected_cert_sha256"))
        expected_key = key_spec.get("expected_sha256", payload.get("expected_key_sha256"))
        previous_cert = self._check_expected(cert_path, expected_cert)
        previous_key = self._check_expected(key_path, expected_key)
        validate_only = bool(payload.get("validate_only", False))
        transaction = self._apply_files(
            [
                {"path": cert_path, "data": cert_data, "mode": 0o644},
                {"path": key_path, "data": key_data, "mode": 0o600},
            ],
            job_id,
            reload_enabled=bool(payload.get("reload", False)) and not validate_only,
            health=_health_from_payload(payload, self.settings.health_check) if not validate_only else None,
            validate_only=validate_only,
        )
        # Never return PEM material.  Only content digests and public paths leave the helper.
        return {
            "certificate_path": str(cert_path),
            "private_key_path": str(key_path),
            "previous_certificate_sha256": previous_cert,
            "previous_private_key_sha256": previous_key,
            "certificate_sha256": hashlib.sha256(cert_data).hexdigest(),
            "private_key_sha256": hashlib.sha256(key_data).hexdigest(),
            "certificate_fingerprint": certificate_fingerprint,
            "validated": True,
            "applied": not validate_only,
            "reloaded": bool(payload.get("reload", False)) and not validate_only,
            **transaction,
        }

    def _verify_certificate_pair(self, certificate: bytes, private_key: bytes) -> str:
        state_dir = Path(self.settings.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        cert_fd, cert_name = tempfile.mkstemp(prefix="certificate-", suffix=".pem", dir=str(state_dir))
        key_fd, key_name = tempfile.mkstemp(prefix="private-key-", suffix=".pem", dir=str(state_dir))
        try:
            os.fchmod(cert_fd, 0o600)
            os.fchmod(key_fd, 0o600)
            with os.fdopen(cert_fd, "wb") as cert_handle:
                cert_handle.write(certificate)
                cert_handle.flush()
                os.fsync(cert_handle.fileno())
            with os.fdopen(key_fd, "wb") as key_handle:
                key_handle.write(private_key)
                key_handle.flush()
                os.fsync(key_handle.fileno())
            cert_public = self._openssl(["x509", "-in", cert_name, "-pubkey", "-noout"])
            key_public = self._openssl(["pkey", "-in", key_name, "-pubout"])
            if not hashlib.sha256(cert_public).digest() == hashlib.sha256(key_public).digest():
                raise ActionError("certificate does not match private key")
            self._openssl(["x509", "-in", cert_name, "-checkend", "0", "-noout"])
            fingerprint = self._openssl(["x509", "-in", cert_name, "-fingerprint", "-sha256", "-noout"])
            text = fingerprint.decode("utf-8", errors="replace").strip()
            return text.split("=", 1)[-1][:256]
        finally:
            with contextlib.suppress(OSError):
                os.unlink(cert_name)
            with contextlib.suppress(OSError):
                os.unlink(key_name)

    def _openssl(self, arguments: List[str]) -> bytes:
        try:
            completed = subprocess.run(
                [self.settings.openssl_binary] + arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.settings.command_timeout,
                check=False,
                shell=False,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ActionError("certificate validation command failed: {}".format(exc))
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[-1024:].strip()
            raise ActionError("certificate validation failed: {}".format(detail or "openssl returned an error"))
        return completed.stdout

    def _certificate_content(self, spec: Dict[str, Any], nested_text: str, nested_b64: str,
                             flat_text: str, flat_b64: str) -> bytes:
        if nested_text in spec or nested_b64 in spec:
            return self._decode_content(spec, nested_text, nested_b64)
        return self._decode_content(spec, flat_text, flat_b64)

    def _apply_files(self, items: List[Dict[str, Any]], job_id: str, reload_enabled: bool,
                     health: Optional[Dict[str, Any]], validate_only: bool) -> Dict[str, Any]:
        return self._apply_files_transactional(items, job_id, reload_enabled, health, validate_only)

    def _apply_files_transactional(self, items: List[Dict[str, Any]], job_id: str,
                                   reload_enabled: bool, health: Optional[Dict[str, Any]],
                                   validate_only: bool) -> Dict[str, Any]:
        transaction_id = hashlib.sha256(job_id.encode("utf-8") + os.urandom(32)).hexdigest()
        manifest_path = self._transaction_manifest_path(transaction_id)
        manifest: Optional[Dict[str, Any]] = None
        manifest_durable = False
        artifacts: List[Path] = []
        test_result: Optional[Dict[str, Any]] = None
        reload_result: Optional[Dict[str, Any]] = None
        health_result: Optional[Dict[str, Any]] = None
        committed = False
        try:
            if not items or len({str(item["path"]) for item in items}) != len(items):
                raise ActionError("transaction targets must be non-empty and unique")

            entries: List[Dict[str, Any]] = []
            touched_directories = set()
            for item in items:
                path: Path = item["path"]
                kind, checked_path = self._transaction_target_kind(path)
                if checked_path != path:
                    raise ActionError("transaction target changed after path validation")
                original_exists = path.exists()
                old_sha = _file_sha256(path) if original_exists else None
                operation = "delete" if bool(item.get("delete", False)) else "write"
                if operation == "delete" and not original_exists:
                    raise ActionError("cannot delete a missing transaction target")
                new_sha = None if operation == "delete" else hashlib.sha256(item["data"]).hexdigest()

                candidate: Optional[Path] = None
                if operation == "write":
                    fd, tmp_name = tempfile.mkstemp(
                        prefix="." + path.name + ".nginx-manager-candidate-" + transaction_id + "-",
                        dir=str(path.parent),
                    )
                    candidate = Path(tmp_name)
                    artifacts.append(candidate)
                    try:
                        if hasattr(os, "fchmod"):
                            os.fchmod(fd, int(item["mode"]))
                        else:  # pragma: no cover - Windows development only
                            os.chmod(tmp_name, int(item["mode"]))
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(item["data"])
                            handle.flush()
                            os.fsync(handle.fileno())
                        if original_exists and hasattr(os, "chown"):
                            current = path.stat()
                            os.chown(str(candidate), current.st_uid, current.st_gid)
                    except Exception:
                        with contextlib.suppress(OSError):
                            os.close(fd)
                        raise

                backup: Optional[Path] = None
                if original_exists:
                    backup_fd, backup_name = tempfile.mkstemp(
                        prefix="." + path.name + ".nginx-manager-backup-" + transaction_id + "-",
                        dir=str(path.parent),
                    )
                    os.close(backup_fd)
                    backup = Path(backup_name)
                    artifacts.append(backup)
                    shutil.copy2(str(path), str(backup))
                    with backup.open("r+b") as handle:
                        os.fsync(handle.fileno())
                    if _file_sha256(backup) != old_sha:
                        raise ActionError("durable backup hash does not match original file")

                entries.append({
                    "kind": kind,
                    "operation": operation,
                    "target": str(path),
                    "candidate": str(candidate) if candidate is not None else None,
                    "backup": str(backup) if backup is not None else None,
                    "original_exists": original_exists,
                    "old_sha256": old_sha,
                    "new_sha256": new_sha,
                    "mode": int(item["mode"]),
                    "replaced": False,
                })
                touched_directories.add(path.parent)

            # Make artifact directory entries durable before the manifest claims
            # that recovery material exists.
            for directory in touched_directories:
                _fsync_dir(directory)
            manifest = {
                "version": 1,
                "transaction_id": transaction_id,
                "job_id_sha256": hashlib.sha256(job_id.encode("utf-8")).hexdigest(),
                "phase": "prepared",
                "reload_enabled": bool(reload_enabled),
                "validate_only": bool(validate_only),
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "files": entries,
            }
            self._persist_manifest(manifest_path, manifest)
            manifest_durable = True

            self._set_manifest_phase(manifest_path, manifest, "replacing")
            for entry in entries:
                target = Path(entry["target"])
                if entry["operation"] == "delete":
                    if not target.exists() or _file_sha256(target) != entry["old_sha256"]:
                        raise ActionError("transaction target changed before deletion")
                    target.unlink()
                else:
                    os.replace(entry["candidate"], entry["target"])
                _fsync_dir(target.parent)
                entry["replaced"] = True
                # Recovery verifies hashes rather than trusting this marker, so a
                # crash between replace and this write is also covered.
                self._persist_manifest(manifest_path, manifest)
            self._set_manifest_phase(manifest_path, manifest, "replaced")

            self._set_manifest_phase(manifest_path, manifest, "testing")
            test_result = self._nginx_test()
            self._set_manifest_phase(manifest_path, manifest, "validated")
            if validate_only:
                self._set_manifest_phase(manifest_path, manifest, "rolling_back")
                self._recover_manifest(manifest_path, verify_after=False)
                return {"test": test_result, "backup_count": 0, "validate_only": True}

            if reload_enabled:
                self._set_manifest_phase(manifest_path, manifest, "reloading")
                reload_result = self._reload_only()
                self._set_manifest_phase(manifest_path, manifest, "reloaded")
            if health:
                self._set_manifest_phase(manifest_path, manifest, "health_checking")
                health_result = self._health_check(health)
                self._set_manifest_phase(manifest_path, manifest, "health_checked")

            # Persist commit before cleanup. If unlink is interrupted, startup
            # preserves the verified new files and only removes the stale marker.
            self._set_manifest_phase(manifest_path, manifest, "committed")
            committed = True
            try:
                self._remove_manifest(manifest_path)
            except OSError as exc:
                LOG.error("committed transaction manifest %s could not be cleaned: %s", transaction_id, exc)

            for item in items:
                try:
                    self._prune_backups(item["path"])
                except OSError as exc:
                    LOG.warning("could not prune old backups for %s: %s", item["path"], exc)
            result: Dict[str, Any] = {
                "test": test_result,
                "backup_count": sum(entry["backup"] is not None for entry in entries),
                "validate_only": False,
            }
            if reload_result is not None:
                result["reload"] = reload_result
            if health_result is not None:
                result["health"] = health_result
            return result
        except Exception as exc:
            if committed:
                raise ActionError("publication committed but result finalization failed; do not replay this job: {}".format(exc))
            if manifest is None or not manifest_durable:
                for artifact in artifacts:
                    with contextlib.suppress(OSError):
                        artifact.unlink()
                for item in items:
                    with contextlib.suppress(OSError):
                        _fsync_dir(item["path"].parent)
                raise ActionError("publish preparation failed before replacement: {}".format(exc))
            try:
                self._recover_manifest(manifest_path, verify_after=True)
            except Exception as recovery_error:
                LOG.critical(
                    "transaction %s failed and automatic recovery is incomplete: %s",
                    transaction_id,
                    recovery_error,
                )
                raise ActionError(
                    "publish failed and automatic recovery could not be verified; helper must not continue: {}; recovery: {}".format(
                        exc, recovery_error
                    )
                )
            raise ActionError("publish failed and previous files were restored: {}".format(exc))

    def _transaction_target_kind(self, path: Path) -> Tuple[str, Path]:
        if any(_is_relative_to(path, root) for root in self._allowed_config_roots):
            return "configuration", self._configuration_path(str(path))
        if any(_is_relative_to(path, root) for root in self._allowed_certificate_roots):
            return "certificate", self._certificate_path(str(path))
        raise ActionError("transaction target is outside all managed roots")

    def _transaction_manifest_path(self, transaction_id: str) -> Path:
        self._secure_transaction_dir()
        return self._transaction_dir / ("tx-" + transaction_id + ".json")

    def _secure_transaction_dir(self) -> None:
        self._transaction_dir.parent.mkdir(parents=True, exist_ok=True)
        if self._transaction_dir.is_symlink():
            raise ActionError("transaction directory must not be a symbolic link")
        self._transaction_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        status = self._transaction_dir.stat()
        if not stat.S_ISDIR(status.st_mode):
            raise ActionError("transaction path is not a directory")
        if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
            if status.st_uid != 0 or stat.S_IMODE(status.st_mode) & 0o022:
                raise ActionError("transaction directory must be root-owned and not group/world writable")
        with contextlib.suppress(OSError):
            os.chmod(str(self._transaction_dir), 0o700)
        _fsync_dir(self._transaction_dir.parent)

    def _persist_manifest(self, path: Path, manifest: Dict[str, Any]) -> None:
        manifest["updated_at"] = utc_now()
        _atomic_json(path, manifest, 0o600)

    def _set_manifest_phase(self, path: Path, manifest: Dict[str, Any], phase: str) -> None:
        if phase not in TRANSACTION_PHASES:
            raise ActionError("invalid transaction phase")
        manifest["phase"] = phase
        self._persist_manifest(path, manifest)

    def _remove_manifest(self, path: Path) -> None:
        path.unlink()
        _fsync_dir(path.parent)

    def _recover_incomplete_transactions_locked(self) -> int:
        self._secure_transaction_dir()
        recovered = 0
        for manifest_path in sorted(self._transaction_dir.glob("tx-*.json")):
            if manifest_path.is_symlink():
                raise ActionError("transaction manifest must not be a symbolic link: {}".format(manifest_path.name))
            manifest = self._load_manifest(manifest_path)
            if manifest["phase"] == "committed":
                self._cleanup_committed_manifest(manifest_path, manifest)
                continue
            LOG.error(
                "recovering interrupted nginx publication transaction %s from phase %s",
                manifest["transaction_id"],
                manifest["phase"],
            )
            self._recover_manifest(manifest_path, verify_after=True, loaded_manifest=manifest)
            recovered += 1
        return recovered

    def _load_manifest(self, path: Path) -> Dict[str, Any]:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise ActionError("transaction manifest must be a single regular file")
        if status.st_size > 1024 * 1024:
            raise ActionError("transaction manifest exceeds size limit")
        if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
            if status.st_uid != 0 or stat.S_IMODE(status.st_mode) & 0o022:
                raise ActionError("transaction manifest is not protected against modification")
        try:
            with path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ActionError("cannot read transaction manifest {}: {}".format(path.name, exc))
        if not isinstance(manifest, dict) or manifest.get("version") != 1:
            raise ActionError("unsupported transaction manifest")
        transaction_id = manifest.get("transaction_id")
        if not isinstance(transaction_id, str) or not TRANSACTION_ID_RE.fullmatch(transaction_id):
            raise ActionError("transaction manifest has an invalid id")
        if path.name != "tx-" + transaction_id + ".json":
            raise ActionError("transaction manifest filename does not match its id")
        phase = manifest.get("phase")
        if phase not in TRANSACTION_PHASES:
            raise ActionError("transaction manifest has an invalid phase")
        if not isinstance(manifest.get("reload_enabled"), bool) or not isinstance(manifest.get("validate_only"), bool):
            raise ActionError("transaction manifest flags are invalid")
        recovery_from = manifest.get("recovery_from_phase")
        if recovery_from is not None and recovery_from not in TRANSACTION_PHASES:
            raise ActionError("transaction manifest recovery phase is invalid")
        files = manifest.get("files")
        if not isinstance(files, list) or not files or len(files) > 16:
            raise ActionError("transaction manifest file list is invalid")
        seen = set()
        for entry in files:
            self._validate_manifest_entry(entry, transaction_id)
            if entry["target"] in seen:
                raise ActionError("transaction manifest contains duplicate targets")
            seen.add(entry["target"])
        return manifest

    def _validate_manifest_entry(self, entry: Any, transaction_id: str) -> None:
        if not isinstance(entry, dict) or entry.get("kind") not in ("configuration", "certificate"):
            raise ActionError("transaction manifest contains an invalid file entry")
        raw_target = entry.get("target")
        if entry["kind"] == "configuration":
            target = self._configuration_path(raw_target)
        else:
            target = self._certificate_path(raw_target)
        if str(target) != str(raw_target):
            raise ActionError("transaction target is not in canonical form")
        original_exists = entry.get("original_exists")
        old_sha = entry.get("old_sha256")
        new_sha = entry.get("new_sha256")
        operation = entry.get("operation", "write")
        if operation not in ("write", "delete") or not isinstance(original_exists, bool):
            raise ActionError("transaction manifest hashes/state are invalid")
        if original_exists:
            if not isinstance(old_sha, str) or not SHA256_RE.fullmatch(old_sha):
                raise ActionError("transaction original hash is invalid")
            if not isinstance(entry.get("backup"), str):
                raise ActionError("transaction for an existing target is missing its backup path")
        elif old_sha is not None or entry.get("backup") is not None:
            raise ActionError("transaction for a new target has invalid original state")
        if not isinstance(entry.get("mode"), int):
            raise ActionError("transaction artifact metadata is invalid")
        if not isinstance(entry.get("replaced"), bool):
            raise ActionError("transaction replacement marker is invalid")
        if operation == "delete":
            if not original_exists or new_sha is not None or entry.get("candidate") is not None:
                raise ActionError("transaction delete metadata is invalid")
        else:
            if not isinstance(new_sha, str) or not SHA256_RE.fullmatch(new_sha):
                raise ActionError("transaction new hash is invalid")
            if not isinstance(entry.get("candidate"), str):
                raise ActionError("transaction candidate metadata is invalid")
            candidate = self._validate_artifact_path(entry["candidate"], target, "candidate", transaction_id)
            if candidate.exists() and _file_sha256(candidate) != new_sha:
                raise ActionError("transaction candidate hash mismatch; possible tampering")
        if entry.get("backup") is not None:
            backup = self._validate_artifact_path(entry["backup"], target, "backup", transaction_id)
            if backup.exists() and _file_sha256(backup) != old_sha:
                raise ActionError("transaction backup hash mismatch; possible tampering")

    def _validate_artifact_path(self, raw: str, target: Path, artifact_kind: str,
                                transaction_id: str) -> Path:
        if not isinstance(raw, str) or not os.path.isabs(raw):
            raise ActionError("transaction artifact path must be absolute")
        candidate = Path(raw)
        try:
            parent = candidate.parent.resolve(strict=True)
        except OSError as exc:
            raise ActionError("transaction artifact parent is unavailable: {}".format(exc))
        if parent != target.parent:
            raise ActionError("transaction artifact escaped the target directory")
        expected_prefix = ".{}.nginx-manager-{}-{}-".format(target.name, artifact_kind, transaction_id)
        if not candidate.name.startswith(expected_prefix) or candidate.name == expected_prefix:
            raise ActionError("transaction artifact name is invalid")
        resolved = parent / candidate.name
        if resolved.exists() or resolved.is_symlink():
            if resolved.is_symlink():
                raise ActionError("transaction artifact must not be a symbolic link")
            status = resolved.stat()
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise ActionError("transaction artifact must be a single regular file")
        return resolved

    def _recover_manifest(self, manifest_path: Path, verify_after: bool,
                          loaded_manifest: Optional[Dict[str, Any]] = None) -> None:
        manifest = loaded_manifest or self._load_manifest(manifest_path)
        interrupted_phase = str(manifest.get("recovery_from_phase") or manifest["phase"])
        if interrupted_phase not in TRANSACTION_PHASES:
            raise ActionError("transaction recovery phase is invalid")
        manifest["recovery_from_phase"] = interrupted_phase
        self._set_manifest_phase(manifest_path, manifest, "recovering")
        changed = False
        try:
            # Validate all entries before touching any target. The manifest cannot
            # be converted into an arbitrary root replace/unlink primitive.
            for entry in manifest["files"]:
                self._validate_manifest_entry(entry, manifest["transaction_id"])

            for entry in reversed(manifest["files"]):
                target = Path(entry["target"])
                candidate = None
                if entry.get("candidate") is not None:
                    candidate = self._validate_artifact_path(
                        entry["candidate"], target, "candidate", manifest["transaction_id"]
                    )
                backup = None
                if entry["backup"] is not None:
                    backup = self._validate_artifact_path(
                        entry["backup"], target, "backup", manifest["transaction_id"]
                    )
                current_sha = _file_sha256(target) if target.exists() else None
                old_sha = entry["old_sha256"]
                new_sha = entry["new_sha256"]

                if entry["original_exists"]:
                    if current_sha not in (old_sha, new_sha, None):
                        raise ActionError("target hash is neither recorded old nor new value: {}".format(target))
                    if backup is not None and backup.exists():
                        if _file_sha256(backup) != old_sha:
                            raise ActionError("verified original backup hash mismatch for {}".format(target))
                        os.replace(str(backup), str(target))
                        _fsync_dir(target.parent)
                        changed = True
                    elif current_sha != old_sha:
                        raise ActionError("verified original backup is unavailable for {}".format(target))
                else:
                    if current_sha not in (new_sha, None):
                        raise ActionError("new transaction target contains an unrecorded value: {}".format(target))
                    if current_sha == new_sha:
                        target.unlink()
                        _fsync_dir(target.parent)
                        changed = True

                if candidate is not None and candidate.exists():
                    candidate.unlink()
                    _fsync_dir(candidate.parent)

            if verify_after and (changed or interrupted_phase != "prepared"):
                self._nginx_test()
                if manifest["reload_enabled"] and interrupted_phase in RECOVERY_RELOAD_PHASES:
                    if self._nginx_is_running():
                        self._reload_only()
                    else:
                        LOG.warning(
                            "transaction %s restored and validated while nginx is stopped; reload is deferred to nginx startup",
                            manifest["transaction_id"],
                        )
            self._set_manifest_phase(manifest_path, manifest, "recovered")
            self._remove_manifest(manifest_path)
        except Exception as exc:
            manifest["recovery_from_phase"] = interrupted_phase
            manifest["last_error"] = _sanitize_output(str(exc), 2048)
            try:
                self._set_manifest_phase(manifest_path, manifest, "recovery_failed")
            except Exception as phase_error:
                LOG.critical("could not persist recovery failure for %s: %s", manifest["transaction_id"], phase_error)
            raise ActionError("transaction {} recovery failed: {}".format(manifest["transaction_id"], exc))

    def _cleanup_committed_manifest(self, manifest_path: Path, manifest: Dict[str, Any]) -> None:
        # A durable commit preserves new files. Validate before deleting only a
        # leftover candidate; successful publication backups remain available.
        for entry in manifest["files"]:
            self._validate_manifest_entry(entry, manifest["transaction_id"])
            target = Path(entry["target"])
            if entry.get("operation", "write") == "delete":
                if target.exists():
                    raise ActionError("committed transaction deleted target reappeared; refusing helper startup")
                candidate = None
            else:
                if not target.exists() or _file_sha256(target) != entry["new_sha256"]:
                    raise ActionError("committed transaction target hash mismatch; refusing helper startup")
                candidate = self._validate_artifact_path(
                    entry["candidate"], target, "candidate", manifest["transaction_id"]
                )
            if candidate is not None and candidate.exists():
                candidate.unlink()
                _fsync_dir(candidate.parent)
        self._remove_manifest(manifest_path)

    def _prune_backups(self, path: Path) -> None:
        keep = max(1, self.settings.backup_retention)
        candidates = sorted(path.parent.glob("." + path.name + ".nginx-manager-backup-*"), key=lambda item: item.stat().st_mtime, reverse=True)
        for old in candidates[keep:]:
            with contextlib.suppress(OSError):
                old.unlink()

    def _health_check(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        url = str(spec.get("url", ""))
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ActionError("health check URL must be absolute http(s)")
        if parsed.username or parsed.password or not parsed.hostname:
            raise ActionError("health check URL must not contain credentials")
        if parsed.hostname.lower() not in self.settings.allowed_health_hosts:
            raise ActionError("health check host is not in allowed_health_hosts")
        expected_raw = spec.get("expected_status", 200)
        expected = {int(value) for value in expected_raw} if isinstance(expected_raw, list) else {int(expected_raw)}
        timeout = min(max(float(spec.get("timeout", 5)), 0.1), 30.0)
        attempts = min(max(int(spec.get("attempts", 3)), 1), 10)
        context = ssl.create_default_context(cafile=_optional_string(spec.get("ca_file")) or self.settings.ca_file)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPHandler(),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirectHandler(),
        )
        last_error = ""
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, method="GET", headers={"User-Agent": "nginx-manager-agent-health/" + VERSION})
            try:
                with opener.open(request, timeout=timeout) as response:
                    status_code = int(response.status)
                if status_code in expected:
                    return {"url": url, "status": status_code, "attempt": attempt}
                last_error = "unexpected HTTP status {}".format(status_code)
            except urllib.error.HTTPError as exc:
                if int(exc.code) in expected:
                    return {"url": url, "status": int(exc.code), "attempt": attempt}
                last_error = "HTTP {}".format(exc.code)
            except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
                last_error = str(exc)
            if attempt < attempts:
                time.sleep(min(attempt, 2))
        raise ActionError("health check failed after {} attempts: {}".format(attempts, last_error))


def _health_from_payload(payload: Dict[str, Any], default: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if "health_check" not in payload:
        return dict(default) if isinstance(default, dict) else None
    value = payload["health_check"]
    if value is False or value is None:
        return None
    if not isinstance(value, dict):
        raise ActionError("health_check must be an object, false, or null")
    return value


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_expired(value: Any) -> bool:
    if not value:
        return False
    if not isinstance(value, str):
        return True
    try:
        parsed = _parse_iso8601(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed <= dt.datetime.now(dt.timezone.utc)
    except ValueError:
        return True


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: List[bytes] = []
    remaining = size
    while remaining:
        data = connection.recv(remaining)
        if not data:
            raise AgentError("unexpected end of helper message")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _recv_frame(connection: socket.socket, limit: int) -> Dict[str, Any]:
    length = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if length < 2 or length > limit:
        raise AgentError("helper request size is invalid")
    raw = _recv_exact(connection, length)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AgentError("helper request is not valid JSON")
    if not isinstance(value, dict):
        raise AgentError("helper request must be an object")
    return value


def _send_frame(connection: socket.socket, value: Dict[str, Any], limit: int) -> None:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > limit:
        raise AgentError("helper response exceeds size limit")
    connection.sendall(struct.pack("!I", len(raw)) + raw)


class HelperClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        request = {"job": job}
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.settings.helper_timeout)
        try:
            connection.connect(self.settings.helper_socket)
            _send_frame(connection, request, self.settings.helper_max_request_bytes)
            response = _recv_frame(connection, self.settings.helper_max_request_bytes)
        except (OSError, AgentError) as exc:
            raise AgentError("privileged helper unavailable: {}".format(exc))
        finally:
            connection.close()
        if not isinstance(response.get("response"), dict):
            raise AgentError("privileged helper returned an invalid response")
        return response["response"]


class HelperServer:
    def __init__(self, settings: Settings, executor: JobExecutor, socket_path: str, allowed_uid: int,
                 socket_group: Optional[str], stop_event: threading.Event):
        self.settings = settings
        self.executor = executor
        self.socket_path = Path(socket_path)
        self.allowed_uid = allowed_uid
        self.socket_group = socket_group
        self.stop_event = stop_event

    def run(self) -> None:
        if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
            raise AgentError("privileged helper requires Linux SO_PEERCRED support")
        recovered = self.executor.recover_incomplete_transactions()
        if recovered:
            LOG.warning("recovered %s interrupted nginx publication transaction(s) before startup", recovered)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            if not stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                raise AgentError("helper socket path exists and is not a socket")
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(str(self.socket_path), 0o660)
            if self.socket_group:
                if grp is None:  # pragma: no cover - helper is Linux-only
                    raise AgentError("socket group lookup is unavailable")
                gid = grp.getgrnam(self.socket_group).gr_gid
                os.chown(str(self.socket_path), 0, gid)
            listener.listen(16)
            listener.settimeout(1.0)
            LOG.info("privileged helper listening on %s for uid %s", self.socket_path, self.allowed_uid)
            while not self.stop_event.is_set():
                try:
                    connection, _address = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(self.settings.helper_timeout)
                    self._serve_one(connection)
        finally:
            listener.close()
            with contextlib.suppress(OSError):
                self.socket_path.unlink()

    def _serve_one(self, connection: socket.socket) -> None:
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", credentials)
        if uid != self.allowed_uid:
            LOG.warning("rejected helper connection from pid=%s uid=%s", pid, uid)
            return
        try:
            request = _recv_frame(connection, self.settings.helper_max_request_bytes)
            job = request.get("job")
            if not isinstance(job, dict):
                raise AgentError("helper request is missing job")
            LOG.info("helper job id=%s action=%s", str(job.get("id", ""))[:200], str(job.get("action", ""))[:100])
            response = self.executor.execute(job)
            _send_frame(connection, {"response": response}, self.settings.helper_max_request_bytes)
        except AgentError as exc:
            _send_frame(connection, {"response": {"status": "failed", "error": str(exc)}}, self.settings.helper_max_request_bytes)


class AgentService:
    def __init__(self, settings: Settings, stop_event: threading.Event):
        self.settings = settings
        self.stop_event = stop_event
        self.api = ApiClient(settings)
        self.identity_path = Path(settings.state_dir) / "identity.json"

    def _read_identity_document(self) -> Optional[Dict[str, Any]]:
        try:
            with self.identity_path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict):
                raise AgentError("agent identity root must be an object")
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise AgentError("cannot read agent identity: {}".format(exc))

    @staticmethod
    def _committed_identity(value: Any) -> Optional[Dict[str, str]]:
        if not isinstance(value, dict) or value.get("enrollment_pending") or not value.get("agent_id"):
            return None
        credential = value.get("machine_credential") or value.get("agent_token")
        if not credential:
            return None
        return {"agent_id": str(value["agent_id"]), "machine_credential": str(credential)}

    def identity(self) -> Optional[Dict[str, str]]:
        value = self._read_identity_document()
        if value is None:
            return None
        if value.get("enrollment_pending"):
            result = self._request_enrollment(value)
            return self._committed_identity(result)
        return self._committed_identity(value)

    def enroll(self, force: bool = False) -> Dict[str, str]:
        document = self._read_identity_document()
        if document is not None and document.get("enrollment_pending") and not force:
            return self._request_enrollment(document)
        existing = self._committed_identity(document)
        if existing is not None and not force:
            return existing

        previous = existing
        if previous is None and isinstance(document, dict) and document.get("enrollment_pending"):
            previous = self._validated_previous_identity(document.get("previous_identity"))
        pending: Dict[str, Any] = {
            "enrollment_pending": True,
            "enrollment_id": str(uuid.uuid4()),
            "enrollment_secret": _new_enrollment_secret(),
        }
        if previous is not None:
            pending["previous_identity"] = previous
        # The Agent owns this secret. It is persisted before the first request,
        # so a lost response can always be retried without operator input.
        _atomic_json(self.identity_path, pending)
        return self._request_enrollment(pending)

    @staticmethod
    def _validated_previous_identity(value: Any) -> Optional[Dict[str, str]]:
        if value is None:
            return None
        if not isinstance(value, dict) or not value.get("agent_id"):
            raise AgentError("pending agent identity has an invalid previous_identity")
        credential = value.get("machine_credential") or value.get("agent_token")
        if not credential:
            raise AgentError("pending agent identity has an invalid previous_identity")
        return {"agent_id": str(value["agent_id"]), "machine_credential": str(credential)}

    def _request_enrollment(self, pending: Dict[str, Any]) -> Dict[str, str]:
        enrollment_id = pending.get("enrollment_id")
        enrollment_secret = pending.get("enrollment_secret")
        if not isinstance(enrollment_id, str) or not enrollment_id:
            raise AgentError("pending enrollment is missing enrollment_id")
        if not isinstance(enrollment_secret, str) or not enrollment_secret:
            raise AgentError("pending enrollment is missing enrollment_secret")
        previous_identity = self._validated_previous_identity(pending.get("previous_identity"))
        try:
            response = self.api.post(
                "/api/v1/agent/enroll",
                {
                    "enrollment_id": enrollment_id,
                    "enrollment_secret": enrollment_secret,
                    "node_name": self.settings.node_name,
                    "hostname": self.settings.hostname,
                    "labels": self.settings.labels,
                },
            )
        except ApiError as exc:
            if exc.status_code in (401, 409):
                if previous_identity:
                    _atomic_json(self.identity_path, previous_identity)
                    LOG.warning("pending enrollment was rejected; restored previous machine identity")
            raise

        enrollment_status = str(response.get("status", ""))
        if enrollment_status == "pending":
            return {"status": "pending", "enrollment_id": enrollment_id}
        if enrollment_status in ("rejected", "expired"):
            if previous_identity:
                _atomic_json(self.identity_path, previous_identity)
            raise AgentError("enrollment request was {} by the control plane".format(enrollment_status))
        if enrollment_status != "approved" or not response.get("agent_id"):
            raise ApiError("control plane returned an invalid enrollment response")
        agent_id = str(response["agent_id"])
        committed = {
            "agent_id": agent_id,
            "machine_credential": _derive_machine_credential(enrollment_secret, enrollment_id, agent_id),
        }
        _atomic_json(self.identity_path, committed)
        return committed

    def _wait_for_identity(self) -> Optional[Dict[str, str]]:
        while not self.stop_event.is_set():
            try:
                result = self.enroll()
                identity = self._committed_identity(result)
                if identity is not None:
                    return identity
                LOG.info("waiting for administrator approval in the Web console")
            except ApiError as exc:
                LOG.warning("enrollment request failed; will retry: %s", exc)
            except AgentError as exc:
                LOG.error("enrollment requires operator attention: %s", exc)
                self.stop_event.wait(30.0)
                continue
            self.stop_event.wait(max(3.0, self.settings.poll_interval))
        return None

    def run(self, once: bool = False, direct_executor: bool = False) -> None:
        identity = self._wait_for_identity()
        if identity is None:
            return
        if direct_executor:
            LOG.warning("DIRECT ROOT EXECUTOR MODE IS FOR DEVELOPMENT ONLY")
            executor: Any = JobExecutor(self.settings, JobStore(Path(self.settings.state_dir) / "direct-jobs.json"))
        else:
            executor = HelperClient(self.settings)
        last_heartbeat = 0.0
        failures = 0
        while not self.stop_event.is_set():
            try:
                now = time.monotonic()
                if now - last_heartbeat >= self.settings.heartbeat_interval:
                    self._heartbeat(identity)
                    last_heartbeat = now
                polled = self.api.post("/api/v1/agent/poll", {"agent_id": identity["agent_id"], "limit": 1}, identity["machine_credential"])
                jobs = polled.get("jobs")
                if jobs is None:  # Backward compatibility with an early singular response.
                    jobs = [] if polled.get("job") is None else [polled.get("job")]
                if not isinstance(jobs, list) or any(not isinstance(item, dict) for item in jobs):
                    raise ApiError("poll response jobs must be an array of objects")
                for job in jobs:
                    job_id = str(job.get("id", ""))
                    action = str(job.get("action", ""))
                    LOG.info("received job id=%s action=%s", job_id[:200], action[:100])
                    try:
                        result = executor.execute(job)
                    except AgentError as exc:
                        result = JobExecutor._response(job_id, action, "failed", error=str(exc))
                    server_result = _to_server_result(result)
                    self.api.post("/api/v1/agent/jobs/{}/result".format(urllib.parse.quote(job_id, safe="")), server_result, identity["machine_credential"])
                failures = 0
                if once:
                    return
                self.stop_event.wait(self.settings.poll_interval)
            except AgentError as exc:
                failures += 1
                LOG.warning("agent loop error: %s", exc)
                if once:
                    raise
                self.stop_event.wait(min(30.0, max(self.settings.poll_interval, 2 ** min(failures, 5))))

    def _heartbeat(self, identity: Dict[str, str]) -> None:
        observation = self._local_observation()
        self.api.post(
            "/api/v1/agent/heartbeat",
            {
                "agent_id": identity["agent_id"],
                "node_name": self.settings.node_name,
                "hostname": self.settings.hostname,
                "labels": self.settings.labels,
                "agent_version": VERSION,
                "capabilities": list(CAPABILITIES),
                "status": "online",
                "timestamp": utc_now(),
                **observation,
            },
            identity["machine_credential"],
        )

    def _local_observation(self) -> Dict[str, Any]:
        observation: Dict[str, Any] = {
            "facts": {
                "nginx_root": self.settings.nginx_root,
                "managed_config_root": self.settings.allowed_config_roots[0],
                "managed_certificate_root": self.settings.allowed_certificate_roots[0],
            }
        }
        try:
            completed = subprocess.run(
                [self.settings.nginx_binary, "-v"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=min(self.settings.command_timeout, 5.0),
                check=False,
                shell=False,
            )
            version_text = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
            matched = re.search(r"nginx version:\s*(?:nginx/)?([^\s]+)", version_text, re.IGNORECASE)
            if completed.returncode == 0 and matched:
                observation["nginx_version"] = matched.group(1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            config = Path(self.settings.nginx_config)
            if config.is_file() and not config.is_symlink():
                observation["config_hash"] = _file_sha256(config)
        except OSError:
            pass
        return observation


def _to_server_result(local: Dict[str, Any]) -> Dict[str, Any]:
    """Map the helper-internal record to the small control-plane result schema."""
    action = str(local.get("action", ""))
    succeeded = local.get("status") == "succeeded"
    raw = local.get("result") if isinstance(local.get("result"), dict) else {}
    details: Dict[str, Any] = {}
    output = ""

    if action == "inspect":
        details = {
            key: raw[key]
            for key in ("agent_version", "hostname", "platform", "python", "nginx_config", "capabilities")
            if key in raw
        }
        if raw.get("config_sha256"):
            details["config_hash"] = raw["config_sha256"]
        nginx = raw.get("nginx") if isinstance(raw.get("nginx"), dict) else {}
        nginx_text = str(nginx.get("stderr") or nginx.get("stdout") or "")
        match = re.search(r"nginx version:\s*(?:nginx/)?([^\s]+)", nginx_text, re.IGNORECASE)
        if match:
            details["nginx_version"] = match.group(1)
        output = nginx_text
    elif action == "nginx_test":
        details = {"syntax_ok": succeeded}
        output = str(raw.get("stderr") or raw.get("stdout") or "")
    elif action == "nginx_reload":
        details = {"syntax_ok": succeeded, "reloaded": succeeded}
        tested = raw.get("test") if isinstance(raw.get("test"), dict) else {}
        reloaded = raw.get("reload") if isinstance(raw.get("reload"), dict) else {}
        output = "\n".join(str(value) for value in (tested.get("stderr") or tested.get("stdout"), reloaded.get("stderr") or reloaded.get("stdout")) if value)
    elif action == "config_inventory":
        details = {
            "files": raw.get("files", []),
            "file_count": raw.get("file_count", 0),
            "total_bytes": raw.get("total_bytes", 0),
            "skipped_count": raw.get("skipped_count", 0),
            "truncated": bool(raw.get("truncated", False)),
        }
    elif action == "config_hash":
        details = {"path": raw.get("path"), "config_hash": raw.get("sha256"), "size": raw.get("size")}
    elif action == "config_read":
        details = {key: raw.get(key) for key in ("path", "content", "sha256", "size")}
        details["config_hash"] = details.pop("sha256", None)
    elif action == "config_apply":
        details = {
            "path": raw.get("path"),
            "config_hash": raw.get("sha256"),
            "previous_config_hash": raw.get("previous_sha256"),
            "syntax_ok": bool(raw.get("validated")) and succeeded,
            "applied": bool(raw.get("applied")),
            "validate_only": bool(raw.get("validate_only")),
            "reloaded": bool(raw.get("reloaded")),
            "backup_count": raw.get("backup_count", 0),
        }
        tested = raw.get("test") if isinstance(raw.get("test"), dict) else {}
        output = str(tested.get("stderr") or tested.get("stdout") or "")
    elif action == "config_delete":
        details = {
            "path": raw.get("path"),
            "previous_config_hash": raw.get("previous_sha256"),
            "deleted": bool(raw.get("deleted")) and succeeded,
            "syntax_ok": bool(raw.get("validated")) and succeeded,
            "reloaded": bool(raw.get("reloaded")) and succeeded,
            "backup_count": raw.get("backup_count", 0),
        }
        tested = raw.get("test") if isinstance(raw.get("test"), dict) else {}
        output = str(tested.get("stderr") or tested.get("stdout") or "")
    elif action == "certificate_apply":
        # Never return key material. The digest is safe concurrency metadata used
        # to prevent a later certificate update from overwriting an unknown key.
        details = {
            "certificate_path": raw.get("certificate_path"),
            "certificate_sha256": raw.get("certificate_sha256"),
            "key_material_sha256": raw.get("private_key_sha256"),
            "certificate_fingerprint": raw.get("certificate_fingerprint"),
            "syntax_ok": bool(raw.get("validated")) and succeeded,
            "applied": bool(raw.get("applied")),
            "validate_only": bool(raw.get("validate_only")),
            "reloaded": bool(raw.get("reloaded")),
            "backup_count": raw.get("backup_count", 0),
        }
        tested = raw.get("test") if isinstance(raw.get("test"), dict) else {}
        output = str(tested.get("stderr") or tested.get("stdout") or "")

    response: Dict[str, Any] = {
        "status": "succeeded" if succeeded else "failed",
        "job_id": str(local.get("job_id", ""))[:200],
        "action": action[:64],
        "details": {key: value for key, value in details.items() if value is not None},
        "duration_ms": _duration_ms(local.get("started_at"), local.get("finished_at")),
    }
    if not succeeded:
        response["error"] = _sanitize_output(str(local.get("error") or "job did not succeed"))
    sanitized_output = _sanitize_output(output)
    if sanitized_output:
        response["output"] = sanitized_output
    return response


def _sanitize_output(value: str, limit: int = 16 * 1024) -> str:
    value = re.sub(
        r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        value,
        flags=re.DOTALL,
    )
    return value[-limit:]


def _duration_ms(started: Any, finished: Any) -> int:
    try:
        start = _parse_iso8601(started)
        end = _parse_iso8601(finished)
        return max(0, int((end - start).total_seconds() * 1000))
    except (TypeError, ValueError):
        return 0


def _resolve_uid(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        if pwd is None:
            raise AgentError("allowed uid must be numeric on this platform")
        try:
            return pwd.getpwnam(value).pw_uid
        except KeyError:
            raise AgentError("unknown allowed uid/user: " + value)


def _warn_config_permissions(path: str) -> None:
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o022:
            LOG.warning("config %s is writable by group/world; remove group/world write access", path)
        elif mode & 0o004:
            LOG.warning("config %s is world readable; remove world read access", path)
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="nginx-manager Linux agent")
    parser.add_argument("--config", default="/etc/nginx-manager-agent/config.json", help="path to JSON configuration")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    # `required=` for subparsers was added in Python 3.7. Keep the parser
    # constructible on CentOS 7's Python 3.6 and enforce it after parsing.
    subcommands = parser.add_subparsers(dest="command")
    enroll = subcommands.add_parser("enroll", help="request Web approval for this Agent")
    enroll.add_argument("--force", action="store_true", help="request approval to replace an existing identity")
    run = subcommands.add_parser("run", help="run the unprivileged network agent")
    run.add_argument("--once", action="store_true", help="poll once and exit")
    run.add_argument("--direct-executor", action="store_true", help="development only: execute locally without helper")
    helper = subcommands.add_parser("helper", help="run the root Unix-socket helper")
    helper.add_argument("--socket", help="override helper_socket")
    helper.add_argument("--allowed-uid", required=True, help="only this numeric uid or user may connect")
    helper.add_argument("--socket-group", help="group owner for the mode-0660 socket")
    subcommands.add_parser("recover", help="recover interrupted publications before nginx starts")
    subcommands.add_parser("validate-config", help="validate configuration and exit")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error("a command is required")
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.load(args.config)
        _warn_config_permissions(args.config)
        stop_event = threading.Event()

        def stop(_signum: int, _frame: Any) -> None:
            stop_event.set()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)

        if args.command == "validate-config":
            print("configuration OK")
            return 0
        if args.command == "enroll":
            identity = AgentService(settings, stop_event).enroll(force=args.force)
            if identity.get("status") == "pending":
                print("enrollment pending; approve node={} in the Web console".format(settings.node_name))
            else:
                print("enrolled agent_id={}".format(identity["agent_id"]))
            return 0
        if args.command == "run":
            AgentService(settings, stop_event).run(once=args.once, direct_executor=args.direct_executor)
            return 0
        if args.command == "recover":
            if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0:
                raise AgentError("recover must run as root")
            settings.state_dir = settings.helper_state_dir
            executor = JobExecutor(settings, JobStore(Path(settings.helper_state_dir) / "helper-jobs.json"))
            recovered = executor.recover_incomplete_transactions()
            print("recovery OK; restored {} interrupted transaction(s)".format(recovered))
            return 0
        if args.command == "helper":
            socket_path = args.socket or settings.helper_socket
            settings.state_dir = settings.helper_state_dir
            executor = JobExecutor(settings, JobStore(Path(settings.helper_state_dir) / "helper-jobs.json"))
            HelperServer(settings, executor, socket_path, _resolve_uid(args.allowed_uid), args.socket_group, stop_event).run()
            return 0
        parser.error("unknown command")
        return 2
    except AgentError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
