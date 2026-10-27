"""Phoebus save-and-restore provider.

Talks to the REST API exposed by the Phoebus save-and-restore service
(deployed in epik8s beamlines via phoebus-services-chart, image
ghcr.io/infn-epics/phoebus/service-save-and-restore). Endpoints below were
confirmed live against a real BTF deployment, not assumed from docs:

    GET /search?...          -> {"hitCount": N, "nodes": [Node, ...]}
    GET /node/{id}           -> Node
    GET /node/{id}/children  -> [Node, ...]
    GET /config/{id}         -> {"uniqueId": ..., "pvList": [...]}
    GET /snapshot/{id}       -> {"uniqueId": ..., "snapshotItems": [...]}

Creating, as the signed-in user: configurations and snapshots are written
with the caller's forwarded INFN AAI token (core/request_context), which the
INFN service build accepts as a JWT bearer (oauth2.enabled, same issuer as
LibreChat and Olog) and records as the node's owner:

    PUT /config?parentNodeId=<folder>        -> Configuration
    GET /take-snapshot/<config>              -> [SnapshotItem]  (the service reads the PVs)
    PUT /snapshot?parentNodeId=<config>      -> Snapshot

The one-call PUT /take-snapshot/<config> is deliberately not used: it skips
the service's authorization check and records no owner.

Restoring a snapshot is still left out: it writes potentially dozens of PVs
back across the machine - the highest-blast-radius write ARGUS could have.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

import httpx

from argus.config.settings import SaveRestoreSettings
from argus.core.request_context import current_caller_token
from argus.providers.base import ProviderHealth, ProviderStatus
from argus.providers.saverestore.exceptions import (
    SaveRestoreQueryError,
    SaveRestoreRefusedError,
    SaveRestoreTimeoutError,
    SaveRestoreUnavailableError,
    SaveRestoreUnconfiguredError,
)
from argus.providers.saverestore.models import SnrConfiguration, SnrNode, SnrPvEntry, SnrSnapshot, SnrSnapshotValue


# Phoebus save-and-restore's fixed root folder (Node.ROOT_FOLDER_UNIQUE_ID).
ROOT_FOLDER_ID = "44bef5de-e8e6-4014-af37-b8f6c8a939a2"


class SaveRestoreProvider:
    name: ClassVar[str] = "saverestore"

    def __init__(self, settings: SaveRestoreSettings) -> None:
        self._settings = settings

    def is_configured(self) -> bool:
        return bool(self._settings.base_url)

    async def health(self) -> ProviderHealth:
        if not self.is_configured():
            return ProviderHealth(name=self.name, status=ProviderStatus.UNCONFIGURED)
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{self._settings.base_url}/")
            if resp.status_code < 500:
                return ProviderHealth(name=self.name, status=ProviderStatus.OK)
        except httpx.HTTPError as exc:
            return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE, detail=str(exc))
        return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE)

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise SaveRestoreUnconfiguredError("Save-and-restore is not configured (set SAVERESTORE_BASE_URL).")

    def _client(self, timeout: float) -> httpx.AsyncClient:
        """As the caller when this tool call carries their token; anonymous otherwise (reads are public)."""
        token = current_caller_token()
        headers = {"Authorization": f"Bearer {token}"} if token else None
        return httpx.AsyncClient(timeout=timeout, headers=headers)

    async def _request(
        self, method: str, path: str, *, params: dict | None = None, json: Any = None, timeout: float
    ) -> Any:
        try:
            async with self._client(timeout) as client:
                resp = await client.request(method, f"{self._settings.base_url}{path}", params=params, json=json)
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException as exc:
            raise SaveRestoreTimeoutError(f"Timeout calling save-and-restore {path}") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise _refusal(exc, method, path) from exc
            raise SaveRestoreUnavailableError(f"Save-and-restore error: {exc}") from exc
        except httpx.HTTPError as exc:
            raise SaveRestoreUnavailableError(f"Save-and-restore unreachable: {exc}") from exc

    async def _get(self, path: str, *, params: dict | None = None, timeout: float) -> dict | list:
        return await self._request("GET", path, params=params, timeout=timeout)

    async def search(self, query: str, *, timeout: float = 5.0) -> list[SnrNode]:
        self._require_configured()
        payload = await self._get("/search", params={"query": query}, timeout=timeout)
        try:
            return [_to_node(n) for n in payload["nodes"]]
        except (KeyError, TypeError) as exc:
            raise SaveRestoreQueryError(f"Unexpected save-and-restore search response shape: {exc}") from exc

    async def get_node(self, node_id: str, *, timeout: float = 5.0) -> SnrNode:
        self._require_configured()
        payload = await self._get(f"/node/{node_id}", timeout=timeout)
        try:
            return _to_node(payload)
        except (KeyError, TypeError) as exc:
            raise SaveRestoreQueryError(f"Unexpected save-and-restore node response shape: {exc}") from exc

    async def get_children(self, node_id: str, *, timeout: float = 5.0) -> list[SnrNode]:
        self._require_configured()
        payload = await self._get(f"/node/{node_id}/children", timeout=timeout)
        try:
            return [_to_node(n) for n in payload]
        except (KeyError, TypeError) as exc:
            raise SaveRestoreQueryError(f"Unexpected save-and-restore children response shape: {exc}") from exc

    async def get_configuration(self, config_id: str, *, timeout: float = 5.0) -> SnrConfiguration:
        self._require_configured()
        payload = await self._get(f"/config/{config_id}", timeout=timeout)
        try:
            pv_list = [
                SnrPvEntry(
                    pv_name=pv["pvName"],
                    readback_pv_name=pv.get("readbackPvName"),
                    read_only=bool(pv.get("readOnly", False)),
                )
                for pv in payload["pvList"]
            ]
            return SnrConfiguration(unique_id=payload["uniqueId"], pv_list=pv_list)
        except (KeyError, TypeError) as exc:
            raise SaveRestoreQueryError(f"Unexpected save-and-restore config response shape: {exc}") from exc

    async def get_snapshot(self, snapshot_id: str, *, timeout: float = 5.0) -> SnrSnapshot:
        self._require_configured()
        payload = await self._get(f"/snapshot/{snapshot_id}", timeout=timeout)
        try:
            items = [_to_snapshot_value(item) for item in payload["snapshotItems"]]
            return SnrSnapshot(unique_id=payload["uniqueId"], items=items)
        except (KeyError, TypeError) as exc:
            raise SaveRestoreQueryError(f"Unexpected save-and-restore snapshot response shape: {exc}") from exc


    async def resolve_folder(self, folder: str, *, timeout: float = 5.0) -> tuple[str, str]:
        """(node id, path) of a folder given as a path ("BTF_CONF/Magnets") or a node id.

        Folders are never created here: a missing one is an error listing
        what exists at the level where the path stopped matching.
        """
        self._require_configured()
        text = (folder or "").strip()
        if not text or text == "/":
            raise SaveRestoreQueryError("A folder is required (configurations can't live in the root).")
        if "/" not in text.strip("/") and len(text) == 36 and text.count("-") == 4:
            node = await self.get_node(text, timeout=timeout)
            if node.node_type != "FOLDER":
                raise SaveRestoreQueryError(f"{text} is a {node.node_type}, not a folder.")
            return node.unique_id, node.name
        parent, walked = ROOT_FOLDER_ID, []
        for part in [p for p in text.strip("/").split("/") if p]:
            children = [c for c in await self.get_children(parent, timeout=timeout) if c.node_type == "FOLDER"]
            match = next((c for c in children if c.name == part), None) or next(
                (c for c in children if c.name.lower() == part.lower()), None
            )
            if match is None:
                here = "/" + "/".join(walked) if walked else "the root"
                names = ", ".join(sorted(c.name for c in children)) or "none"
                raise SaveRestoreQueryError(f"No folder {part!r} in {here}. Folders there: {names}.")
            parent = match.unique_id
            walked.append(match.name)
        return parent, "/" + "/".join(walked)

    async def create_configuration(
        self,
        folder_id: str,
        name: str,
        pv_list: list[SnrPvEntry],
        *,
        description: str = "",
        timeout: float = 10.0,
    ) -> SnrNode:
        """A new configuration in folder_id, owned by the caller."""
        self._require_configured()
        body = {
            "configurationNode": {"name": name, "nodeType": "CONFIGURATION", "description": description or name},
            "configurationData": {
                "pvList": [
                    {
                        "pvName": pv.pv_name,
                        **({"readbackPvName": pv.readback_pv_name} if pv.readback_pv_name else {}),
                        "readOnly": pv.read_only,
                    }
                    for pv in pv_list
                ]
            },
        }
        payload = await self._request("PUT", "/config", params={"parentNodeId": folder_id}, json=body, timeout=timeout)
        try:
            return _to_node(payload["configurationNode"])
        except (KeyError, TypeError) as exc:
            raise SaveRestoreQueryError(f"Unexpected save-and-restore configuration response shape: {exc}") from exc

    async def take_snapshot(
        self, config_id: str, name: str, comment: str, *, timeout: float = 30.0
    ) -> tuple[SnrNode, list[SnrSnapshotValue]]:
        """Read the configuration's PVs (the service does it) and save them as a snapshot owned by the caller."""
        self._require_configured()
        items = await self._request("GET", f"/take-snapshot/{config_id}", timeout=timeout)
        if not isinstance(items, list):
            raise SaveRestoreQueryError("Unexpected take-snapshot response shape.")
        body = {
            "snapshotNode": {"name": name, "nodeType": "SNAPSHOT", "description": comment or name},
            "snapshotData": {"snapshotItems": items},
        }
        payload = await self._request("PUT", "/snapshot", params={"parentNodeId": config_id}, json=body, timeout=timeout)
        try:
            node = _to_node(payload["snapshotNode"])
        except (KeyError, TypeError) as exc:
            raise SaveRestoreQueryError(f"Unexpected save-and-restore snapshot response shape: {exc}") from exc
        return node, [_to_snapshot_value(item) for item in items]


def _refusal(exc: httpx.HTTPStatusError, method: str, path: str) -> SaveRestoreRefusedError:
    try:
        body = exc.response.json()
        reason = body.get("message") or body.get("error") if isinstance(body, dict) else exc.response.text
    except ValueError:
        reason = exc.response.text
    reason = (reason or exc.response.reason_phrase or "").strip()[:300]
    status = exc.response.status_code
    message = f"Save-and-restore refused {method} {path.split('?')[0]} (HTTP {status}): {reason}"
    if status in (401, 403):
        message += (
            " - creating needs the signed-in user's token (LibreChat with OPENID_REUSE_TOKENS and "
            "forwardUserToken), and the user must be allowed to write here."
        )
    return SaveRestoreRefusedError(message)


def _to_node(payload: dict) -> SnrNode:
    return SnrNode(
        unique_id=payload["uniqueId"],
        name=payload["name"],
        node_type=payload["nodeType"],
        description=payload.get("description"),
        created=_epoch_ms_to_dt(payload.get("created")),
        last_modified=_epoch_ms_to_dt(payload.get("lastModified")),
        user_name=payload.get("userName"),
    )


def _to_snapshot_value(item: dict) -> SnrSnapshotValue:
    config_pv = item.get("configPv", {})
    value_field = item.get("value") or {}
    return SnrSnapshotValue(
        pv_name=config_pv.get("pvName", ""),
        value=value_field.get("value"),
        readback_value=(item.get("readbackValue") or {}).get("value"),
        units=(value_field.get("display") or {}).get("units"),
        severity=(value_field.get("alarm") or {}).get("severity"),
    )


def _epoch_ms_to_dt(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000)
