from __future__ import annotations

from argus.core.errors import ArgusError, ProviderTimeoutError, ProviderUnavailableError, ProviderUnconfiguredError


class SaveRestoreError(ArgusError):
    code = "saverestore_error"


class SaveRestoreUnconfiguredError(SaveRestoreError, ProviderUnconfiguredError):
    code = "saverestore_unconfigured"


class SaveRestoreUnavailableError(SaveRestoreError, ProviderUnavailableError):
    code = "saverestore_unavailable"


class SaveRestoreTimeoutError(SaveRestoreError, ProviderTimeoutError):
    code = "saverestore_timeout"


class SaveRestoreQueryError(SaveRestoreError):
    code = "saverestore_query_error"


class SaveRestoreRefusedError(SaveRestoreError):
    """The service answered and refused (4xx): not signed in, a name already taken, a bad folder, ..."""

    code = "saverestore_refused"
