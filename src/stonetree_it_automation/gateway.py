from __future__ import annotations

import json
import socket
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .errors import BitrixApiError, ValidationError
from .models import BitrixUser, Employee


class BitrixGateway(Protocol):
    def find_user_by_email(self, email: str) -> BitrixUser | None: ...

    def create_user(self, employee: Employee, department_id: int) -> str: ...

    def set_user_active(self, user_id: str, active: bool) -> None: ...

    def add_to_group(self, user_id: str, group_id: int) -> None: ...

    def remove_from_group(self, user_id: str, group_id: int) -> None: ...


class Bitrix24Client:
    """Minimal Bitrix24 webhook client with bounded retries for transient errors."""

    TRANSIENT_HTTP_CODES = {429, 502, 503, 504}
    TRANSIENT_BITRIX_CODES = {"QUERY_LIMIT_EXCEEDED", "INTERNAL_SERVER_ERROR"}

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.25,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        parsed = urlparse(webhook_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValidationError("BITRIX_WEBHOOK_URL must be a valid HTTPS URL")
        if parsed.query or parsed.fragment:
            raise ValidationError("BITRIX_WEBHOOK_URL must not contain query parameters or a fragment")
        self._webhook_url = webhook_url.rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._opener = opener
        self._sleeper = sleeper

    def _wait_before_retry(self, attempt: int) -> None:
        self._sleeper(self._backoff_seconds * (2**attempt))

    def call(self, method: str, params: Mapping[str, Any]) -> Any:
        payload = json.dumps(dict(params)).encode("utf-8")
        request = Request(
            self._webhook_url + method + ".json",
            data=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )

        for attempt in range(self._max_retries + 1):
            try:
                with self._opener(request, timeout=self._timeout_seconds) as response:
                    response_data = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code in self.TRANSIENT_HTTP_CODES and attempt < self._max_retries:
                    self._wait_before_retry(attempt)
                    continue
                raise BitrixApiError(f"HTTP_{exc.code}", "Bitrix24 request failed", transient=exc.code in self.TRANSIENT_HTTP_CODES) from exc
            except (URLError, TimeoutError, socket.timeout) as exc:
                if attempt < self._max_retries:
                    self._wait_before_retry(attempt)
                    continue
                raise BitrixApiError("NETWORK_ERROR", str(exc), transient=True) from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BitrixApiError("INVALID_RESPONSE", "Bitrix24 returned invalid JSON") from exc

            if "error" in response_data:
                code = str(response_data.get("error", "UNKNOWN_ERROR"))
                description = str(response_data.get("error_description", "Unknown Bitrix24 error"))
                transient = code in self.TRANSIENT_BITRIX_CODES
                if transient and attempt < self._max_retries:
                    self._wait_before_retry(attempt)
                    continue
                raise BitrixApiError(code, description, transient=transient)
            if "result" not in response_data:
                raise BitrixApiError("INVALID_RESPONSE", "Bitrix24 response has no 'result' field")
            return response_data["result"]

        raise AssertionError("Retry loop exhausted unexpectedly")

    def find_user_by_email(self, email: str) -> BitrixUser | None:
        result = self.call("user.get", {"FILTER": {"EMAIL": email}})
        if not result:
            return None
        user = result[0]
        return BitrixUser(str(user["ID"]), str(user.get("EMAIL", email)).lower(), user.get("ACTIVE") == "Y")

    def create_user(self, employee: Employee, department_id: int) -> str:
        result = self.call(
            "user.add",
            {
                "EMAIL": employee.email,
                "NAME": employee.first_name,
                "LAST_NAME": employee.last_name,
                "PERSONAL_MOBILE": employee.phone,
                "WORK_POSITION": employee.job_title,
                "UF_DEPARTMENT": [department_id],
            },
        )
        return str(result)

    def set_user_active(self, user_id: str, active: bool) -> None:
        self.call("user.update", {"ID": user_id, "ACTIVE": "Y" if active else "N"})

    def add_to_group(self, user_id: str, group_id: int) -> None:
        self.call("sonet_group.user.add", {"GROUP_ID": group_id, "USER_ID": user_id})

    def remove_from_group(self, user_id: str, group_id: int) -> None:
        self.call("sonet_group.user.delete", {"GROUP_ID": group_id, "USER_ID": user_id})


class FakeBitrixGateway:
    """Deterministic in-memory gateway used by the demo and unit tests."""

    def __init__(self, *, fail_on: set[str] | None = None):
        self.users_by_email: dict[str, BitrixUser] = {}
        self.groups_by_user: dict[str, set[int]] = {}
        self.calls: list[str] = []
        self.fail_on = fail_on or set()
        self._next_user_id = 1000

    def _record(self, marker: str) -> None:
        self.calls.append(marker)
        if marker in self.fail_on:
            raise BitrixApiError("FAKE_FAILURE", f"Injected failure at {marker}")

    def add_existing_user(self, email: str, *, user_id: str = "900", active: bool = True) -> None:
        self.users_by_email[email.lower()] = BitrixUser(user_id, email.lower(), active)
        self.groups_by_user.setdefault(user_id, set())

    def find_user_by_email(self, email: str) -> BitrixUser | None:
        self._record("find_user")
        return self.users_by_email.get(email.lower())

    def create_user(self, employee: Employee, department_id: int) -> str:
        self._record("create_user")
        if employee.email in self.users_by_email:
            raise BitrixApiError("DUPLICATE_EMAIL", "User already exists")
        user_id = str(self._next_user_id)
        self._next_user_id += 1
        self.users_by_email[employee.email] = BitrixUser(user_id, employee.email, True)
        self.groups_by_user[user_id] = set()
        return user_id

    def set_user_active(self, user_id: str, active: bool) -> None:
        self._record(f"set_active:{active}")
        for email, user in tuple(self.users_by_email.items()):
            if user.user_id == user_id:
                self.users_by_email[email] = BitrixUser(user_id, email, active)
                return
        raise BitrixApiError("USER_NOT_FOUND", user_id)

    def add_to_group(self, user_id: str, group_id: int) -> None:
        self._record(f"add_to_group:{group_id}")
        self.groups_by_user.setdefault(user_id, set()).add(group_id)

    def remove_from_group(self, user_id: str, group_id: int) -> None:
        self._record(f"remove_from_group:{group_id}")
        self.groups_by_user.setdefault(user_id, set()).discard(group_id)
