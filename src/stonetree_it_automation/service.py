from __future__ import annotations

from typing import Any

from .errors import OperationInProgressError
from .gateway import BitrixGateway
from .models import Employee, OperationStatus, StepResult, StepStatus, WorkflowResult
from .policy import AccessPolicy
from .repository import SQLiteRepository


class WorkflowRecorder:
    def __init__(self, request_id: str, repository: SQLiteRepository):
        self.request_id = request_id
        self.repository = repository
        self.steps: list[StepResult] = []

    def add(self, name: str, status: StepStatus, **details: Any) -> None:
        step = StepResult(name, status, details)
        self.steps.append(step)
        self.repository.append_event(self.request_id, step)


class BaseService:
    operation_name = ""

    def __init__(self, gateway: BitrixGateway, repository: SQLiteRepository, policy: AccessPolicy):
        self.gateway = gateway
        self.repository = repository
        self.policy = policy

    def _start(self, request_id: str, employee: Employee) -> WorkflowResult | None:
        employee.validate()
        cached = self.repository.get_result(request_id)
        if cached is not None:
            return cached
        if not self.repository.begin_operation(request_id, self.operation_name, employee.employee_id):
            cached = self.repository.get_result(request_id)
            if cached is not None:
                return cached
            raise OperationInProgressError(f"Request '{request_id}' is already running")
        return None


class OnboardingService(BaseService):
    operation_name = "onboarding"

    def execute(self, request_id: str, employee: Employee) -> WorkflowResult:
        employee.validate()
        role_policy = self.policy.resolve(employee.role)
        cached = self._start(request_id, employee)
        if cached is not None:
            return cached

        recorder = WorkflowRecorder(request_id, self.repository)
        user_id: str | None = None
        created_user = False
        reactivated_user = False
        added_groups: list[int] = []
        all_asset_tags: tuple[str, ...] = ()
        new_asset_tags: tuple[str, ...] = ()

        try:
            recorder.add("validate_input", StepStatus.SUCCEEDED, role=employee.role)

            reservation = self.repository.reserve_assets(employee.employee_id, role_policy.asset_types)
            all_asset_tags = reservation.all_tags
            new_asset_tags = reservation.newly_reserved_tags
            recorder.add(
                "reserve_assets",
                StepStatus.SUCCEEDED,
                asset_tags=list(all_asset_tags),
                newly_reserved=list(new_asset_tags),
            )

            existing_user = self.gateway.find_user_by_email(employee.email)
            if existing_user is None:
                user_id = self.gateway.create_user(employee, role_policy.department_id)
                created_user = True
                recorder.add("create_bitrix_user", StepStatus.SUCCEEDED, user_id=user_id)
            else:
                user_id = existing_user.user_id
                recorder.add("create_bitrix_user", StepStatus.SKIPPED, reason="email_exists", user_id=user_id)
                if not existing_user.active:
                    self.gateway.set_user_active(user_id, True)
                    reactivated_user = True
                    recorder.add("reactivate_bitrix_user", StepStatus.SUCCEEDED, user_id=user_id)

            for group_id in role_policy.group_ids:
                # Add the group to the compensation set before the request. If the
                # network fails after Bitrix24 applied the change, rollback still
                # attempts an idempotent delete instead of leaving unknown access.
                added_groups.append(group_id)
                self.gateway.add_to_group(user_id, group_id)
                recorder.add("add_to_group", StepStatus.SUCCEEDED, user_id=user_id, group_id=group_id)

            result = WorkflowResult(
                request_id=request_id,
                operation=self.operation_name,
                employee_id=employee.employee_id,
                status=OperationStatus.SUCCEEDED,
                steps=tuple(recorder.steps),
                bitrix_user_id=user_id,
                asset_tags=all_asset_tags,
            )
            self.repository.finish_operation(result)
            return result
        except Exception as exc:
            recorder.add("workflow", StepStatus.FAILED, error=type(exc).__name__, message=str(exc))
            compensation_errors: list[str] = []

            if user_id is not None:
                for group_id in reversed(added_groups):
                    try:
                        self.gateway.remove_from_group(user_id, group_id)
                        recorder.add("rollback_group", StepStatus.COMPENSATED, group_id=group_id)
                    except Exception as rollback_exc:
                        compensation_errors.append(f"group {group_id}: {rollback_exc}")
                        recorder.add("rollback_group", StepStatus.FAILED, group_id=group_id, message=str(rollback_exc))

            if user_id is not None and (created_user or reactivated_user):
                try:
                    self.gateway.set_user_active(user_id, False)
                    recorder.add("rollback_user", StepStatus.COMPENSATED, user_id=user_id)
                except Exception as rollback_exc:
                    compensation_errors.append(f"user {user_id}: {rollback_exc}")
                    recorder.add("rollback_user", StepStatus.FAILED, user_id=user_id, message=str(rollback_exc))

            try:
                released = self.repository.release_assets(employee.employee_id, new_asset_tags)
                recorder.add("rollback_assets", StepStatus.COMPENSATED, asset_tags=list(released))
            except Exception as rollback_exc:
                compensation_errors.append(f"assets: {rollback_exc}")
                recorder.add("rollback_assets", StepStatus.FAILED, message=str(rollback_exc))

            status = OperationStatus.PARTIAL if compensation_errors else OperationStatus.FAILED
            error_message = str(exc)
            if compensation_errors:
                error_message += "; compensation errors: " + "; ".join(compensation_errors)
            result = WorkflowResult(
                request_id=request_id,
                operation=self.operation_name,
                employee_id=employee.employee_id,
                status=status,
                steps=tuple(recorder.steps),
                bitrix_user_id=user_id,
                asset_tags=self.repository.active_assignments(employee.employee_id),
                error=error_message,
            )
            self.repository.finish_operation(result)
            return result


class OffboardingService(BaseService):
    operation_name = "offboarding"

    def execute(self, request_id: str, employee: Employee) -> WorkflowResult:
        employee.validate()
        role_policy = self.policy.resolve(employee.role)
        cached = self._start(request_id, employee)
        if cached is not None:
            return cached

        recorder = WorkflowRecorder(request_id, self.repository)
        errors: list[str] = []
        user_id: str | None = None

        recorder.add("validate_input", StepStatus.SUCCEEDED, role=employee.role)
        try:
            user = self.gateway.find_user_by_email(employee.email)
        except Exception as exc:
            user = None
            errors.append(f"user lookup: {exc}")
            recorder.add("find_bitrix_user", StepStatus.FAILED, message=str(exc))

        if user is None:
            recorder.add("deactivate_bitrix_user", StepStatus.SKIPPED, reason="user_not_found")
        else:
            user_id = user.user_id
            if user.active:
                try:
                    self.gateway.set_user_active(user_id, False)
                    recorder.add("deactivate_bitrix_user", StepStatus.SUCCEEDED, user_id=user_id)
                except Exception as exc:
                    errors.append(f"deactivate user: {exc}")
                    recorder.add("deactivate_bitrix_user", StepStatus.FAILED, user_id=user_id, message=str(exc))
            else:
                recorder.add("deactivate_bitrix_user", StepStatus.SKIPPED, reason="already_inactive", user_id=user_id)

            for group_id in role_policy.group_ids:
                try:
                    self.gateway.remove_from_group(user_id, group_id)
                    recorder.add("remove_from_group", StepStatus.SUCCEEDED, user_id=user_id, group_id=group_id)
                except Exception as exc:
                    errors.append(f"remove group {group_id}: {exc}")
                    recorder.add("remove_from_group", StepStatus.FAILED, group_id=group_id, message=str(exc))

        try:
            released = self.repository.release_assets(employee.employee_id)
            recorder.add("release_assets", StepStatus.SUCCEEDED, asset_tags=list(released))
        except Exception as exc:
            errors.append(f"release assets: {exc}")
            recorder.add("release_assets", StepStatus.FAILED, message=str(exc))

        status = OperationStatus.SUCCEEDED if not errors else OperationStatus.PARTIAL
        result = WorkflowResult(
            request_id=request_id,
            operation=self.operation_name,
            employee_id=employee.employee_id,
            status=status,
            steps=tuple(recorder.steps),
            bitrix_user_id=user_id,
            asset_tags=self.repository.active_assignments(employee.employee_id),
            error="; ".join(errors) if errors else None,
        )
        self.repository.finish_operation(result)
        return result
