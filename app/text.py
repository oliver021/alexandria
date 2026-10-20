from __future__ import annotations

from typing import TypeGuard


def is_valid_text(value: object) -> TypeGuard[str]:
    if not isinstance(value, str) or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
