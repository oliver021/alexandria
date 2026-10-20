from __future__ import annotations

from collections.abc import Sequence

from app.services.errors import UnauthorizedError

AUTHORIZATION_HEADER = "authorization"
BEARER_SCHEME = "bearer"
DUPLICATE_HEADER_MESSAGE = "Exactly one Authorization header is allowed"
MALFORMED_HEADER_MESSAGE = "The Authorization header must be 'Bearer <credential>'"


def extract_bearer(values: Sequence[str]) -> str | None:
    if not values:
        return None
    if len(values) > 1:
        raise UnauthorizedError(DUPLICATE_HEADER_MESSAGE)
    scheme, _, credential = values[0].partition(" ")
    credential = credential.strip()
    if scheme.lower() != BEARER_SCHEME or not credential or len(credential.split()) != 1:
        raise UnauthorizedError(MALFORMED_HEADER_MESSAGE)
    return credential
