from __future__ import annotations

from argus.core.errors import ArgusError, ProviderTimeoutError, ProviderUnavailableError, ProviderUnconfiguredError


class PrometheusError(ArgusError):
    code = "prometheus_error"


class PrometheusUnconfiguredError(PrometheusError, ProviderUnconfiguredError):
    code = "prometheus_unconfigured"


class PrometheusUnavailableError(PrometheusError, ProviderUnavailableError):
    code = "prometheus_unavailable"


class PrometheusTimeoutError(PrometheusError, ProviderTimeoutError):
    code = "prometheus_timeout"


class PrometheusQueryError(PrometheusError):
    code = "prometheus_query_error"


class PrometheusNodeNotFoundError(PrometheusError):
    code = "prometheus_node_not_found"
