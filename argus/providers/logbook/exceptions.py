from __future__ import annotations

from argus.core.errors import ArgusError, ProviderTimeoutError, ProviderUnavailableError, ProviderUnconfiguredError


class LogbookError(ArgusError):
    code = "logbook_error"


class LogbookUnconfiguredError(LogbookError, ProviderUnconfiguredError):
    code = "logbook_unconfigured"


class LogbookUnavailableError(LogbookError, ProviderUnavailableError):
    code = "logbook_unavailable"


class LogbookTimeoutError(LogbookError, ProviderTimeoutError):
    code = "logbook_timeout"


class LogbookQueryError(LogbookError):
    code = "logbook_query_error"


class LogbookRefusedError(LogbookError):
    """Olog answered, and refused the request (4xx): a bad logbook name, no permission, ..."""

    code = "logbook_refused"
