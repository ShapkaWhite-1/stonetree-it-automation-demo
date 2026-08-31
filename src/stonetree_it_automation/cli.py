from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from .errors import AutomationError
from .gateway import Bitrix24Client, FakeBitrixGateway
from .models import Employee, OperationStatus
from .policy import AccessPolicy
from .repository import SQLiteRepository
from .service import OffboardingService, OnboardingService


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = PROJECT_ROOT / "examples" / "access_policy.json"
DEFAULT_EMPLOYEE = PROJECT_ROOT / "examples" / "employee.json"


def load_employee(path: str | Path) -> Employee:
    with Path(path).open("r", encoding="utf-8") as stream:
        return Employee.from_mapping(json.load(stream))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Automate Bitrix24 onboarding/offboarding with an auditable local workflow."
    )
    parser.add_argument("--db", default=os.getenv("AUDIT_DB_PATH", str(PROJECT_ROOT / ".data" / "operations.sqlite3")))
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--webhook-url", default=os.getenv("BITRIX_WEBHOOK_URL"))
    parser.add_argument("--dry-run", action="store_true", help="Use an in-memory fake Bitrix24 gateway")

    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="Run a safe offline onboarding example")
    demo.add_argument("--employee", default=str(DEFAULT_EMPLOYEE))

    for command in ("onboard", "offboard"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--employee", required=True, help="Path to employee JSON")
        command_parser.add_argument("--request-id", required=True, help="Unique idempotency key")
    return parser


def seed_demo_assets(repository: SQLiteRepository, asset_types: tuple[str, ...]) -> None:
    repository.seed_assets(
        (f"DEMO-{asset_type.upper().replace('_', '-')}-01", asset_type)
        for asset_type in asset_types
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        employee = load_employee(args.employee)
        policy = AccessPolicy.from_file(args.policy)
        role_policy = policy.resolve(employee.role)
        repository = SQLiteRepository(args.db)

        use_fake = args.dry_run or args.command == "demo"
        if use_fake:
            gateway = FakeBitrixGateway()
            seed_demo_assets(repository, role_policy.asset_types)
        else:
            if not args.webhook_url:
                raise AutomationError("Set BITRIX_WEBHOOK_URL or pass --webhook-url; use --dry-run for an offline run")
            gateway = Bitrix24Client(args.webhook_url)

        if args.command in {"demo", "onboard"}:
            request_id = f"demo-{uuid4()}" if args.command == "demo" else args.request_id
            result = OnboardingService(gateway, repository, policy).execute(request_id, employee)
        else:
            if use_fake:
                gateway.add_existing_user(employee.email)
                for group_id in role_policy.group_ids:
                    gateway.groups_by_user["900"].add(group_id)
            result = OffboardingService(gateway, repository, policy).execute(args.request_id, employee)

        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0 if result.status == OperationStatus.SUCCEEDED else 1
    except (AutomationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__, "message": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
