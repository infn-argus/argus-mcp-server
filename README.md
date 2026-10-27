# ARGUS

ARGUS is an MCP server that gives an AI assistant a **unified operational
interface** to an accelerator control room — not just EPICS. A single tool
call like `diagnose_device("QF12")` transparently pulls together live PV
values, archived trends, Kubernetes pod status, ArgoCD deployment state,
recent logbook entries, and documentation, and returns one structured
report. The LLM never needs to know (or ask) which backend answered.

ARGUS started as a fork of INFN's `epics-mcp-server` (a 3-tool EPICS-only
proof of concept) and has been rebuilt into a modular
**provider → service → tool** architecture. See
[docs/architecture.md](docs/architecture.md) for the full picture.

## Why not just expose EPICS functions as tools?

Because a control room isn't just PVs. A device like quadrupole QF12 is a
PV group, an IOC, a Kubernetes pod, a GitOps deployment, a maintenance
history, and a stack of documentation — and answering "what's wrong with
QF12?" means correlating all of that. ARGUS exposes ~18 high-level,
intent-shaped tools instead of hundreds of thin per-backend wrappers; each
tool decides internally which of EPICS, the Archiver Appliance,
ChannelFinder, Kubernetes, ArgoCD, Elasticsearch, the electronic logbook, or
local documentation to query (in parallel, with per-backend timeouts and
graceful degradation).

## Quickstart

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # at minimum, set EPICS_CA_ADDR_LIST
.venv/bin/python -m argus --transport stdio
```

Every provider besides EPICS is entirely optional — the server boots fine
with just `EPICS_CA_ADDR_LIST` set; unconfigured providers report themselves
as `unconfigured` rather than erroring, and `diagnose_device()` still
returns a graceful partial report. See [.env.example](.env.example) for
every provider's config, and [docs/providers.md](docs/providers.md) for what
each one talks to.

Serve over SSE instead of stdio:

```bash
.venv/bin/python -m argus --transport sse --host 0.0.0.0 --port 8000
```

## Backward compatibility

The original 3 tools still exist, unchanged, so existing clients (including
the example scripts under `test/`) work with zero changes:

- `get_pv_value(pv_name)` → `{"status": "success", "value": ...}`
- `set_pv_value(pv_name, pv_value)` → `{"status": "success", "message": ...}`
- `get_pv_info(pv_name)` → `{"status": "success", "info": {...}}`

`get_pv`/`set_pv` are the new, richer equivalents (include timestamp and
alarm severity); the legacy names are thin aliases with byte-identical
input schemas, enforced by
`tests/tools/test_pv_tools_backward_compat.py`.

## Tools

| Tool | What it does |
|---|---|
| `get_pv` / `set_pv` / `get_pv_info` | Read/write/inspect a single PV |
| `get_pv_value` / `set_pv_value` | Legacy aliases of `get_pv`/`set_pv` |
| `search_pvs` | Search PVs/channels by name, tag, or property (ChannelFinder) |
| `get_device` | Full device metadata: PVs, IOC, pod, namespace, rack, owner, docs |
| `device_status` | Quick health snapshot: live PVs + pod status |
| `diagnose_device` | Full cross-system diagnostic report, one call |
| `beamline_status` | Aggregate status across a group of devices |
| `get_history` | Historical trend data for a PV (Archiver Appliance) |
| `get_alarm_history` | Recent alarm-tagged logbook entries for a device |
| `create_logbook_entry` | Post a new entry to the operations logbook (as the signed-in user, when their token is forwarded) |
| `get_logbook_entry` | Read one logbook entry straight from Olog, by id or hub code (`OLOG-SPARC-1234`) |
| `update_logbook_entry` | Change an existing logbook entry (as the signed-in user); Olog keeps the old version |
| `get_logs` | Search historical application/IOC logs (Elasticsearch) |
| `list_iocs` | List IOCs and their Kubernetes pod status |
| `restart_ioc` | Restart an IOC's pod |
| `machine_summary` | Machine-wide IOC health + recent alarm counts |
| `execute_procedure` | Run a named, pre-approved operational procedure (scaffolded; no procedures ship yet — see [ADR context](docs/adr/)) |
| `search_documentation` | Search technical documentation (local TF-IDF by default, or RAGFLOW — see [providers.md](docs/providers.md)) |
| `list_beamline_devices` | Structured device inventory (IOCs, zones, devgroup/devfunc) parsed from a beamline's deploy YAML, or read from the ARGUS Knowledge Hub |
| `list_knowledge_hub_workspaces` | The ARGUS Knowledge Hub workspaces this server reads from (only when the hub is configured) |
| `get_config_history` | Read a beamline config file's current content, git history, or a commit's diff (GitHub/GitLab) |
| `search_knowledge_base` | Search issues/tickets across configured repos for past problems and resolutions (GitHub/GitLab) |
| `search_snapshots` | Search/browse Phoebus save-and-restore configurations and snapshots (read-only) |
| `get_snapshot` | Read a save-and-restore configuration's PV list, or a snapshot's saved values (read-only) |
| `create_saverestore_configuration` | Create a save-and-restore configuration from devices' setpoint (+ readback) PVs, as the signed-in user |
| `take_saverestore_snapshot` | Snapshot an existing configuration (the service reads the PVs), as the signed-in user; never restores |

## Knowledge source: legacy YAML/RAG/Olog, or the ARGUS Knowledge Hub

The device inventory, documentation and logbook reads can each come from
the [ARGUS Knowledge Hub](https://infn-argus.github.io/argus-knowledge-hub/)
MCP server instead of the beamline's deploy YAML, a RAG and Olog:

```bash
INVENTORY_BACKEND=knowledge_hub
DOCUMENTATION_BACKEND=knowledge_hub
LOGBOOK_BACKEND=knowledge_hub
KNOWLEDGE_HUB_URL=https://argus-hub-api.example.org/mcp
KNOWLEDGE_HUB_TOKEN=argus_pat_...     # see below
KNOWLEDGE_HUB_WORKSPACES=btf         # blank = every workspace the token may read
```

The token is a personal access token made in the hub (*My account →
Personal access tokens*). To read **every** workspace, an administrator
makes it with the `read` and `admin` scopes and without fixing it to a
workspace; a token with only `read` reads the workspaces its owner holds a
role in. Tool names and result shapes are the same either way, so a
YAML-backed and a hub-backed server can answer the same question side by
side — see [ADR 0004](docs/adr/0004-knowledge-hub-as-alternative-source.md).

## Confirmed writes

Write tools (`set_pv`, `set_pv_value`, `restart_ioc`, `execute_procedure`,
`create_logbook_entry`, `update_logbook_entry`,
`create_saverestore_configuration`, `take_saverestore_snapshot`) can be confirmed in two
steps, for clients that send `X-Argus-Confirm-Writes: true` (LibreChat's
registrations do; it has no approval step of its own). The first call
changes nothing: it returns `confirmation_required`, a plain-language
preview, and a one-time `confirmation_id`. Only the same call repeated with
that id, by the same user, within 10 minutes, runs. Clients without the
header (the voice agent, which has its own operator confirmation) are
unaffected, and their tool schemas are unchanged. See
`argus/mcp_server/confirmation.py` for what this does and doesn't
guarantee.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Tests never require a running EPICS/Kubernetes/ArgoCD/etc. instance — every
backend is mocked. See [docs/developer-guide.md](docs/developer-guide.md)
for how to add a new provider or tool, and
[docs/class-diagram.md](docs/class-diagram.md) for the object model.

## EPICS Channel Access, for local testing

```bash
softIoc -d ~/EPICS/DB/test.db
caget temperature:water
caput temperature:water 100
```

then point ARGUS at it with `EPICS_CA_ADDR_LIST=127.0.0.1` in `.env`.

## Deploying

- **Docker**: `docker build -t argus .` — runs `python -m argus --transport stdio` by default; override the command for SSE.
- **Smithery**: `smithery.yaml` launches `python -m argus --transport stdio`.
