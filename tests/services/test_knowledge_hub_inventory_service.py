import respx

from argus.services.knowledge_hub_inventory_service import KnowledgeHubInventoryService
from tests.providers.knowledge_hub.fake_hub import URL, FakeHub, hub_client


def _device(i: int, name: str, system: str = "mag", **attributes):
    return {
        "uid": f"u{i}",
        "key": f"BTF:DEV:danfysik-1:{name}",
        "name": name,
        "type": "Control Device",
        "link": f"[{name}](https://hub/assets/u{i})",
        "attributes": {
            "system": system,
            "devtype": "sys8x00",
            "ioc": "danfysik-1",
            "pv_prefix": f"BTF:MAG:DANFYSIK:{name}",
            "zones": ["TL"],
            "geo": 53,
            **attributes,
        },
    }


def _paging_hub(devices):
    """A current hub: pages with offset and returns attributes with_attributes."""

    def search_objects(args, ws):
        assert args["types"] == ["Control Device"]
        offset, limit = args.get("offset", 0), args["limit"]
        page = devices[offset : offset + limit]
        if not args.get("with_attributes"):
            page = [{k: v for k, v in d.items() if k != "attributes"} for d in page]
        return {"total": len(devices), "by_type": {"Control Device": len(devices)}, "objects": page}

    return search_objects


@respx.mock
async def test_devices_are_mapped_like_the_yaml_source_with_key_pvs_and_links():
    hub = FakeHub({"search_objects": _paging_hub([_device(1, "QUATM002"), _device(2, "VGTB01", system="vac")])})
    respx.post(URL).mock(side_effect=hub)

    listing = await KnowledgeHubInventoryService(hub_client()).list_inventory(devgroup="mag")

    assert listing.source == "knowledge_hub" and listing.notes == []
    [quad] = listing.devices
    assert quad.name == "QUATM002"
    assert quad.ioc_name == "danfysik-1"
    assert quad.iocprefix == "BTF:MAG:DANFYSIK"
    assert (quad.devgroup, quad.devtype, quad.devfunc) == ("mag", "sys8x00", "QUA")
    assert quad.zones == ["TL"] and quad.geo == 53.0
    assert quad.key_pvs["current_readback"] == "BTF:MAG:DANFYSIK:QUATM002:CURRENT_RB"
    assert quad.workspace == "btf" and quad.link == "[QUATM002](https://hub/assets/u1)"


@respx.mock
async def test_every_page_is_read():
    devices = [_device(i, f"QUATM{i:03d}") for i in range(250)]
    hub = FakeHub({"search_objects": _paging_hub(devices)})
    respx.post(URL).mock(side_effect=hub)

    listing = await KnowledgeHubInventoryService(hub_client()).list_inventory()

    assert len(listing.devices) == 250 and listing.notes == []
    assert [c["arguments"]["offset"] for c in hub.tool_calls("search_objects")] == [0, 100, 200]


@respx.mock
async def test_an_older_hub_without_paging_gets_attributes_per_object_and_says_it_is_truncated():
    devices = [_device(i, f"QUATM{i:03d}") for i in range(150)]
    by_uid = {d["uid"]: d for d in devices}

    def old_search_objects(args, ws):
        # Unknown arguments (offset, with_attributes) are dropped, as the real hub's call() does.
        return {"total": 150, "objects": [{k: v for k, v in d.items() if k != "attributes"} for d in devices[:100]]}

    hub = FakeHub(
        {
            "search_objects": old_search_objects,
            "get_object": lambda args, ws: {"found": True, **by_uid[args["uid_or_key"]]},
        }
    )
    respx.post(URL).mock(side_effect=hub)

    listing = await KnowledgeHubInventoryService(hub_client()).list_inventory()

    assert len(listing.devices) == 100
    assert listing.devices[0].devgroup == "mag"
    assert len(hub.tool_calls("get_object")) == 100
    assert listing.notes == [
        "Workspace 'btf': only 100 of 150 control devices could be listed (the hub does not support paging "
        "search_objects with `offset`; upgrade the hub)."
    ]


@respx.mock
async def test_workspaces_are_fanned_out_deduplicated_and_narrowable():
    shared = _device(9, "DHRTB101")

    def search_objects(args, ws):
        own = [_device(1 if ws == "btf" else 2, "QUATM002" if ws == "btf" else "QUASP001")]
        return {"total": 2, "objects": own + [shared]}

    hub = FakeHub({"search_objects": search_objects}, workspaces=["btf", "sparc"])
    respx.post(URL).mock(side_effect=hub)
    service = KnowledgeHubInventoryService(hub_client())

    everything = await service.list_inventory()
    assert sorted(d.name for d in everything.devices) == ["DHRTB101", "QUASP001", "QUATM002"]

    only_sparc = await service.list_inventory(workspace="sparc")
    assert sorted(d.name for d in only_sparc.devices) == ["DHRTB101", "QUASP001"]


@respx.mock
async def test_a_workspace_never_imported_is_noted_and_git_arguments_are_said_to_be_ignored():
    hub = FakeHub(
        {"search_objects": lambda args, ws: {"total": 0, "objects": [], "unknown_types": ["Control Device"]}}
    )
    respx.post(URL).mock(side_effect=hub)

    listing = await KnowledgeHubInventoryService(hub_client()).list_inventory(
        repo="https://github.com/infn-epics/epik8s-btf.git"
    )

    assert listing.devices == []
    assert "repo/path/ref were ignored" in listing.notes[0]
    assert "has not been imported" in listing.notes[1]
