from __future__ import annotations

from argus.core.errors import ArgusError, ProviderTimeoutError, ProviderUnavailableError, ProviderUnconfiguredError


class KnowledgeHubError(ArgusError):
    code = "knowledge_hub_error"


class KnowledgeHubUnconfiguredError(KnowledgeHubError, ProviderUnconfiguredError):
    code = "knowledge_hub_unconfigured"


class KnowledgeHubUnavailableError(KnowledgeHubError, ProviderUnavailableError):
    code = "knowledge_hub_unavailable"


class KnowledgeHubTimeoutError(KnowledgeHubError, ProviderTimeoutError):
    code = "knowledge_hub_timeout"


class KnowledgeHubAuthError(KnowledgeHubError):
    code = "knowledge_hub_auth_error"


class KnowledgeHubQueryError(KnowledgeHubError):
    """A JSON-RPC error, an isError tool result, or a response of the wrong shape."""

    code = "knowledge_hub_query_error"
