from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .errors import PolicyError


@dataclass(frozen=True, slots=True)
class RolePolicy:
    department_id: int
    group_ids: tuple[int, ...]
    asset_types: tuple[str, ...]


class AccessPolicy:
    def __init__(self, roles: Mapping[str, RolePolicy]):
        self._roles = {name.strip().lower(): policy for name, policy in roles.items()}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AccessPolicy":
        raw_roles = data.get("roles")
        if not isinstance(raw_roles, Mapping) or not raw_roles:
            raise PolicyError("Policy must contain a non-empty 'roles' object")

        roles: dict[str, RolePolicy] = {}
        for role_name, raw_policy in raw_roles.items():
            if not isinstance(raw_policy, Mapping):
                raise PolicyError(f"Policy for role '{role_name}' must be an object")
            try:
                department_id = int(raw_policy["department_id"])
                group_ids = tuple(int(value) for value in raw_policy.get("group_ids", []))
                asset_types = tuple(str(value).strip().lower() for value in raw_policy.get("asset_types", []))
            except (KeyError, TypeError, ValueError) as exc:
                raise PolicyError(f"Invalid policy for role '{role_name}'") from exc
            if department_id <= 0 or any(value <= 0 for value in group_ids):
                raise PolicyError(f"IDs must be positive for role '{role_name}'")
            if any(not value for value in asset_types):
                raise PolicyError(f"Empty asset type for role '{role_name}'")
            roles[str(role_name)] = RolePolicy(department_id, group_ids, asset_types)
        return cls(roles)

    @classmethod
    def from_file(cls, path: str | Path) -> "AccessPolicy":
        with Path(path).open("r", encoding="utf-8") as stream:
            return cls.from_mapping(json.load(stream))

    def resolve(self, role: str) -> RolePolicy:
        normalized_role = role.strip().lower()
        try:
            return self._roles[normalized_role]
        except KeyError as exc:
            available = ", ".join(sorted(self._roles))
            raise PolicyError(f"Unknown role '{role}'. Available roles: {available}") from exc

