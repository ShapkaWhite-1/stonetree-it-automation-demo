class AutomationError(Exception):
    """Base error for the application."""


class ValidationError(AutomationError):
    """Input data is invalid."""


class PolicyError(AutomationError):
    """No access policy can be resolved for the employee."""


class InventoryError(AutomationError):
    """Required equipment is not available."""


class OperationInProgressError(AutomationError):
    """The same idempotency key is already being processed."""


class BitrixApiError(AutomationError):
    def __init__(self, code: str, description: str, *, transient: bool = False):
        super().__init__(f"Bitrix24 error {code}: {description}")
        self.code = code
        self.description = description
        self.transient = transient

