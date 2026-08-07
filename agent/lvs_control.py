#!/usr/bin/env python3
"""Structured Keepalived/IPVS control for the privileged Agent helper.

The module deliberately exposes a small interface: ``observe``, ``apply`` and
``recover``.  It never accepts shell commands, executable paths, service unit
names or raw Keepalived text from the control plane.  Those values come only
from the root-owned Agent configuration.

Keepalived remains the persistent source of truth.  ``/proc/net/ip_vs`` is
read only and is used to verify that a successful reload converged.
"""

from __future__ import print_function

import base64
import glob
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
MAX_CONFIG_FILES = 128
MAX_CONFIG_BYTES = 8 * 1024 * 1024
MAX_INCLUDE_DEPTH = 16
MAX_SERVICES = 256
MAX_MEMBERS = 1024
MAX_SERVICE_MEMBERS = 256
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
SAFE_ALGORITHMS = {
    "rr", "wrr", "lc", "wlc", "lblc", "lblcr", "dh", "sh", "sed", "nq", "mh",
}
SAFE_FORWARDING = {"DR", "NAT", "TUN"}
SAFE_PROTOCOLS = {"TCP", "UDP", "SCTP"}
INCLUDE_DIRECTIVES = {"include", "includer", "includem", "includew", "includeb", "includea"}
IPVS_TABLE_PATH = "/proc/net/ip_vs"
TRANSACTION_PHASES = {
    "prepared", "replacing", "validating", "reloading", "verifying",
    "rolling_back", "rolled_back", "committed", "recovered", "recovery_failed", "aborted",
}
TERMINAL_TRANSACTION_PHASES = {"rolled_back", "committed", "recovered", "aborted"}
MAX_TRANSACTION_FILES = 8
MAX_TRANSACTION_MANIFEST_BYTES = 128 * 1024
MAX_RETAINED_TERMINAL_TRANSACTIONS = 64


class LvsControlError(Exception):
    """Stable, redacted error returned through the Agent result envelope."""

    def __init__(
        self,
        message: str,
        code: str = "lvs_operation_failed",
        stage: str = "precheck",
        rolled_back: bool = False,
        reload_attempted: bool = False,
    ) -> None:
        super().__init__(message)
        self.failure_code = code
        self.failure_stage = stage
        self.rolled_back = rolled_back
        self.reload_attempted = reload_attempted


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _current_owner() -> Tuple[int, int]:
    return int(getattr(os, "getuid", lambda: 0)()), int(getattr(os, "getgid", lambda: 0)())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, data: bytes, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(temporary), stat.S_IMODE(mode))
        try:
            chown = getattr(os, "chown", None)
            if chown is not None:
                chown(str(temporary), uid, gid)
        except PermissionError:
            pass
        os.replace(str(temporary), str(path))
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _strip_comments(text: str) -> str:
    output: List[str] = []
    quote: Optional[str] = None
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            output.append(char)
            escaped = False
        elif quote:
            output.append(char)
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ("'", '"'):
            output.append(char)
            quote = char
        elif char in ("#", "!"):
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            if index < len(text):
                output.append(text[index])
        else:
            output.append(char)
        index += 1
    if quote:
        raise LvsControlError(
            "Keepalived configuration contains an unterminated quoted string",
            "lvs_config_unsupported",
            "inventory",
        )
    return "".join(output)


def _lex(text: str, keep_newlines: bool = False) -> List[Tuple[str, int, int]]:
    """Return Keepalived tokens and byte-like character spans.

    The lexer intentionally understands only quoting, braces and comments.  It
    does not evaluate Keepalived directives and therefore cannot execute or
    expand data from the configuration.
    """

    tokens: List[Tuple[str, int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\f\v":
            index += 1
            continue
        if char in "\r\n":
            start = index
            if char == "\r" and index + 1 < length and text[index + 1] == "\n":
                index += 2
            else:
                index += 1
            if keep_newlines:
                tokens.append(("\n", start, index))
            continue
        if char in ("#", "!"):
            while index < length and text[index] not in "\r\n":
                index += 1
            continue
        if char in "{}":
            tokens.append((char, index, index + 1))
            index += 1
            continue
        start = index
        if char in ("'", '"'):
            quote = char
            index += 1
            escaped = False
            while index < length:
                current = text[index]
                index += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == quote:
                    break
            else:
                raise LvsControlError(
                    "Keepalived configuration contains an unterminated quoted string",
                    "lvs_config_unsupported",
                    "inventory",
                )
            tokens.append((text[start:index], start, index))
            continue
        while index < length:
            current = text[index]
            if current.isspace() or current in "{}#!":
                break
            index += 1
        tokens.append((text[start:index], start, index))
    return tokens


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _resolved_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_ip(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise LvsControlError(field + " must be an IP address", "invalid_lvs_intent", "compile")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise LvsControlError(field + " must be an IP address", "invalid_lvs_intent", "compile")


def _safe_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise LvsControlError(
            "{} must be between {} and {}".format(field, minimum, maximum),
            "invalid_lvs_intent",
            "compile",
        )
    return value


def _only_keys(value: Dict[str, Any], allowed: Iterable[str], label: str) -> None:
    extras = sorted(set(value) - set(allowed))
    if extras:
        raise LvsControlError(
            "{} contains unsupported fields".format(label),
            "invalid_lvs_intent",
            "compile",
        )


def normalize_listener(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise LvsControlError("listener must be an object", "invalid_lvs_intent", "compile")
    _only_keys(value, {"address", "port", "protocol"}, "listener")
    protocol = str(value.get("protocol", "TCP")).upper()
    if protocol not in SAFE_PROTOCOLS:
        raise LvsControlError("unsupported LVS protocol", "unsupported_protocol", "compile")
    return {
        "address": _safe_ip(value.get("address"), "listener.address"),
        "port": _safe_int(value.get("port"), "listener.port", 1, 65535),
        "protocol": protocol,
    }


def listener_key(value: Dict[str, Any]) -> str:
    listener = normalize_listener(value)
    return "{}:{}:{}".format(listener["protocol"], listener["address"], listener["port"])


def normalize_monitor(value: Any, member_port: int) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LvsControlError("monitor must be an object", "invalid_lvs_intent", "compile")
    _only_keys(
        value,
        {"kind", "connect_timeout", "retries", "delay_before_retry", "connect_port"},
        "monitor",
    )
    kind = str(value.get("kind", "tcp")).lower()
    if kind != "tcp":
        raise LvsControlError("only TCP health monitors are supported", "unsupported_monitor", "compile")
    return {
        "kind": "tcp",
        "connect_timeout": _safe_int(value.get("connect_timeout", 3), "monitor.connect_timeout", 1, 300),
        "retries": _safe_int(value.get("retries", 3), "monitor.retries", 1, 20),
        "delay_before_retry": _safe_int(
            value.get("delay_before_retry", 3), "monitor.delay_before_retry", 1, 300
        ),
        "connect_port": _safe_int(value.get("connect_port", member_port), "monitor.connect_port", 1, 65535),
    }


def normalize_member(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise LvsControlError("member must be an object", "invalid_lvs_intent", "compile")
    _only_keys(value, {"address", "port", "weight", "enabled", "monitor"}, "member")
    port = _safe_int(value.get("port"), "member.port", 1, 65535)
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise LvsControlError("member.enabled must be a boolean", "invalid_lvs_intent", "compile")
    weight = _safe_int(value.get("weight", 1), "member.weight", 1, 65535)
    return {
        "address": _safe_ip(value.get("address"), "member.address"),
        "port": port,
        "weight": weight,
        "enabled": enabled,
        "monitor": normalize_monitor(value.get("monitor"), port),
    }


def normalize_service(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise LvsControlError("service must be an object", "invalid_lvs_intent", "compile")
    _only_keys(
        value,
        {"name", "listener", "scheduler", "forwarding", "delay_loop", "persistence_seconds", "members"},
        "service",
    )
    name = str(value.get("name", "")).strip()
    if not name or len(name) > 128 or "#" in name or re.search(r"[\x00-\x1f]", name):
        raise LvsControlError("service.name is invalid", "invalid_lvs_intent", "compile")
    scheduler = str(value.get("scheduler", "wlc")).lower()
    if scheduler not in SAFE_ALGORITHMS:
        raise LvsControlError("unsupported LVS scheduler", "unsupported_scheduler", "compile")
    forwarding = str(value.get("forwarding", "DR")).upper()
    if forwarding not in SAFE_FORWARDING:
        raise LvsControlError("unsupported LVS forwarding mode", "unsupported_forwarding", "compile")
    persistence = value.get("persistence_seconds")
    if persistence is not None:
        persistence = _safe_int(persistence, "service.persistence_seconds", 1, 86400)
    raw_members = value.get("members")
    if not isinstance(raw_members, list) or not raw_members or len(raw_members) > MAX_SERVICE_MEMBERS:
        raise LvsControlError(
            "service.members must contain between 1 and {} members".format(MAX_SERVICE_MEMBERS),
            "invalid_lvs_intent",
            "compile",
        )
    members = [normalize_member(item) for item in raw_members]
    member_keys = [(item["address"], item["port"]) for item in members]
    if len(set(member_keys)) != len(member_keys):
        raise LvsControlError("service contains duplicate members", "duplicate_member", "compile")
    if not any(item["enabled"] for item in members):
        raise LvsControlError("at least one member must remain enabled", "last_enabled_member", "compile")
    return {
        "name": name,
        "listener": normalize_listener(value.get("listener")),
        "scheduler": scheduler,
        "forwarding": forwarding,
        "delay_loop": _safe_int(value.get("delay_loop", 6), "service.delay_loop", 1, 3600),
        "persistence_seconds": persistence,
        "members": members,
    }


def normalize_intent(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise LvsControlError("intent must be an object", "invalid_lvs_intent", "compile")
    _only_keys(value, {"kind", "target", "service", "change_note"}, "intent")
    kind = str(value.get("kind", ""))
    if kind not in {"upsert_service", "delete_service"}:
        raise LvsControlError("unsupported LVS intent", "invalid_lvs_intent", "compile")
    note = str(value.get("change_note", "")).strip()
    if len(note) > 500 or re.search(r"[\x00-\x08\x0b-\x1f]", note):
        raise LvsControlError("intent.change_note is invalid", "invalid_lvs_intent", "compile")
    target = normalize_listener(value.get("target"))
    service = None
    if kind == "upsert_service":
        service = normalize_service(value.get("service"))
        if listener_key(service["listener"]) != listener_key(target):
            raise LvsControlError(
                "an existing Virtual Service listener is immutable; create a new service instead",
                "invalid_lvs_intent",
                "compile",
            )
    elif value.get("service") is not None:
        raise LvsControlError("delete_service must not include service", "invalid_lvs_intent", "compile")
    return {"kind": kind, "target": target, "service": service, "change_note": note}


def render_service(value: Dict[str, Any]) -> bytes:
    service = normalize_service(value)
    listener = service["listener"]
    lines = [
        "# nginx-manager: managed",
        "# name: {}".format(service["name"]),
        "virtual_server {} {} {{".format(listener["address"], listener["port"]),
        "    delay_loop {}".format(service["delay_loop"]),
        "    lb_algo {}".format(service["scheduler"]),
        "    lb_kind {}".format(service["forwarding"]),
        "    protocol {}".format(listener["protocol"]),
    ]
    if service["persistence_seconds"] is not None:
        lines.append("    persistence_timeout {}".format(service["persistence_seconds"]))
    for member in service["members"]:
        lines.extend([
            "",
            "    real_server {} {} {{".format(member["address"], member["port"]),
            "        weight {}".format(member["weight"] if member["enabled"] else 0),
        ])
        monitor = member["monitor"]
        if monitor:
            lines.extend([
                "        TCP_CHECK {",
                "            connect_timeout {}".format(monitor["connect_timeout"]),
                "            nb_get_retry {}".format(monitor["retries"]),
                "            delay_before_retry {}".format(monitor["delay_before_retry"]),
                "            connect_port {}".format(monitor["connect_port"]),
                "        }",
            ])
        lines.append("    }")
    lines.extend(["}", ""])
    return ("\n".join(lines) + "\n").encode("utf-8")


def _next_token(tokens: Sequence[Tuple[str, int, int]], index: int) -> int:
    while index < len(tokens) and tokens[index][0] == "\n":
        index += 1
    return index


def _matching_brace(tokens: Sequence[Tuple[str, int, int]], opening: int) -> int:
    depth = 1
    index = opening + 1
    while index < len(tokens):
        if tokens[index][0] == "{":
            depth += 1
        elif tokens[index][0] == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise LvsControlError(
        "Keepalived configuration contains an unterminated block",
        "lvs_config_unsupported",
        "inventory",
    )


def _line_end(tokens: Sequence[Tuple[str, int, int]], index: int) -> int:
    while index < len(tokens) and tokens[index][0] not in {"\n", "}"}:
        index += 1
    return index


def _parse_integer_token(
    tokens: Sequence[Tuple[str, int, int]], index: int, minimum: int, maximum: int
) -> Optional[int]:
    index = _next_token(tokens, index)
    if index >= len(tokens):
        return None
    try:
        number = int(_unquote(tokens[index][0]))
    except ValueError:
        return None
    return number if minimum <= number <= maximum else None


def _parse_tcp_monitor(tokens: Sequence[Tuple[str, int, int]]) -> Tuple[Dict[str, Any], List[str]]:
    monitor = {
        "kind": "tcp",
        "connect_timeout": 3,
        "retries": 3,
        "delay_before_retry": 3,
        "connect_port": None,
    }
    unknown: List[str] = []
    scalar = {
        "connect_timeout": ("connect_timeout", 1, 300),
        "nb_get_retry": ("retries", 1, 20),
        "delay_before_retry": ("delay_before_retry", 1, 300),
        "connect_port": ("connect_port", 1, 65535),
    }
    index = 0
    while index < len(tokens):
        token = tokens[index][0]
        if token in {"\n", "{", "}"}:
            index += 1
            continue
        lowered = _unquote(token).lower()
        value_index = _next_token(tokens, index + 1)
        if lowered in scalar:
            field, minimum, maximum = scalar[lowered]
            number = _parse_integer_token(tokens, value_index, minimum, maximum)
            if number is None:
                unknown.append(lowered)
            else:
                monitor[field] = number
            index = _line_end(tokens, value_index + 1)
            continue
        if value_index < len(tokens) and tokens[value_index][0] == "{":
            index = _matching_brace(tokens, value_index) + 1
        else:
            index = _line_end(tokens, value_index + 1)
        unknown.append(lowered[:64])
    return monitor, sorted(set(unknown))


def _parse_real_server(
    address_token: str,
    port_token: str,
    tokens: Sequence[Tuple[str, int, int]],
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    try:
        address = str(ipaddress.ip_address(_unquote(address_token)))
        port = int(_unquote(port_token))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        return None, ["invalid_real_server"]
    member: Dict[str, Any] = {
        "address": address,
        "port": port,
        "weight": 1,
        "enabled": True,
        "monitor": None,
    }
    unknown: List[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index][0]
        if token in {"\n", "{", "}"}:
            index += 1
            continue
        lowered = _unquote(token).lower()
        value_index = _next_token(tokens, index + 1)
        if lowered == "weight":
            weight = _parse_integer_token(tokens, value_index, 0, 65535)
            if weight is None:
                unknown.append("weight")
            else:
                member["weight"] = max(1, weight)
                member["enabled"] = weight > 0
            index = _line_end(tokens, value_index + 1)
            continue
        if lowered == "tcp_check" and value_index < len(tokens) and tokens[value_index][0] == "{":
            closing = _matching_brace(tokens, value_index)
            monitor, monitor_unknown = _parse_tcp_monitor(tokens[value_index + 1:closing])
            if monitor["connect_port"] is None:
                monitor["connect_port"] = port
            member["monitor"] = monitor
            unknown.extend("TCP_CHECK." + item for item in monitor_unknown)
            index = closing + 1
            continue
        if value_index < len(tokens) and tokens[value_index][0] == "{":
            index = _matching_brace(tokens, value_index) + 1
        else:
            index = _line_end(tokens, value_index + 1)
        unknown.append(lowered[:64])
    return member, sorted(set(unknown))


def _parse_virtual_service(
    address_token: str,
    port_token: str,
    tokens: Sequence[Tuple[str, int, int]],
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    try:
        address = str(ipaddress.ip_address(_unquote(address_token)))
        port = int(_unquote(port_token))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        return None, ["unsupported_virtual_server_header"]
    service: Dict[str, Any] = {
        "name": "{}:{}".format(address, port),
        "listener": {"address": address, "port": port, "protocol": "TCP"},
        "scheduler": "wlc",
        "forwarding": "NAT",
        "delay_loop": 6,
        "persistence_seconds": None,
        "members": [],
    }
    unknown: List[str] = []
    scalar = {
        "delay_loop": ("delay_loop", 1, 3600),
        "persistence_timeout": ("persistence_seconds", 1, 86400),
    }
    index = 0
    while index < len(tokens):
        token = tokens[index][0]
        if token in {"\n", "{", "}"}:
            index += 1
            continue
        lowered = _unquote(token).lower()
        value_index = _next_token(tokens, index + 1)
        if lowered in scalar:
            field, minimum, maximum = scalar[lowered]
            number = _parse_integer_token(tokens, value_index, minimum, maximum)
            if number is None:
                unknown.append(lowered)
            else:
                service[field] = number
            index = _line_end(tokens, value_index + 1)
            continue
        if lowered in {"lb_algo", "lvs_sched"}:
            if value_index < len(tokens):
                algorithm = _unquote(tokens[value_index][0]).lower()
                if algorithm in SAFE_ALGORITHMS:
                    service["scheduler"] = algorithm
                else:
                    unknown.append("unsupported_scheduler")
            index = _line_end(tokens, value_index + 1)
            continue
        if lowered in {"lb_kind", "lvs_method"}:
            if value_index < len(tokens):
                forwarding = _unquote(tokens[value_index][0]).upper()
                aliases = {"MASQ": "NAT", "TUNNEL": "TUN", "ROUTE": "DR"}
                forwarding = aliases.get(forwarding, forwarding)
                if forwarding in SAFE_FORWARDING:
                    service["forwarding"] = forwarding
                else:
                    unknown.append("unsupported_forwarding")
            index = _line_end(tokens, value_index + 1)
            continue
        if lowered == "protocol":
            if value_index < len(tokens):
                protocol = _unquote(tokens[value_index][0]).upper()
                if protocol in SAFE_PROTOCOLS:
                    service["listener"]["protocol"] = protocol
                else:
                    unknown.append("unsupported_protocol")
            index = _line_end(tokens, value_index + 1)
            continue
        if lowered == "real_server":
            address_index = value_index
            port_index = _next_token(tokens, address_index + 1)
            opening = _next_token(tokens, port_index + 1)
            if opening >= len(tokens) or tokens[opening][0] != "{":
                unknown.append("invalid_real_server")
                index = _line_end(tokens, opening)
                continue
            closing = _matching_brace(tokens, opening)
            member, member_unknown = _parse_real_server(
                tokens[address_index][0] if address_index < len(tokens) else "",
                tokens[port_index][0] if port_index < len(tokens) else "",
                tokens[opening + 1:closing],
            )
            if member is not None:
                service["members"].append(member)
            unknown.extend("real_server." + item for item in member_unknown)
            index = closing + 1
            continue
        # These directives materially affect traffic but are not yet expressible
        # in the structured model, so the whole service becomes read-only.
        if value_index < len(tokens) and tokens[value_index][0] == "{":
            index = _matching_brace(tokens, value_index) + 1
        else:
            index = _line_end(tokens, value_index + 1)
        unknown.append(lowered[:64])
    if len(service["members"]) > MAX_SERVICE_MEMBERS:
        unknown.append("too_many_members")
        service["members"] = service["members"][:MAX_SERVICE_MEMBERS]
    return service, sorted(set(unknown))


def _virtual_service_blocks(path: Path, data: bytes, managed_file: Path) -> List[Dict[str, Any]]:
    text = data.decode("utf-8", errors="replace")
    tokens = _lex(text, keep_newlines=True)
    blocks: List[Dict[str, Any]] = []
    depth = 0
    index = 0
    while index < len(tokens) and len(blocks) < MAX_SERVICES:
        token = tokens[index][0]
        if token == "{":
            depth += 1
            index += 1
            continue
        if token == "}":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth != 0 or _unquote(token).lower() != "virtual_server":
            index += 1
            continue
        address_index = _next_token(tokens, index + 1)
        port_index = _next_token(tokens, address_index + 1)
        opening = _next_token(tokens, port_index + 1)
        if opening >= len(tokens) or tokens[opening][0] != "{":
            index += 1
            continue
        closing = _matching_brace(tokens, opening)
        service, unknown = _parse_virtual_service(
            tokens[address_index][0] if address_index < len(tokens) else "",
            tokens[port_index][0] if port_index < len(tokens) else "",
            tokens[opening + 1:closing],
        )
        if service is not None:
            block_start = tokens[index][1]
            managed_header = re.search(
                r"(?m)^[ \t]*# nginx-manager: managed[ \t]*\r?\n"
                r"[ \t]*#\s*name:\s*([^#\x00-\x1f]{1,128})[ \t]*\r?\n[ \t]*\Z",
                text[:block_start],
            )
            if managed_header:
                service["name"] = managed_header.group(1).strip()
                block_start = managed_header.start()
            service["origin"] = "managed" if path == managed_file else "existing"
            service["source_path"] = str(path)
            service["editable"] = not unknown
            service["unsupported_directives"] = unknown[:32]
            service["id"] = listener_key(service["listener"])
            blocks.append({
                "service": service,
                "start": block_start,
                "end": tokens[closing][2],
            })
        index = closing + 1
    return blocks


def _include_names(path: Path, text: str) -> List[str]:
    tokens = _lex(text, keep_newlines=True)
    names: List[str] = []
    index = 0
    while index < len(tokens):
        lowered = _unquote(tokens[index][0]).lower()
        if lowered not in INCLUDE_DIRECTIVES:
            index += 1
            continue
        value_index = _next_token(tokens, index + 1)
        if value_index < len(tokens):
            raw = _unquote(tokens[value_index][0])
            if raw and "\x00" not in raw and len(raw) <= 4096:
                names.append(raw)
        index = value_index + 1
    return names


def read_config_graph(main_config: Path, maximum_file_bytes: int) -> List[Tuple[Path, bytes]]:
    root = main_config.resolve(strict=True).parent
    active: List[Path] = []
    seen: Dict[Path, bytes] = {}
    total = [0]

    def visit(candidate: Path, depth: int) -> None:
        if depth > MAX_INCLUDE_DEPTH:
            raise LvsControlError("Keepalived include depth is too large", "lvs_config_unsupported", "inventory")
        try:
            if candidate.is_symlink():
                raise LvsControlError("Keepalived includes must not use symbolic links", "path_rejected", "inventory")
            resolved = candidate.resolve(strict=True)
            if not _resolved_inside(resolved, root):
                raise LvsControlError("Keepalived include leaves the configured directory", "path_rejected", "inventory")
            if resolved in seen:
                return
            if resolved in active:
                raise LvsControlError("Keepalived include graph contains a cycle", "lvs_config_unsupported", "inventory")
            status = resolved.stat()
            if not stat.S_ISREG(status.st_mode):
                raise LvsControlError("Keepalived include is not a regular file", "path_rejected", "inventory")
            limit = min(maximum_file_bytes, MAX_CONFIG_BYTES - total[0])
            with resolved.open("rb") as handle:
                data = handle.read(limit + 1)
            if len(data) > limit:
                raise LvsControlError("Keepalived configuration is too large", "lvs_config_unsupported", "inventory")
        except LvsControlError:
            raise
        except OSError:
            raise LvsControlError("Keepalived configuration is unavailable", "lvs_inventory_incomplete", "inventory")
        active.append(resolved)
        total[0] += len(data)
        if total[0] > MAX_CONFIG_BYTES or len(seen) >= MAX_CONFIG_FILES:
            raise LvsControlError("Keepalived configuration graph is too large", "lvs_config_unsupported", "inventory")
        seen[resolved] = data
        try:
            for raw_name in _include_names(resolved, data.decode("utf-8", errors="replace")):
                pattern = Path(raw_name)
                if not pattern.is_absolute():
                    pattern = resolved.parent / pattern
                pattern_text = os.path.abspath(os.path.normpath(str(pattern)))
                pattern_path = Path(pattern_text)
                # Check the non-glob prefix as well as every expanded path.
                prefix_text = re.split(r"[*?[]", pattern_text, maxsplit=1)[0]
                prefix = Path(prefix_text or str(root)).resolve()
                if not _resolved_inside(prefix, root):
                    raise LvsControlError("Keepalived include leaves the configured directory", "path_rejected", "inventory")
                matches = sorted(glob.glob(pattern_text))
                for matched in matches:
                    visit(Path(matched), depth + 1)
        finally:
            active.pop()

    visit(main_config, 0)
    return sorted(seen.items(), key=lambda item: str(item[0]))


def graph_hash(graph: Sequence[Tuple[Path, bytes]], root: Path) -> str:
    records = []
    for path, data in graph:
        try:
            relative = str(path.relative_to(root))
        except ValueError:
            relative = path.name
        records.append({"path": relative, "sha256": _sha256_bytes(data)})
    return _sha256_bytes(_canonical_json(records).encode("utf-8"))


def _decode_ipvs_address(value: str) -> Optional[str]:
    try:
        if value.startswith("[") and value.endswith("]"):
            return str(ipaddress.ip_address(value[1:-1]))
        if len(value) == 8:
            return str(ipaddress.IPv4Address(int(value, 16)))
        if len(value) == 32:
            return str(ipaddress.ip_address(bytes.fromhex(value)))
    except ValueError:
        return None
    return None


def _read_ipvs_services(path: str = IPVS_TABLE_PATH, strict: bool = False) -> List[Dict[str, Any]]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
    except OSError:
        if strict:
            raise LvsControlError(
                "IPVS runtime state is unavailable",
                "ipvs_observation_unavailable",
                "verify",
            )
        return []
    if len(raw) > 1024 * 1024:
        if strict:
            raise LvsControlError(
                "IPVS runtime state exceeds the safe observation limit",
                "ipvs_observation_unavailable",
                "verify",
            )
        return []
    services: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        matched = re.match(
            r"^(TCP|UDP|SCTP)\s+(\[[^\]]+\]|[0-9A-Fa-f]{8}|[0-9A-Fa-f]{32}):([0-9A-Fa-f]{4})\s+(\S+)",
            line,
        )
        if matched:
            address = _decode_ipvs_address(matched.group(2))
            if address is None:
                current = None
                continue
            current = {
                "listener": {
                    "protocol": matched.group(1),
                    "address": address,
                    "port": int(matched.group(3), 16),
                },
                "scheduler": matched.group(4).lower(),
                "members": [],
            }
            services.append(current)
            continue
        destination = re.match(
            r"^\s*->\s+(\[[^\]]+\]|[0-9A-Fa-f]{8}|[0-9A-Fa-f]{32}):([0-9A-Fa-f]{4})\s+(\S+)\s+(\d+)",
            line,
        )
        if destination and current is not None:
            address = _decode_ipvs_address(destination.group(1))
            if address is not None:
                current["members"].append({
                    "address": address,
                    "port": int(destination.group(2), 16),
                    "forwarding": destination.group(3),
                    "weight": int(destination.group(4)),
                })
    return services


def _target_runtime_snapshot(
    listener: Dict[str, Any], runtime: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Return stable traffic fields for one Virtual Service."""
    key = listener_key(listener)
    matched: List[Dict[str, Any]] = []
    for item in runtime:
        if not isinstance(item, dict) or listener_key(item.get("listener", {})) != key:
            continue
        members = [
            {
                "address": member.get("address"),
                "port": member.get("port"),
                "forwarding": str(member.get("forwarding", "")).lower(),
                "weight": member.get("weight"),
            }
            for member in item.get("members", [])
            if isinstance(member, dict)
        ]
        members.sort(key=lambda member: (str(member["address"]), int(member["port"] or 0)))
        matched.append({
            "listener": item.get("listener"),
            "scheduler": str(item.get("scheduler", "")).lower(),
            "members": members,
        })
    matched.sort(key=_canonical_json)
    return matched


def _runtime_matches(intent: Dict[str, Any], runtime: Sequence[Dict[str, Any]]) -> bool:
    target = intent["target"]
    key = listener_key(target)
    matched = [item for item in runtime if listener_key(item.get("listener", {})) == key]
    if intent["kind"] == "delete_service":
        return not matched
    if len(matched) != 1:
        return False
    desired = intent["service"]
    current = matched[0]
    if current.get("scheduler") != desired.get("scheduler"):
        return False
    runtime_members = {
        (item.get("address"), item.get("port")): item
        for item in current.get("members", [])
        if isinstance(item, dict)
    }
    desired_members = {
        (member["address"], member["port"]): member
        for member in desired.get("members", [])
    }
    # A removed destination that remains in the kernel is still traffic state.
    # Do not report convergence merely because every desired member exists.
    if any(key_tuple not in desired_members for key_tuple in runtime_members):
        return False
    forwarding_aliases = {
        "DR": {"route", "dr"},
        "NAT": {"masq", "nat"},
        "TUN": {"tunnel", "tun"},
    }
    expected_forwarding = forwarding_aliases.get(str(desired.get("forwarding", "")).upper(), set())
    for member in desired.get("members", []):
        key_tuple = (member["address"], member["port"])
        if member["enabled"]:
            observed = runtime_members.get(key_tuple)
            # Keepalived may legitimately suppress a monitored destination while
            # it is unhealthy.  Publication convergence proves the listener and
            # every currently active destination; pool health is a separate fact.
            if observed is None and member.get("monitor") is not None:
                continue
            if observed is None:
                return False
            observed_weight = int(observed.get("weight", -1))
            if observed_weight != member["weight"]:
                if member.get("monitor") is not None and observed_weight == 0:
                    continue
                return False
            if str(observed.get("forwarding", "")).lower() not in expected_forwarding:
                return False
        elif key_tuple in runtime_members and int(runtime_members[key_tuple].get("weight", 0)) > 0:
            return False
    return True


class LvsControlModule:
    """Deep module that owns LVS inventory, validation, publication and recovery."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.main_config = Path(str(settings.keepalived_config)).resolve()
        configured_managed = getattr(settings, "lvs_managed_file", None)
        self.managed_file = Path(
            str(configured_managed or self.main_config.parent / "nginx-manager.d" / "50-lvs-managed.conf")
        ).resolve()
        self.state_dir = Path(str(settings.helper_state_dir)) / "lvs-transactions"
        self.maximum_file_bytes = int(getattr(settings, "max_file_bytes", 4 * 1024 * 1024))
        if not SAFE_UNIT_RE.fullmatch(str(settings.keepalived_service)):
            raise LvsControlError("invalid Keepalived service unit", "path_rejected", "precheck")
        root = self.main_config.parent
        if not _resolved_inside(self.managed_file, root):
            raise LvsControlError("managed LVS file is outside Keepalived directory", "path_rejected", "precheck")

    def _binary(self) -> str:
        configured = getattr(self.settings, "keepalived_binary", None)
        candidates = [configured, shutil.which("keepalived"), "/usr/sbin/keepalived", "/sbin/keepalived"]
        for candidate in candidates:
            if candidate and os.path.isabs(candidate) and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return str(Path(candidate).resolve())
        raise LvsControlError(
            "configured Keepalived binary is unavailable",
            "keepalived_validation_unavailable",
            "precheck",
        )

    def _validation_flag(self, binary: str) -> str:
        try:
            completed = subprocess.run(
                [binary, "--help"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=min(float(getattr(self.settings, "command_timeout", 30)), 5.0),
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise LvsControlError(
                "cannot inspect Keepalived validation support",
                "keepalived_validation_unavailable",
                "precheck",
            )
        help_text = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")[-32768:]
        if "--config-test" in help_text:
            return "--config-test"
        if re.search(r"(?:^|[\s,])-t(?:[\s,]|$)", help_text, re.MULTILINE):
            return "-t"
        raise LvsControlError(
            "configured Keepalived cannot validate a candidate configuration",
            "keepalived_validation_unavailable",
            "precheck",
        )

    def _validate(self) -> None:
        binary = self._binary()
        flag = self._validation_flag(binary)
        try:
            completed = subprocess.run(
                [binary, "-f", str(self.main_config), flag],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=float(getattr(self.settings, "command_timeout", 30)),
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise LvsControlError("Keepalived validation timed out", "command_timeout", "validate")
        except OSError:
            raise LvsControlError(
                "cannot execute Keepalived validation",
                "keepalived_validation_unavailable",
                "validate",
            )
        if completed.returncode != 0:
            raise LvsControlError(
                "Keepalived configuration validation failed",
                "keepalived_config_test_failed",
                "validate",
            )

    def _reload(self) -> None:
        systemctl = shutil.which("systemctl") or "/bin/systemctl"
        unit = str(self.settings.keepalived_service)
        try:
            completed = subprocess.run(
                [systemctl, "reload", unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=float(getattr(self.settings, "command_timeout", 30)),
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise LvsControlError("Keepalived reload timed out", "command_timeout", "reload", reload_attempted=True)
        except OSError:
            raise LvsControlError(
                "cannot reload Keepalived service", "keepalived_reload_failed", "reload", reload_attempted=True
            )
        if completed.returncode != 0:
            raise LvsControlError(
                "Keepalived service reload failed", "keepalived_reload_failed", "reload", reload_attempted=True
            )
        try:
            active = subprocess.run(
                [systemctl, "is-active", "--quiet", unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise LvsControlError(
                "cannot verify Keepalived service state", "keepalived_reload_failed", "reload", reload_attempted=True
            )
        if active.returncode != 0:
            raise LvsControlError(
                "Keepalived service is not active after reload",
                "keepalived_reload_failed",
                "reload",
                reload_attempted=True,
            )

    def _service_active(self) -> bool:
        systemctl = shutil.which("systemctl") or "/bin/systemctl"
        try:
            completed = subprocess.run(
                [systemctl, "is-active", "--quiet", str(self.settings.keepalived_service)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LvsControlError(
                "cannot inspect the Keepalived service state",
                "lvs_inventory_incomplete",
                "precheck",
            ) from exc
        return completed.returncode == 0

    def _local_ip_addresses(self) -> set:
        ip_binary = shutil.which("ip")
        if not ip_binary:
            for candidate in ("/usr/sbin/ip", "/sbin/ip"):
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    ip_binary = candidate
                    break
        if not ip_binary:
            raise LvsControlError(
                "cannot inspect local IP addresses",
                "lvs_inventory_incomplete",
                "precheck",
            )
        try:
            completed = subprocess.run(
                [ip_binary, "-o", "address", "show"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LvsControlError(
                "cannot inspect local IP addresses",
                "lvs_inventory_incomplete",
                "precheck",
            ) from exc
        if completed.returncode != 0:
            raise LvsControlError(
                "cannot inspect local IP addresses",
                "lvs_inventory_incomplete",
                "precheck",
            )
        addresses = set()
        for matched in re.finditer(
            r"\sinet6?\s+([^\s/]+)(?:/\d+)?",
            completed.stdout.decode("utf-8", errors="replace")[-1024 * 1024:],
        ):
            try:
                addresses.add(str(ipaddress.ip_address(matched.group(1).split("%", 1)[0])))
            except ValueError:
                continue
        return addresses

    def _assert_expected_ha(self, payload: Dict[str, Any], stage: str = "precheck") -> None:
        expected_role = payload.get("expected_role")
        expected_vip = payload.get("expected_vip")
        if expected_role is None and expected_vip is None:
            return
        if expected_role is not None:
            if not isinstance(expected_role, str) or expected_role.upper() not in {"MASTER", "BACKUP"}:
                raise LvsControlError("expected_role is invalid", "invalid_lvs_intent", stage)
            expected_role = expected_role.upper()
        try:
            configured_vip = str(ipaddress.ip_address(str(self.settings.keepalived_vip)))
            normalized_vip = configured_vip if expected_vip is None else str(ipaddress.ip_address(str(expected_vip)))
        except ValueError as exc:
            raise LvsControlError("expected_vip is invalid", "invalid_lvs_intent", stage) from exc
        if normalized_vip != configured_vip:
            raise LvsControlError(
                "Keepalived VIP changed after planning",
                "concurrent_change",
                stage,
            )
        if expected_role is None:
            return
        actual_role = (
            "FAULT"
            if not self._service_active()
            else ("MASTER" if normalized_vip in self._local_ip_addresses() else "BACKUP")
        )
        if actual_role != expected_role:
            raise LvsControlError(
                "Keepalived role changed after planning",
                "concurrent_change",
                stage,
            )

    def _verify_restored_runtime(self, manifest: Dict[str, Any], stage: str) -> None:
        target = manifest.get("runtime_target")
        expected = manifest.get("pre_runtime_services")
        if not isinstance(target, dict) or not isinstance(expected, list):
            raise LvsControlError(
                "the previous IPVS runtime snapshot is unavailable",
                "rollback_failed",
                stage,
            )
        try:
            target = normalize_listener(target)
        except LvsControlError as exc:
            raise LvsControlError(
                "the previous IPVS runtime snapshot is invalid",
                "rollback_failed",
                stage,
            ) from exc
        deadline = time.monotonic() + min(
            15.0,
            max(3.0, float(getattr(self.settings, "command_timeout", 30))),
        )
        while time.monotonic() < deadline:
            try:
                current = _target_runtime_snapshot(target, _read_ipvs_services(strict=True))
            except LvsControlError as exc:
                raise LvsControlError(
                    "the restored IPVS runtime could not be observed",
                    "rollback_failed",
                    stage,
                ) from exc
            if _canonical_json(current) == _canonical_json(expected):
                return
            time.sleep(0.5)
        raise LvsControlError(
            "the previous IPVS runtime was not restored",
            "rollback_failed",
            stage,
        )

    def _graph(self) -> List[Tuple[Path, bytes]]:
        return read_config_graph(self.main_config, self.maximum_file_bytes)

    @staticmethod
    def _snapshot_expectations(
        graph: Sequence[Tuple[Path, bytes]], candidates: Dict[Path, bytes]
    ) -> Dict[Path, Optional[str]]:
        graph_data = {path: data for path, data in graph}
        return {
            path: (_sha256_bytes(graph_data[path]) if path in graph_data else None)
            for path in candidates
        }

    def observe(self) -> Dict[str, Any]:
        graph = self._graph()
        services: List[Dict[str, Any]] = []
        duplicate_keys = set()
        seen_keys = set()
        for path, data in graph:
            for block in _virtual_service_blocks(path, data, self.managed_file):
                service = block["service"]
                key = service["id"]
                if key in seen_keys:
                    duplicate_keys.add(key)
                seen_keys.add(key)
                services.append(service)
                if len(services) >= MAX_SERVICES:
                    break
        for service in services:
            if service["id"] in duplicate_keys:
                service["editable"] = False
                service["unsupported_directives"] = sorted(set(
                    list(service.get("unsupported_directives", [])) + ["duplicate_virtual_service"]
                ))
        runtime = _read_ipvs_services()
        return {
            "schema_version": SCHEMA_VERSION,
            "management_enabled": bool(getattr(self.settings, "lvs_management_enabled", False)),
            "config_hash": graph_hash(graph, self.main_config.parent),
            "managed_file": str(self.managed_file),
            "services": services[:MAX_SERVICES],
            "runtime_services": runtime[:MAX_SERVICES],
            "service_count": len(services),
            "runtime_service_count": len(runtime),
            "partial": len(services) >= MAX_SERVICES,
        }

    def _find_blocks(
        self, graph: Sequence[Tuple[Path, bytes]], target: Dict[str, Any]
    ) -> List[Tuple[Path, bytes, Dict[str, Any]]]:
        key = listener_key(target)
        found: List[Tuple[Path, bytes, Dict[str, Any]]] = []
        for path, data in graph:
            for block in _virtual_service_blocks(path, data, self.managed_file):
                if block["service"]["id"] == key:
                    found.append((path, data, block))
        return found

    @staticmethod
    def _without_block(data: bytes, block: Dict[str, Any]) -> bytes:
        """Remove one parsed Virtual Service and its trailing line break."""
        text = data.decode("utf-8", errors="replace")
        start = int(block["start"])
        end = int(block["end"])
        while end < len(text) and text[end] in " \t":
            end += 1
        if end < len(text) and text[end] == "\r":
            end += 1
        if end < len(text) and text[end] == "\n":
            end += 1
        return (text[:start] + text[end:]).encode("utf-8")

    @staticmethod
    def _append_managed_block(data: bytes, replacement: bytes) -> bytes:
        separator = (
            b""
            if not data or data.endswith(b"\n\n")
            else (b"\n" if data.endswith(b"\n") else b"\n\n")
        )
        return data + separator + replacement

    def _candidate_files(
        self,
        graph: Sequence[Tuple[Path, bytes]],
        intent: Dict[str, Any],
        adopt_existing: bool = False,
    ) -> Dict[Path, bytes]:
        found = self._find_blocks(graph, intent["target"])
        if len(found) > 1:
            raise LvsControlError(
                "the target Virtual Service is duplicated",
                "duplicate_virtual_service",
                "compile",
            )
        if intent["kind"] == "delete_service":
            if not found:
                raise LvsControlError("the target Virtual Service no longer exists", "concurrent_change", "compile")
            path, data, block = found[0]
            if path != self.managed_file:
                raise LvsControlError(
                    "an existing Virtual Service must be explicitly taken over before deletion",
                    "lvs_takeover_required",
                    "compile",
                )
            if adopt_existing:
                raise LvsControlError(
                    "the Virtual Service is already managed",
                    "invalid_lvs_intent",
                    "compile",
                )
            return {path: self._without_block(data, block)}

        assert intent["service"] is not None
        replacement = render_service(intent["service"])
        desired_key = listener_key(intent["service"]["listener"])
        collisions = self._find_blocks(graph, intent["service"]["listener"])
        target_key = listener_key(intent["target"])
        if desired_key != target_key and collisions:
            raise LvsControlError(
                "another Virtual Service already uses the requested listener",
                "duplicate_virtual_service",
                "compile",
            )
        if found:
            path, data, block = found[0]
            service = block["service"]
            if not service.get("editable", False):
                raise LvsControlError(
                    "the existing Virtual Service uses directives that are read-only",
                    "lvs_config_unsupported",
                    "compile",
                )
            if path != self.managed_file:
                if not adopt_existing:
                    raise LvsControlError(
                        "explicit takeover is required for an existing Virtual Service",
                        "lvs_takeover_required",
                        "compile",
                    )
                managed_data = next(
                    (item_data for item_path, item_data in graph if item_path == self.managed_file),
                    b"",
                )
                return {
                    path: self._without_block(data, block),
                    self.managed_file: self._append_managed_block(managed_data, replacement),
                }
            if adopt_existing:
                raise LvsControlError(
                    "the Virtual Service is already managed",
                    "invalid_lvs_intent",
                    "compile",
                )
            text = data.decode("utf-8", errors="replace")
            return {path: text[:int(block["start"])].encode("utf-8") + replacement + text[int(block["end"]):].encode("utf-8")}

        managed_data = b""
        for path, data in graph:
            if path == self.managed_file:
                managed_data = data
                break
        if adopt_existing:
            raise LvsControlError(
                "the existing Virtual Service disappeared before takeover",
                "concurrent_change",
                "compile",
            )
        return {self.managed_file: self._append_managed_block(managed_data, replacement)}

    def _manifest_path(self, transaction_id: str) -> Path:
        return self.state_dir / transaction_id / "manifest.json"

    @staticmethod
    def _transaction_id(job_id: str) -> str:
        return hashlib.sha256((job_id + "\0lvs_apply_v1").encode("utf-8")).hexdigest()

    def _secure_state_dir(self) -> None:
        if self.state_dir.is_symlink():
            raise LvsControlError(
                "LVS transaction state must not be a symbolic link", "rollback_failed", "recovery"
            )
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        status = self.state_dir.stat()
        uid, _gid = _current_owner()
        if not stat.S_ISDIR(status.st_mode) or (
            os.name == "posix" and (status.st_uid != uid or stat.S_IMODE(status.st_mode) & 0o022)
        ):
            raise LvsControlError(
                "LVS transaction state is not securely owned", "rollback_failed", "recovery"
            )

    def _validate_transaction_dir(self, transaction_dir: Path) -> str:
        try:
            if transaction_dir.is_symlink():
                raise OSError("symbolic link")
            parent = transaction_dir.parent.resolve(strict=True)
            resolved = transaction_dir.resolve(strict=True)
            status = transaction_dir.stat()
        except OSError as exc:
            raise LvsControlError(
                "LVS transaction directory is invalid", "rollback_failed", "recovery"
            ) from exc
        transaction_id = transaction_dir.name
        uid, _gid = _current_owner()
        if (
            parent != self.state_dir.resolve(strict=True)
            or not SHA256_RE.fullmatch(transaction_id)
            or not stat.S_ISDIR(status.st_mode)
            or (os.name == "posix" and (status.st_uid != uid or stat.S_IMODE(status.st_mode) & 0o022))
        ):
            raise LvsControlError(
                "LVS transaction directory is invalid", "rollback_failed", "recovery"
            )
        return transaction_id

    def _allowed_transaction_paths(self) -> set:
        allowed = {self.main_config, self.managed_file}
        try:
            allowed.update(path for path, _data in self._graph())
        except LvsControlError as exc:
            raise LvsControlError(
                "Keepalived configuration paths cannot be verified for recovery",
                "rollback_failed",
                "recovery",
            ) from exc
        return allowed

    def _read_target_snapshot(self, path: Path, stage: str) -> Tuple[bytes, Any]:
        try:
            status_before = path.lstat()
        except FileNotFoundError:
            return b"", None
        except OSError as exc:
            raise LvsControlError(
                "Keepalived configuration cannot be checked safely", "path_rejected", stage
            ) from exc
        if (
            stat.S_ISLNK(status_before.st_mode)
            or not stat.S_ISREG(status_before.st_mode)
            or status_before.st_nlink != 1
        ):
            raise LvsControlError(
                "LVS configuration path must be a single regular file", "path_rejected", stage
            )
        try:
            with path.open("rb") as handle:
                data = handle.read(self.maximum_file_bytes + 1)
                opened_status = os.fstat(handle.fileno())
            status_after = path.lstat()
        except OSError as exc:
            raise LvsControlError(
                "Keepalived configuration changed while it was checked", "concurrent_change", stage
            ) from exc
        if len(data) > self.maximum_file_bytes:
            raise LvsControlError("Keepalived configuration is too large", "lvs_config_unsupported", stage)
        identity_before = (
            status_before.st_dev, status_before.st_ino, status_before.st_size,
            getattr(status_before, "st_mtime_ns", int(status_before.st_mtime * 1000000000)),
            getattr(status_before, "st_ctime_ns", int(status_before.st_ctime * 1000000000)),
        )
        identity_after = (
            status_after.st_dev, status_after.st_ino, status_after.st_size,
            getattr(status_after, "st_mtime_ns", int(status_after.st_mtime * 1000000000)),
            getattr(status_after, "st_ctime_ns", int(status_after.st_ctime * 1000000000)),
        )
        opened_identity = (
            opened_status.st_dev, opened_status.st_ino, opened_status.st_size,
            getattr(opened_status, "st_mtime_ns", int(opened_status.st_mtime * 1000000000)),
            getattr(opened_status, "st_ctime_ns", int(opened_status.st_ctime * 1000000000)),
        )
        if (
            not stat.S_ISREG(status_after.st_mode)
            or status_after.st_nlink != 1
            or identity_before != identity_after
            or opened_identity != identity_before
        ):
            raise LvsControlError(
                "Keepalived configuration changed while it was checked", "concurrent_change", stage
            )
        return data, status_after

    def _read_expected_target(self, path: Path, expected_sha256: Optional[str], stage: str) -> Tuple[bytes, Any]:
        data, status = self._read_target_snapshot(path, stage)
        if (
            (status is None and expected_sha256 is not None)
            or (status is not None and expected_sha256 is None)
            or (status is not None and _sha256_bytes(data) != expected_sha256)
        ):
            raise LvsControlError(
                "Keepalived configuration changed after preview", "concurrent_change", stage
            )
        return data, status

    def _validate_artifact(
        self, transaction_dir: Path, name: Any, expected_name: str, expected_sha256: str
    ) -> Path:
        if not isinstance(name, str) or name != expected_name:
            raise LvsControlError("LVS transaction artifact name is invalid", "rollback_failed", "recovery")
        artifact = transaction_dir / name
        try:
            if artifact.is_symlink():
                raise OSError("symbolic link")
            resolved = artifact.resolve(strict=True)
            with artifact.open("rb") as handle:
                data = handle.read(self.maximum_file_bytes + 1)
                status = os.fstat(handle.fileno())
            status_after = artifact.lstat()
        except OSError as exc:
            raise LvsControlError(
                "LVS transaction recovery artifact is unavailable", "rollback_failed", "recovery"
            ) from exc
        uid, _gid = _current_owner()
        if (
            resolved.parent != transaction_dir.resolve(strict=True)
            or not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or not stat.S_ISREG(status_after.st_mode)
            or status_after.st_dev != status.st_dev
            or status_after.st_ino != status.st_ino
            or (os.name == "posix" and (status.st_uid != uid or stat.S_IMODE(status.st_mode) & 0o022))
            or len(data) > self.maximum_file_bytes
            or _sha256_bytes(data) != expected_sha256
        ):
            raise LvsControlError(
                "LVS transaction recovery artifact failed integrity checks", "rollback_failed", "recovery"
            )
        return artifact

    def _load_manifest(self, transaction_dir: Path) -> Dict[str, Any]:
        transaction_id = self._validate_transaction_dir(transaction_dir)
        manifest_path = transaction_dir / "manifest.json"
        try:
            if manifest_path.is_symlink():
                raise OSError("symbolic link")
            status = manifest_path.stat()
            uid, _gid = _current_owner()
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
                or (os.name == "posix" and (status.st_uid != uid or stat.S_IMODE(status.st_mode) & 0o022))
                or status.st_size > MAX_TRANSACTION_MANIFEST_BYTES
            ):
                raise OSError("unsafe manifest")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LvsControlError(
                "LVS transaction manifest is corrupt", "rollback_failed", "recovery"
            ) from exc
        required = {
            "schema_version", "transaction_id", "phase", "files", "reload_attempted",
            "runtime_target", "pre_runtime_services",
        }
        allowed = required | {"result", "runtime_verification"}
        if (
            not isinstance(manifest, dict)
            or not required.issubset(manifest)
            or not set(manifest).issubset(allowed)
            or manifest.get("schema_version") != 1
            or manifest.get("transaction_id") != transaction_id
            or manifest.get("phase") not in TRANSACTION_PHASES
            or not isinstance(manifest.get("reload_attempted"), bool)
        ):
            raise LvsControlError(
                "LVS transaction manifest schema is invalid", "rollback_failed", "recovery"
            )
        try:
            normalize_listener(manifest.get("runtime_target"))
        except LvsControlError as exc:
            raise LvsControlError(
                "LVS transaction runtime target is invalid", "rollback_failed", "recovery"
            ) from exc
        runtime = manifest.get("pre_runtime_services")
        files = manifest.get("files")
        if (
            not isinstance(runtime, list)
            or len(runtime) > MAX_SERVICES
            or not isinstance(files, list)
            or not (1 <= len(files) <= MAX_TRANSACTION_FILES)
        ):
            raise LvsControlError("LVS transaction manifest is invalid", "rollback_failed", "recovery")
        allowed_paths = self._allowed_transaction_paths()
        expected_children = {"manifest.json"}
        seen_paths = set()
        for index, item in enumerate(files):
            if not isinstance(item, dict) or set(item) != {
                "path", "existed", "mode", "uid", "gid", "backup", "candidate",
                "old_sha256", "new_sha256",
            }:
                raise LvsControlError("LVS transaction file entry is invalid", "rollback_failed", "recovery")
            try:
                path = Path(str(item["path"]))
                canonical = Path(os.path.abspath(str(path)))
            except (OSError, ValueError) as exc:
                raise LvsControlError("LVS transaction target is invalid", "rollback_failed", "recovery") from exc
            if not path.is_absolute() or path != canonical or path not in allowed_paths or path in seen_paths:
                raise LvsControlError("LVS transaction target is not allowed", "rollback_failed", "recovery")
            seen_paths.add(path)
            existed = item.get("existed")
            mode = item.get("mode")
            uid_value = item.get("uid")
            gid_value = item.get("gid")
            old_sha = item.get("old_sha256")
            new_sha = item.get("new_sha256")
            if (
                not isinstance(existed, bool)
                or not isinstance(mode, int) or isinstance(mode, bool) or not (0 <= mode <= 0o7777)
                or not isinstance(uid_value, int) or isinstance(uid_value, bool) or not (0 <= uid_value < 2 ** 32 - 1)
                or not isinstance(gid_value, int) or isinstance(gid_value, bool) or not (0 <= gid_value < 2 ** 32 - 1)
                or not isinstance(old_sha, str) or not SHA256_RE.fullmatch(old_sha)
                or not isinstance(new_sha, str) or not SHA256_RE.fullmatch(new_sha)
                or (not existed and old_sha != _sha256_bytes(b""))
            ):
                raise LvsControlError("LVS transaction metadata is invalid", "rollback_failed", "recovery")
            backup_name = "{:03d}.bak".format(index)
            candidate_name = "{:03d}.new".format(index)
            self._validate_artifact(transaction_dir, item.get("backup"), backup_name, old_sha)
            self._validate_artifact(transaction_dir, item.get("candidate"), candidate_name, new_sha)
            expected_children.update({backup_name, candidate_name})
        try:
            children = {item.name for item in transaction_dir.iterdir()}
        except OSError as exc:
            raise LvsControlError("LVS transaction directory is unavailable", "rollback_failed", "recovery") from exc
        if children != expected_children:
            raise LvsControlError("LVS transaction directory contains unexpected files", "rollback_failed", "recovery")
        return manifest

    def _assert_manifest_targets_unchanged(self, manifest: Dict[str, Any], stage: str) -> None:
        for item in manifest["files"]:
            expected = str(item["old_sha256"]) if item["existed"] else None
            self._read_expected_target(Path(str(item["path"])), expected, stage)

    def _assert_recovery_targets_known(self, manifest: Dict[str, Any]) -> None:
        for item in manifest["files"]:
            data, status = self._read_target_snapshot(Path(str(item["path"])), "recovery")
            if status is None:
                if item["existed"]:
                    raise LvsControlError(
                        "LVS recovery target has an unknown state", "rollback_failed", "recovery"
                    )
                continue
            current_sha = _sha256_bytes(data)
            if current_sha not in {item["old_sha256"], item["new_sha256"]}:
                raise LvsControlError(
                    "LVS recovery target was edited after interruption", "rollback_failed", "recovery"
                )

    def _write_manifest(self, transaction_dir: Path, value: Dict[str, Any]) -> None:
        self._validate_transaction_dir(transaction_dir)
        raw = (_canonical_json(value) + "\n").encode("utf-8")
        if len(raw) > MAX_TRANSACTION_MANIFEST_BYTES:
            raise LvsControlError("LVS transaction manifest is too large", "rollback_failed", "recovery")
        uid, gid = _current_owner()
        _atomic_bytes(transaction_dir / "manifest.json", raw, 0o600, uid, gid)

    def _prepare_transaction(
        self,
        job_id: str,
        candidates: Dict[Path, bytes],
        snapshot_expectations: Dict[Path, Optional[str]],
        runtime_target: Dict[str, Any],
        pre_runtime_services: List[Dict[str, Any]],
    ) -> Tuple[Path, Dict[str, Any]]:
        if set(candidates) != set(snapshot_expectations) or not (1 <= len(candidates) <= MAX_TRANSACTION_FILES):
            raise LvsControlError("LVS transaction snapshot is invalid", "concurrent_change", "prepare")
        snapshots: Dict[Path, Tuple[bytes, Any]] = {}
        for path in sorted(candidates, key=str):
            snapshots[path] = self._read_expected_target(path, snapshot_expectations[path], "prepare")
        self._secure_state_dir()
        transaction_id = self._transaction_id(job_id)
        transaction_dir = self.state_dir / transaction_id
        try:
            transaction_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise LvsControlError(
                "an LVS transaction with this job id already exists", "concurrent_change", "prepare"
            ) from exc
        files: List[Dict[str, Any]] = []
        for index, path in enumerate(sorted(candidates, key=str)):
            old_data, status = snapshots[path]
            existed = status is not None
            if status is not None:
                mode = stat.S_IMODE(status.st_mode)
                uid = status.st_uid
                gid = status.st_gid
            else:
                old_data = b""
                mode = 0o600
                main_status = self.main_config.stat()
                uid = main_status.st_uid
                gid = main_status.st_gid
            backup_name = "{:03d}.bak".format(index)
            candidate_name = "{:03d}.new".format(index)
            current_uid, current_gid = _current_owner()
            _atomic_bytes(transaction_dir / backup_name, old_data, 0o600, current_uid, current_gid)
            _atomic_bytes(transaction_dir / candidate_name, candidates[path], 0o600, current_uid, current_gid)
            files.append({
                "path": str(path),
                "existed": existed,
                "mode": mode,
                "uid": uid,
                "gid": gid,
                "backup": backup_name,
                "candidate": candidate_name,
                "old_sha256": _sha256_bytes(old_data),
                "new_sha256": _sha256_bytes(candidates[path]),
            })
        manifest = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "phase": "prepared",
            "files": files,
            "reload_attempted": False,
            "runtime_target": runtime_target,
            "pre_runtime_services": pre_runtime_services,
        }
        self._write_manifest(transaction_dir, manifest)
        return transaction_dir, manifest

    def committed_result(self, payload: Any, job_id: str) -> Optional[Dict[str, Any]]:
        """Reconstruct a result committed immediately before a helper crash.

        The LVS transaction manifest is written before the generic JobStore.  A
        power loss in that small window must not turn an already-applied change
        into an ambiguous failure or replay the reload.  Only a result bound to
        the same job, intent, plan digest and current configuration graph is
        accepted here.
        """
        if not isinstance(payload, dict):
            return None
        try:
            _only_keys(
                payload,
                {
                    "intent", "expected_config_hash", "plan_digest", "expected_role",
                    "expected_vip", "adopt_existing",
                },
                "LVS payload",
            )
            intent = normalize_intent(payload.get("intent"))
        except LvsControlError:
            return None
        plan_digest = payload.get("plan_digest")
        if not isinstance(plan_digest, str) or not SHA256_RE.fullmatch(plan_digest):
            return None
        transaction_id = self._transaction_id(job_id)
        transaction_dir = self.state_dir / transaction_id
        try:
            manifest = self._load_manifest(transaction_dir)
        except LvsControlError:
            return None
        if (
            manifest.get("phase") != "committed"
        ):
            return None
        result = manifest.get("result")
        if not isinstance(result, dict) or set(result) != {
            "applied", "rolled_back", "plan_digest", "config_hash",
            "target", "intent_kind", "service_count",
        }:
            return None
        try:
            target = normalize_listener(result.get("target"))
        except LvsControlError:
            return None
        if (
            result.get("applied") is not True
            or result.get("rolled_back") is not False
            or result.get("plan_digest") != plan_digest
            or result.get("intent_kind") != intent["kind"]
            or listener_key(target) != listener_key(intent["target"])
            or not isinstance(result.get("config_hash"), str)
            or not SHA256_RE.fullmatch(str(result.get("config_hash")))
            or not isinstance(result.get("service_count"), int)
            or isinstance(result.get("service_count"), bool)
            or not (0 <= int(result["service_count"]) <= MAX_SERVICES)
        ):
            return None
        try:
            current_hash = graph_hash(self._graph(), self.main_config.parent)
        except LvsControlError:
            return None
        if current_hash != result["config_hash"]:
            return None
        return dict(result)

    def _replace_from_transaction(self, transaction_dir: Path, manifest: Dict[str, Any], candidate: bool) -> None:
        for item in manifest.get("files", []):
            path = Path(str(item["path"]))
            source = transaction_dir / str(item["candidate"] if candidate else item["backup"])
            if candidate:
                data = source.read_bytes()
                _atomic_bytes(path, data, int(item["mode"]), int(item["uid"]), int(item["gid"]))
            elif bool(item.get("existed")):
                data = source.read_bytes()
                _atomic_bytes(path, data, int(item["mode"]), int(item["uid"]), int(item["gid"]))
            else:
                try:
                    path.unlink()
                    _fsync_directory(path.parent)
                except FileNotFoundError:
                    pass

    def _rollback(self, transaction_dir: Path, manifest: Dict[str, Any]) -> None:
        manifest["phase"] = "rolling_back"
        self._write_manifest(transaction_dir, manifest)
        manifest = self._load_manifest(transaction_dir)
        self._assert_recovery_targets_known(manifest)
        self._replace_from_transaction(transaction_dir, manifest, candidate=False)
        self._validate()
        self._reload()
        self._verify_restored_runtime(manifest, "rollback")
        manifest["phase"] = "rolled_back"
        self._write_manifest(transaction_dir, manifest)

    def recover(self) -> int:
        restored = 0
        if not self.state_dir.exists():
            return restored
        self._secure_state_dir()
        for transaction_dir in sorted(self.state_dir.iterdir()):
            if transaction_dir.is_symlink() or not transaction_dir.is_dir() or not SHA256_RE.fullmatch(transaction_dir.name):
                raise LvsControlError(
                    "LVS transaction state contains an invalid entry", "rollback_failed", "recovery"
                )
            manifest = self._load_manifest(transaction_dir)
            phase = str(manifest.get("phase", ""))
            if phase in TERMINAL_TRANSACTION_PHASES:
                continue
            try:
                self._assert_recovery_targets_known(manifest)
                self._replace_from_transaction(transaction_dir, manifest, candidate=False)
                self._validate()
                if manifest.get("reload_attempted"):
                    if self._service_active():
                        self._reload()
                        self._verify_restored_runtime(manifest, "recovery")
                    else:
                        manifest["runtime_verification"] = "deferred_service_inactive"
                manifest["phase"] = "recovered"
                self._write_manifest(transaction_dir, manifest)
                restored += 1
            except Exception:
                manifest["phase"] = "recovery_failed"
                try:
                    self._write_manifest(transaction_dir, manifest)
                except Exception:
                    pass
                raise LvsControlError(
                    "an interrupted LVS publication could not be recovered",
                    "rollback_failed",
                    "recovery",
                )
        self._cleanup_terminal_transactions()
        return restored

    def _cleanup_terminal_transactions(self) -> None:
        terminal: List[Tuple[float, Path]] = []
        for transaction_dir in self.state_dir.iterdir():
            manifest = self._load_manifest(transaction_dir)
            if manifest["phase"] in TERMINAL_TRANSACTION_PHASES:
                terminal.append((transaction_dir.stat().st_mtime, transaction_dir))
        for _mtime, transaction_dir in sorted(terminal, reverse=True)[MAX_RETAINED_TERMINAL_TRANSACTIONS:]:
            shutil.rmtree(str(transaction_dir))
            _fsync_directory(self.state_dir)

    def apply(self, payload: Any, job_id: str) -> Dict[str, Any]:
        if not bool(getattr(self.settings, "lvs_management_enabled", False)):
            raise LvsControlError("LVS management is disabled", "lvs_profile_required", "precheck")
        if not isinstance(payload, dict):
            raise LvsControlError("LVS payload must be an object", "invalid_lvs_intent", "precheck")
        _only_keys(
            payload,
            {
                "intent", "expected_config_hash", "plan_digest", "expected_role",
                "expected_vip", "adopt_existing",
            },
            "LVS payload",
        )
        expected_hash = payload.get("expected_config_hash")
        plan_digest = payload.get("plan_digest")
        if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
            raise LvsControlError("expected_config_hash is invalid", "invalid_lvs_intent", "precheck")
        if not isinstance(plan_digest, str) or not SHA256_RE.fullmatch(plan_digest):
            raise LvsControlError("plan_digest is invalid", "invalid_lvs_intent", "precheck")
        adopt_existing = payload.get("adopt_existing", False)
        if not isinstance(adopt_existing, bool):
            raise LvsControlError("adopt_existing is invalid", "invalid_lvs_intent", "precheck")
        intent = normalize_intent(payload.get("intent"))
        self.recover()
        graph = self._graph()
        current_hash = graph_hash(graph, self.main_config.parent)
        if current_hash != expected_hash:
            raise LvsControlError(
                "Keepalived configuration changed after preview",
                "concurrent_change",
                "precheck",
            )
        self._assert_expected_ha(payload)
        runtime_before = _target_runtime_snapshot(
            intent["target"],
            _read_ipvs_services(strict=True),
        )
        candidates = self._candidate_files(graph, intent, adopt_existing=adopt_existing)
        snapshot_expectations = self._snapshot_expectations(graph, candidates)
        transaction_dir, manifest = self._prepare_transaction(
            job_id,
            candidates,
            snapshot_expectations,
            intent["target"],
            runtime_before,
        )
        try:
            if graph_hash(self._graph(), self.main_config.parent) != current_hash:
                raise LvsControlError(
                    "Keepalived configuration changed after preview", "concurrent_change", "prepare"
                )
            self._assert_manifest_targets_unchanged(manifest, "prepare")
            manifest = self._load_manifest(transaction_dir)
        except LvsControlError:
            manifest["phase"] = "aborted"
            self._write_manifest(transaction_dir, manifest)
            raise
        try:
            manifest["phase"] = "replacing"
            self._write_manifest(transaction_dir, manifest)
            self._replace_from_transaction(transaction_dir, manifest, candidate=True)
            manifest["phase"] = "validating"
            self._write_manifest(transaction_dir, manifest)
            self._validate()
            self._assert_expected_ha(payload, "verify")
            manifest["phase"] = "reloading"
            manifest["reload_attempted"] = True
            self._write_manifest(transaction_dir, manifest)
            self._reload()
            manifest["phase"] = "verifying"
            self._write_manifest(transaction_dir, manifest)
            deadline = time.monotonic() + min(15.0, max(3.0, float(getattr(self.settings, "command_timeout", 30))))
            converged = False
            while time.monotonic() < deadline:
                if _runtime_matches(intent, _read_ipvs_services(strict=True)):
                    converged = True
                    break
                time.sleep(0.5)
            if not converged:
                raise LvsControlError(
                    "IPVS runtime did not converge to the published configuration",
                    "ipvs_reconcile_timeout",
                    "verify",
                    reload_attempted=True,
                )
            new_observation = self.observe()
            result = {
                "applied": True,
                "rolled_back": False,
                "plan_digest": plan_digest,
                "config_hash": new_observation["config_hash"],
                "target": intent["target"],
                "intent_kind": intent["kind"],
                "service_count": new_observation["service_count"],
            }
            manifest["result"] = result
            manifest["phase"] = "committed"
            self._write_manifest(transaction_dir, manifest)
            return result
        except LvsControlError as exc:
            try:
                self._rollback(transaction_dir, manifest)
            except Exception:
                raise LvsControlError(
                    "LVS publication failed and automatic rollback could not be verified",
                    "rollback_failed",
                    "rollback",
                    rolled_back=False,
                    reload_attempted=bool(manifest.get("reload_attempted")),
                )
            raise LvsControlError(
                str(exc),
                exc.failure_code,
                exc.failure_stage,
                rolled_back=True,
                reload_attempted=bool(manifest.get("reload_attempted")),
            )
        except Exception:
            try:
                self._rollback(transaction_dir, manifest)
            except Exception:
                raise LvsControlError(
                    "LVS publication failed and automatic rollback could not be verified",
                    "rollback_failed",
                    "rollback",
                    rolled_back=False,
                    reload_attempted=bool(manifest.get("reload_attempted")),
                )
            raise LvsControlError(
                "LVS publication failed and the previous configuration was restored",
                "lvs_operation_failed",
                "apply",
                rolled_back=True,
                reload_attempted=bool(manifest.get("reload_attempted")),
            )
