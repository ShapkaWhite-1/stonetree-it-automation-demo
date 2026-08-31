from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from stonetree_it_automation.errors import ValidationError  # noqa: E402
from stonetree_it_automation.gateway import FakeBitrixGateway  # noqa: E402
from stonetree_it_automation.models import Employee, OperationStatus  # noqa: E402
from stonetree_it_automation.policy import AccessPolicy  # noqa: E402
from stonetree_it_automation.repository import SQLiteRepository  # noqa: E402
from stonetree_it_automation.service import OffboardingService, OnboardingService  # noqa: E402


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repository = SQLiteRepository(Path(self.temp_dir.name) / "test.sqlite3")
        self.repository.seed_assets([("LT-001", "laptop"), ("SIM-001", "sim_card")])
        self.policy = AccessPolicy.from_mapping(
            {
                "roles": {
                    "it-support-specialist": {
                        "department_id": 1,
                        "group_ids": [10, 20],
                        "asset_types": ["laptop", "sim_card"],
                    }
                }
            }
        )
        self.employee = Employee(
            employee_id="ST-001",
            first_name="Alex",
            last_name="Morgan",
            email="alex@example.com",
            role="it-support-specialist",
            job_title="IT Support Specialist",
            phone="+971500000000",
        )

    def test_onboarding_happy_path(self) -> None:
        gateway = FakeBitrixGateway()

        result = OnboardingService(gateway, self.repository, self.policy).execute("req-001", self.employee)

        self.assertEqual(OperationStatus.SUCCEEDED, result.status)
        self.assertEqual(("LT-001", "SIM-001"), result.asset_tags)
        self.assertEqual({10, 20}, gateway.groups_by_user[result.bitrix_user_id])
        self.assertEqual(("LT-001", "SIM-001"), self.repository.active_assignments(self.employee.employee_id))

    def test_same_request_is_idempotent(self) -> None:
        gateway = FakeBitrixGateway()
        service = OnboardingService(gateway, self.repository, self.policy)

        first = service.execute("same-request", self.employee)
        call_count = len(gateway.calls)
        second = service.execute("same-request", self.employee)

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(call_count, len(gateway.calls), "The replay must not call Bitrix24 again")

    def test_group_failure_runs_compensation(self) -> None:
        gateway = FakeBitrixGateway(fail_on={"add_to_group:20"})

        result = OnboardingService(gateway, self.repository, self.policy).execute("req-failure", self.employee)

        self.assertEqual(OperationStatus.FAILED, result.status)
        user = gateway.users_by_email[self.employee.email]
        self.assertFalse(user.active, "A newly created user must be deactivated during rollback")
        self.assertEqual(set(), gateway.groups_by_user[user.user_id])
        self.assertEqual("available", self.repository.asset_status("LT-001"))
        self.assertEqual("available", self.repository.asset_status("SIM-001"))

    def test_inventory_failure_happens_before_external_side_effects(self) -> None:
        empty_repository = SQLiteRepository(Path(self.temp_dir.name) / "empty.sqlite3")
        gateway = FakeBitrixGateway()

        result = OnboardingService(gateway, empty_repository, self.policy).execute("req-no-assets", self.employee)

        self.assertEqual(OperationStatus.FAILED, result.status)
        self.assertEqual([], gateway.calls, "Bitrix24 must not be called when preflight checks fail")

    def test_offboarding_disables_access_before_releasing_assets(self) -> None:
        gateway = FakeBitrixGateway()
        onboard = OnboardingService(gateway, self.repository, self.policy).execute("req-on", self.employee)
        self.assertEqual(OperationStatus.SUCCEEDED, onboard.status)

        result = OffboardingService(gateway, self.repository, self.policy).execute("req-off", self.employee)

        self.assertEqual(OperationStatus.SUCCEEDED, result.status)
        self.assertFalse(gateway.users_by_email[self.employee.email].active)
        self.assertEqual(set(), gateway.groups_by_user[onboard.bitrix_user_id])
        self.assertEqual((), self.repository.active_assignments(self.employee.employee_id))
        self.assertLess(
            gateway.calls.index("set_active:False"),
            gateway.calls.index("remove_from_group:10"),
            "Account deactivation is the first security action",
        )

    def test_offboarding_continues_after_group_cleanup_error(self) -> None:
        gateway = FakeBitrixGateway()
        OnboardingService(gateway, self.repository, self.policy).execute("req-on-partial", self.employee)
        gateway.fail_on.add("remove_from_group:20")

        result = OffboardingService(gateway, self.repository, self.policy).execute("req-off-partial", self.employee)

        self.assertEqual(OperationStatus.PARTIAL, result.status)
        self.assertFalse(gateway.users_by_email[self.employee.email].active)
        self.assertEqual((), self.repository.active_assignments(self.employee.employee_id))

    def test_invalid_email_is_rejected(self) -> None:
        invalid = Employee(
            employee_id="ST-002",
            first_name="Invalid",
            last_name="Email",
            email="not-an-email",
            role="it-support-specialist",
            job_title="IT Support Specialist",
        )

        with self.assertRaises(ValidationError):
            OnboardingService(FakeBitrixGateway(), self.repository, self.policy).execute("req-invalid", invalid)


if __name__ == "__main__":
    unittest.main()

