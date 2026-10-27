"""ARGUS configuration.

Every provider's settings are entirely optional so the server boots with only
EPICS configured (today's ``.env``). Absence of a provider's settings is what
makes ``Provider.is_configured()`` return False for that provider.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class _ProviderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class EpicsSettings(_ProviderSettings):
    ca_addr_list: str | None = Field(default=None, alias="EPICS_CA_ADDR_LIST")
    ca_auto_addr_list: bool = Field(default=True, alias="EPICS_CA_AUTO_ADDR_LIST")
    pva_addr_list: str | None = Field(default=None, alias="EPICS_PVA_ADDR_LIST")


class ArchiverSettings(_ProviderSettings):
    base_url: str | None = Field(default=None, alias="ARCHIVER_BASE_URL")


class ChannelFinderSettings(_ProviderSettings):
    base_url: str | None = Field(default=None, alias="CHANNELFINDER_BASE_URL")
    username: str | None = Field(default=None, alias="CHANNELFINDER_USERNAME")
    password: str | None = Field(default=None, alias="CHANNELFINDER_PASSWORD")


class KubernetesSettings(_ProviderSettings):
    kubeconfig_path: str | None = Field(default=None, alias="KUBECONFIG")
    default_namespace: str = Field(default="default", alias="K8S_NAMESPACE_DEFAULT")
    ioc_label_selector_template: str = Field(
        default="app=ioc,device={ioc_name}", alias="K8S_IOC_LABEL_SELECTOR_TEMPLATE"
    )


class ArgoCDSettings(_ProviderSettings):
    server_url: str | None = Field(default=None, alias="ARGOCD_SERVER_URL")
    auth_token: str | None = Field(default=None, alias="ARGOCD_AUTH_TOKEN")


class SaveRestoreSettings(_ProviderSettings):
    # Phoebus save-and-restore service base URL, e.g. http://saveandrestore.btf.svc.cluster.local/save-restore
    base_url: str | None = Field(default=None, alias="SAVERESTORE_BASE_URL")


class LogbookSettings(_ProviderSettings):
    base_url: str | None = Field(default=None, alias="LOGBOOK_BASE_URL")
    username: str | None = Field(default=None, alias="LOGBOOK_USERNAME")
    password: str | None = Field(default=None, alias="LOGBOOK_PASSWORD")


class ElasticSettings(_ProviderSettings):
    url: str | None = Field(default=None, alias="ELASTIC_URL")
    api_key: str | None = Field(default=None, alias="ELASTIC_API_KEY")
    default_index: str = Field(default="argus-logs", alias="ELASTIC_DEFAULT_INDEX")


class LokiSettings(_ProviderSettings):
    # Base URL of the platform's shared Loki instance, e.g.
    # http://epik8s-platform-loki.logging.svc.cluster.local:3100 — pod log
    # history (7-day retention, survives pod restarts), distinct from
    # ElasticSettings above (a separate, control-critical log store).
    base_url: str | None = Field(default=None, alias="LOKI_BASE_URL")
    default_limit: int = Field(default=200, alias="LOKI_DEFAULT_LIMIT")


class DocumentationSettings(_ProviderSettings):
    docs_path: str | None = Field(default=None, alias="DOCS_PATH")
    index_path: str | None = Field(default=None, alias="DOCS_INDEX_PATH")


class RagflowSettings(_ProviderSettings):
    base_url: str | None = Field(default=None, alias="RAGFLOW_BASE_URL")
    api_key: str | None = Field(default=None, alias="RAGFLOW_API_KEY")
    # Comma-separated exact dataset UUIDs, if already known. Most deployments
    # should prefer dataset_names below instead -- UUIDs are opaque and
    # specific to one RAGFLOW instance, so hardcoding them isn't portable
    # across beamlines. NOTE: at least one dataset (by id or resolved name)
    # is required by some RAGFLOW deployments -- confirmed live against
    # INFN's instance, which rejects a request with neither ("`dataset_ids`
    # is required."). Left both empty, search will fail with that same
    # RAGFLOW-side error, not a generic "no docs found".
    dataset_ids: str | None = Field(default=None, alias="RAGFLOW_DATASET_IDS")
    # Comma-separated case-insensitive substrings matched against dataset
    # names (e.g. "Controlli" matches a dataset literally named
    # "Controlli-KB") and resolved to their ids at query time via GET
    # /api/v1/datasets, cached briefly. A name with no match is skipped, not
    # an error -- e.g. a per-beamline dataset that doesn't exist yet.
    dataset_names: str | None = Field(default=None, alias="RAGFLOW_DATASET_NAMES")
    timeout_seconds: float = Field(default=20.0, alias="RAGFLOW_TIMEOUT_SECONDS")


class KnowledgeHubSettings(_ProviderSettings):
    # The ARGUS Knowledge Hub's MCP endpoint (streamable HTTP), e.g.
    # https://argus-hub-api.example.org/mcp -- the hub serves its MCP from
    # its own API, behind the same personal access token as its REST API.
    url: str | None = Field(default=None, alias="KNOWLEDGE_HUB_URL")
    # A personal access token generated in the hub (My account -> Personal
    # access tokens, "argus_pat_..."). It reads what its owner may read: an
    # administrator's token with the `admin` scope and no fixed workspace
    # reads every workspace; a read-only token reads only the workspaces its
    # owner holds a role in (or that are open to everyone).
    token: str | None = Field(default=None, alias="KNOWLEDGE_HUB_TOKEN")
    # Comma-separated workspace ids to ask in (e.g. "btf,controls-catalogue").
    # Blank = every workspace the token may read, as the hub's own
    # list_workspaces reports it. Each search fans out across all of them.
    workspaces: str | None = Field(default=None, alias="KNOWLEDGE_HUB_WORKSPACES")
    timeout_seconds: float = Field(default=20.0, alias="KNOWLEDGE_HUB_TIMEOUT_SECONDS")


class GitHubSettings(_ProviderSettings):
    base_url: str = Field(default="https://api.github.com", alias="GITHUB_BASE_URL")
    token: str | None = Field(default=None, alias="GITHUB_TOKEN")


class GitLabSettings(_ProviderSettings):
    base_url: str | None = Field(default=None, alias="GITLAB_BASE_URL")
    token: str | None = Field(default=None, alias="GITLAB_TOKEN")


class CacheSettings(_ProviderSettings):
    device_ttl_seconds: float = Field(default=300.0, alias="CACHE_DEVICE_TTL_SECONDS")
    channelfinder_ttl_seconds: float = Field(default=120.0, alias="CACHE_CHANNELFINDER_TTL_SECONDS")
    kubernetes_ttl_seconds: float = Field(default=30.0, alias="CACHE_KUBERNETES_TTL_SECONDS")
    # get_config_history is now the recommended first call for most beamline
    # questions (see knowledge_tools.py), so it needs a much longer TTL than
    # live-data caches above -- config files change on the order of days, not
    # seconds, and every GitHub/GitLab call is a real network round-trip.
    knowledge_ttl_seconds: float = Field(default=600.0, alias="CACHE_KNOWLEDGE_TTL_SECONDS")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    transport: Literal["stdio", "sse"] = Field(default="stdio", alias="ARGUS_TRANSPORT")
    sse_host: str = Field(default="127.0.0.1", alias="ARGUS_SSE_HOST")
    sse_port: int = Field(default=8000, alias="ARGUS_SSE_PORT")
    log_level: str = Field(default="INFO", alias="ARGUS_LOG_LEVEL")
    log_json: bool = Field(default=True, alias="ARGUS_LOG_JSON")

    # Comma-separated repo URLs search_knowledge_base searches when the
    # caller doesn't specify a repo list explicitly.
    git_default_repos: str = Field(default="", alias="GIT_DEFAULT_REPOS")

    # Git ref (branch/tag) get_file/get_config_history/list_beamline_devices
    # resolve a caller-passed "HEAD" (or an omitted ref) to, instead of git's
    # own bare "HEAD" meaning -- a beamline isn't always deployed from its
    # repo's default branch (e.g. "devel" instead of "main"), so literal
    # "HEAD" can silently read the wrong branch's config.
    git_default_ref: str = Field(default="HEAD", alias="GIT_DEFAULT_REF")

    # Which DocumentationProvider backs search_documentation. "ragflow"
    # requires ragflow.base_url + ragflow.api_key; "knowledge_hub" requires
    # knowledge_hub.url + knowledge_hub.token. Either reports itself as
    # unconfigured (rather than silently falling back) when its settings are
    # missing.
    documentation_backend: Literal["local", "ragflow", "knowledge_hub"] = Field(
        default="local", alias="DOCUMENTATION_BACKEND"
    )

    # Where list_beamline_devices reads the device inventory from: "yaml"
    # parses the beamline's epik8s deploy YAML from its git repo (the legacy
    # path); "knowledge_hub" reads the Control Device records the hub
    # imported from that same configuration.
    inventory_backend: Literal["yaml", "knowledge_hub"] = Field(default="yaml", alias="INVENTORY_BACKEND")

    # Where logbook reads (get_alarm_history, diagnose_device, machine_summary)
    # come from: "olog" queries the Olog REST API; "knowledge_hub" searches
    # the Logbook Entry documents the hub keeps of that Olog. The hub is
    # read-only, so create_logbook_entry always posts to Olog either way.
    logbook_backend: Literal["olog", "knowledge_hub"] = Field(default="olog", alias="LOGBOOK_BACKEND")

    epics: EpicsSettings = Field(default_factory=EpicsSettings)
    archiver: ArchiverSettings = Field(default_factory=ArchiverSettings)
    channelfinder: ChannelFinderSettings = Field(default_factory=ChannelFinderSettings)
    kubernetes: KubernetesSettings = Field(default_factory=KubernetesSettings)
    argocd: ArgoCDSettings = Field(default_factory=ArgoCDSettings)
    saverestore: SaveRestoreSettings = Field(default_factory=SaveRestoreSettings)
    logbook: LogbookSettings = Field(default_factory=LogbookSettings)
    elastic: ElasticSettings = Field(default_factory=ElasticSettings)
    loki: LokiSettings = Field(default_factory=LokiSettings)
    documentation: DocumentationSettings = Field(default_factory=DocumentationSettings)
    ragflow: RagflowSettings = Field(default_factory=RagflowSettings)
    knowledge_hub: KnowledgeHubSettings = Field(default_factory=KnowledgeHubSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    gitlab: GitLabSettings = Field(default_factory=GitLabSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
