from __future__ import annotations

PACKAGE_REASONS: tuple[str, ...] = (
    "NOT_A_ZIP",
    "CORRUPT_ARCHIVE",
    "ENCRYPTED_ENTRY",
    "UNSUPPORTED_COMPRESSION",
    "SYMLINK_NOT_ALLOWED",
    "UNSAFE_PATH",
    "DUPLICATE_PATH",
    "TOO_MANY_FILES",
    "TOO_LARGE",
    "MISSING_ENTRYPOINT",
    "AMBIGUOUS_ENTRYPOINT",
    "BAD_FRONTMATTER",
    "MISSING_FIELD",
    "INVALID_FIELD",
    "KIND_MISMATCH",
    "VERSION_MISMATCH",
)


class DomainError(ValueError):
    code = "DOMAIN_ERROR"
    default_message = "Domain error"

    def __init__(self, message: str | None = None, *, reason: str | None = None) -> None:
        text = message if message else self.default_message
        super().__init__(text)
        self.message = text
        self.reason = reason

    def __reduce__(self):
        return (_restore_error, (type(self), self.message, self.reason))


def _restore_error(cls: type[DomainError], message: str, reason: str | None) -> DomainError:
    error = cls.__new__(cls)
    DomainError.__init__(error, message, reason=reason)
    return error


class NotFoundError(DomainError):
    code = "NOT_FOUND"
    default_message = "Resource not found"


class ConflictError(DomainError):
    code = "CONFLICT"
    default_message = "Conflict with the current state of the resource"


class ValidationError(DomainError):
    code = "VALIDATION_ERROR"
    default_message = "Validation error"


class InvalidPackageError(DomainError):
    code = "INVALID_PACKAGE"
    default_message = "Invalid package"

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message if message else f"Invalid package: {reason}", reason=reason)


class ForbiddenError(DomainError):
    code = "FORBIDDEN"
    default_message = "Operation not permitted"


class UnauthorizedError(DomainError):
    code = "UNAUTHORIZED"
    default_message = "Authentication required"


class PayloadTooLargeError(DomainError):
    code = "PAYLOAD_TOO_LARGE"
    default_message = "Payload too large"


class StorageError(DomainError):
    code = "STORAGE_ERROR"
    default_message = "Stored package data is missing or corrupt"
