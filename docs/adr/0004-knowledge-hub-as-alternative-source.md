# ADR 0004: The ARGUS Knowledge Hub as an alternative source for inventory, documentation and logbook

## Status

Accepted.

## Context

Three kinds of knowledge reach ARGUS from three places today:

- the **device inventory** (`list_beamline_devices`) is parsed from each
  beamline's epik8s deploy YAML, read from git;
- **documentation** (`search_documentation`, `diagnose_device`) comes from a
  local TF-IDF index or RAGFLOW;
- **logbook reads** (`get_alarm_history`, `diagnose_device`,
  `machine_summary`) come from Olog.

The ARGUS Knowledge Hub (argus-knowledge-hub) now holds all three for each
facility: it imports the same epik8s configuration as Control Device
records, keeps controlled documentation with a knowledge index, and mirrors
each Olog as Logbook Entry documents. It serves them as an MCP server,
scoped by workspace, behind a personal access token. Whether answers built
on the hub are as good as answers built on the raw sources is an open
question, and the way to answer it is to ask both.

## Decision

Add the hub as an alternative source behind the existing service and
provider protocols, one independent switch per area:

| Setting | Legacy | Knowledge Hub |
|---|---|---|
| `INVENTORY_BACKEND` | `yaml` (`BeamlineInventoryService`) | `knowledge_hub` (`KnowledgeHubInventoryService`) |
| `DOCUMENTATION_BACKEND` | `local` / `ragflow` | `knowledge_hub` (`KnowledgeHubDocumentationProvider`) |
| `LOGBOOK_BACKEND` | `olog` (`LogbookProvider`) | `knowledge_hub` (`KnowledgeHubLogbookProvider`) |

All three share one `KnowledgeHubClient`, which talks to the hub as an MCP
**client** (the protocol LibreChat uses), not through its REST API — so
ARGUS receives exactly the records, links and restricted-record redaction
any other assistant gets. Tool names and result shapes are unchanged; only
the `list_beamline_devices` description and schema follow the source (no
`repo`/`ref` for the hub, an optional `workspace` instead), and
`list_knowledge_hub_workspaces` is added when the hub is configured.

Workspaces: every hub tool works in one workspace. ARGUS fans each search
out across `KNOWLEDGE_HUB_WORKSPACES`, or across every workspace the token
may read when that is blank. An administrator's personal access token with
the `admin` scope and no fixed workspace reads every workspace.

## Consequences

- A YAML-backed and a hub-backed ARGUS can run side by side from the same
  image (epik8s-platform's `aiPlatform.argusMcpInstances`) and be compared
  question by question.
- Real differences between the sources surface as different answers: the
  hub names a device by `name` (the YAML source prefers `alias`), reflects
  the last imported revision rather than the branch HEAD, and includes
  corrections people made in the hub.
- The hub is read-only: `create_logbook_entry` still posts to Olog.
- Logbook reads cost one `get_document` per candidate entry (the hub's
  document tools don't return Olog's date/tags as fields; they are parsed
  from the entry's header), capped per workspace — a 24 h alarm count on a
  busy logbook is a lower bound.
- Inventory listing pages with `search_objects(offset, with_attributes)`.
  A hub without those arguments still works, at one `get_object` per device
  and with an explicit "truncated" note past 100 devices.
