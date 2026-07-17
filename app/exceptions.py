"""Domain exceptions.

Each carries an HTTP status code so the API layer can translate them into
clean responses without leaking internals.
"""


class DomainError(Exception):
    """Base class for all expected business-rule violations."""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    """A state conflict, e.g. reconciling an already-settled sale."""

    status_code = 409
    code = "conflict"


class ValidationError(DomainError):
    status_code = 422
    code = "validation_error"


class InsufficientBalanceError(DomainError):
    status_code = 422
    code = "insufficient_balance"


class WithdrawalCooldownError(DomainError):
    """Raised when a user tries to withdraw again inside the 24h window."""

    status_code = 429
    code = "withdrawal_cooldown"

    def __init__(self, message: str, retry_after_seconds: int):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
