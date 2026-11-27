"""Reading a forwarded access token's claims - without verifying it.

ARGUS never trusts these claims for access: Olog and the other services verify
the token themselves. They are read only to tell who a caller is across token
refreshes (a confirmation stays theirs) and whether the token has already
expired (so a write stops with a clear message instead of reaching Olog
without a valid sign-in).
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any


def claims(token: str | None) -> dict[str, Any]:
    """The token's payload, or {} when it is not a readable JWT."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def expired(token: str | None, *, leeway: float = 5.0) -> bool:
    """True when the token says it has expired (a token without exp never does)."""
    exp = claims(token).get("exp")
    return isinstance(exp, (int, float)) and exp <= time.time() + leeway


def subject(token: str | None) -> str | None:
    """A stable id of the person the token is for (issuer + sub), the same across refreshes."""
    data = claims(token)
    sub = data.get("sub")
    return f"{data.get('iss', '')}|{sub}" if sub else None
