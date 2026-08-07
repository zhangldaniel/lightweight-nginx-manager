"""Strict, command-free API models for managed LVS configuration."""

from __future__ import annotations

import ipaddress
import re
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Literal


def _canonical_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except (AttributeError, ValueError):
        raise ValueError("invalid IP address")


class LvsListener(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(..., min_length=2, max_length=64)
    port: int = Field(..., ge=1, le=65535)
    protocol: Literal["TCP", "UDP", "SCTP"] = "TCP"

    @field_validator("address")
    def validate_address(cls, value: str) -> str:
        return _canonical_ip(value)


class LvsTcpMonitor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["tcp"] = "tcp"
    connect_timeout: int = Field(3, ge=1, le=300)
    retries: int = Field(3, ge=1, le=20)
    delay_before_retry: int = Field(3, ge=1, le=300)
    connect_port: Optional[int] = Field(None, ge=1, le=65535)


class LvsMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(..., min_length=2, max_length=64)
    port: int = Field(..., ge=1, le=65535)
    weight: int = Field(1, ge=1, le=65535)
    enabled: bool = True
    monitor: Optional[LvsTcpMonitor] = None

    @field_validator("address")
    def validate_address(cls, value: str) -> str:
        return _canonical_ip(value)


class LvsService(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    listener: LvsListener
    scheduler: Literal["rr", "wrr", "lc", "wlc", "lblc", "lblcr", "dh", "sh", "sed", "nq", "mh"] = "wlc"
    forwarding: Literal["DR", "NAT", "TUN"] = "DR"
    delay_loop: int = Field(6, ge=1, le=3600)
    persistence_seconds: Optional[int] = Field(None, ge=1, le=86400)
    members: List[LvsMember] = Field(..., min_length=1, max_length=256)

    @field_validator("name")
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or "#" in value or re.search(r"[\x00-\x1f]", value):
            raise ValueError("invalid service name")
        return value

    @field_validator("members")
    def unique_members(cls, value: List[LvsMember]) -> List[LvsMember]:
        keys = [(item.address, item.port) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate LVS pool member")
        if not any(item.enabled for item in value):
            raise ValueError("at least one LVS pool member must remain enabled")
        return value


class LvsIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["upsert_service", "delete_service"]
    target: LvsListener
    service: Optional[LvsService] = None
    change_note: str = Field("", max_length=500)

    @field_validator("change_note")
    def validate_change_note(cls, value: str) -> str:
        value = value.strip()
        if re.search(r"[\x00-\x08\x0b-\x1f]", value):
            raise ValueError("invalid change note")
        return value

    @model_validator(mode="after")
    def validate_action_target(self) -> "LvsIntent":
        if self.kind == "upsert_service":
            if self.service is None:
                raise ValueError("upsert_service requires service")
            if self.service.listener != self.target:
                raise ValueError(
                    "an existing Virtual Service listener is immutable; create a new service instead"
                )
        elif self.service is not None:
            raise ValueError("delete_service must not include service")
        return self

    def target_listener(self) -> LvsListener:
        return self.target


class LvsPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_ids: List[str] = Field(..., min_length=1, max_length=32)
    intent: LvsIntent
    adopt_existing: bool = False
    ttl_seconds: int = Field(900, ge=30, le=3600)

    @field_validator("node_ids")
    def validate_node_ids(cls, value: List[str]) -> List[str]:
        cleaned = [str(item).strip() for item in value]
        if any(not item or len(item) > 200 for item in cleaned):
            raise ValueError("invalid node id")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("duplicate node id")
        return cleaned


class LvsPlanApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_digest: str = Field(..., min_length=64, max_length=64)
    request_id: str = Field(..., min_length=16, max_length=128)

    @field_validator("plan_digest")
    def validate_digest(cls, value: str) -> str:
        value = value.lower()
        if re.fullmatch(r"[a-f0-9]{64}", value) is None:
            raise ValueError("invalid plan digest")
        return value

    @field_validator("request_id")
    def validate_request_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9._:-]{16,128}", value) is None:
            raise ValueError("invalid request id")
        return value
