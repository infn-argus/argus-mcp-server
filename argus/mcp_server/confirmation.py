"""Two-step confirmation for write tools.

LibreChat (v0.8.7) has no way to ask the user before an MCP tool runs - no
approval setting, no MCP elicitation - so a model can write the moment it
decides to (it once posted a "TEST" logbook entry while saying it would search
the logbook). For clients that ask for it (X-Argus-Confirm-Writes, see
correlation.py), ARGUS enforces the pause itself:

1. A write tool called without ``confirmation_id`` does nothing. It answers
   ``confirmation_required`` with a plain-language preview of the action and a
   one-time ``confirmation_id``.
2. Only the same tool, called again with the same arguments plus that id, by
   the same caller, within CONFIRMATION_TTL_SECONDS, runs. The id is consumed.

This rules out one-shot writes and makes every write show the user what is
about to happen. It cannot prove a person said yes - the model sees the id
and could call again without waiting - which is why the tool descriptions
tell it to ask, and why the voice agent keeps its own operator gate.

Pending confirmations live in memory: a restart simply makes the model ask
again. One ARGUS replica per deployment, so no shared store is needed.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

CONFIRMATION_TTL_SECONDS = 600
CONFIRMATION_ARG = "confirmation_id"


def arguments_digest(tool: str, arguments: dict[str, Any]) -> str:
    """A stable fingerprint of exactly what is to be written."""
    canonical = json.dumps(
        {"tool": tool, "arguments": {k: v for k, v in arguments.items() if k != CONFIRMATION_ARG}},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def caller_key(caller_token: str | None, correlation: dict[str, str] | None) -> str:
    """Who is confirming: a hash of their forwarded token, else LibreChat's user id, else nobody in particular."""
    if caller_token:
        return "token:" + hashlib.sha256(caller_token.encode()).hexdigest()[:32]
    user_id = (correlation or {}).get("user_id")
    return f"user:{user_id}" if user_id else "anonymous"


@dataclass
class _Pending:
    digest: str
    caller: str
    expires_at: float


class ConfirmationStore:
    def __init__(self, ttl: float = CONFIRMATION_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._pending: dict[str, _Pending] = {}

    def _prune(self, now: float) -> None:
        for key in [k for k, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[key]

    def issue(self, tool: str, arguments: dict[str, Any], caller: str) -> str:
        now = time.monotonic()
        self._prune(now)
        confirmation_id = "confirm-" + secrets.token_urlsafe(9)
        self._pending[confirmation_id] = _Pending(arguments_digest(tool, arguments), caller, now + self._ttl)
        return confirmation_id

    def redeem(self, confirmation_id: str, tool: str, arguments: dict[str, Any], caller: str) -> str | None:
        """Consume the id. None if it matches; otherwise why it doesn't (the id stays unusable either way)."""
        now = time.monotonic()
        self._prune(now)
        pending = self._pending.pop(confirmation_id, None)
        if pending is None:
            return "unknown or expired confirmation_id - call the tool again without it to get a new one"
        if pending.caller != caller:
            return "this confirmation_id was issued to another user"
        if pending.digest != arguments_digest(tool, arguments):
            return (
                "the arguments differ from the ones that were confirmed - call the tool again without "
                "confirmation_id to preview and confirm the new ones"
            )
        return None

    @property
    def ttl(self) -> float:
        return self._ttl
