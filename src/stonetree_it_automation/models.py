from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Any, Mapping

from .errors import ValidationError


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OperationStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


class StepStatus(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    COMPENSATED = "compensated"


@dataclass(frozen=True, slots=True)
class Employee:
    employee_id: str
    first_name: str
    last_name: str
    email: str
    role: str
    job_title: str
    phone: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Employee":
        return cls(
            employee_id=str(data.get("employee_id", "")).strip(),
            first_name=str(data.get("first_name", "")).strip(),
            last_name=str(data.get("last_name", "")).strip(),
            email=str(data.get("email", "")).strip().lower(),
            role=str(data.get("role", "")).strip().lower(),
            job_title=str(data.get("job_title", "")).strip(),
            phone=str(data.get("phone", "")).strip(),
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("employee_id", self.employee_id),
                ("first_name", self.first_name),
                ("last_name", self.last_name),
                ("email", self.email),
                ("role", self.role),
                ("job_title", self.job_title),
            )
            if not value
        ]
        if missing:
            raise ValidationError(f"Missing required fields: {', '.join(missing)}")
        if not EMAIL_RE.fullmatch(self.email):
            raise ValidationError(f"Invalid email address: {self.email}")


@dataclass(frozen=True, slots=True)
class BitrixUser:
    user_id: str
    email: str
    active: bool


@dataclass(frozen=True, slots=True)
class StepResult:
    name: str
    status: StepStatus
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status.value, "details": self.details}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StepResult":
        return cls(
            name=str(data["name"]),
            status=StepStatus(str(data["status"])),
            details=dict(data.get("details", {})),
        )


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    request_id: str
    operation: str
    employee_id: str
    status: OperationStatus
    steps: tuple[StepResult, ...]
    bitrix_user_id: str | None = None
    asset_tags: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "operation": self.operation,
            "employee_id": self.employee_id,
            "status": self.status.value,
            "bitrix_user_id": self.bitrix_user_id,
            "asset_tags": list(self.asset_tags),
            "steps": [step.to_dict() for step in self.steps],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowResult":
        return cls(
            request_id=str(data["request_id"]),
            operation=str(data["operation"]),
            employee_id=str(data["employee_id"]),
            status=OperationStatus(str(data["status"])),
            bitrix_user_id=(str(data["bitrix_user_id"]) if data.get("bitrix_user_id") else None),
            asset_tags=tuple(str(tag) for tag in data.get("asset_tags", [])),
            steps=tuple(StepResult.from_dict(step) for step in data.get("steps", [])),
            error=(str(data["error"]) if data.get("error") else None),
        )


@dataclass(frozen=True, slots=True)
class AssetReservation:
    all_tags: tuple[str, ...]
    newly_reserved_tags: tuple[str, ...]
