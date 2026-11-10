# Explicit read integration contract

No mem0 or universal connector availability is claimed. An explicit bounded Calendar
read fallback through the installed Composio CLI and fixed-destination Hermes
output are supported in 0.2.0; existing host connections remain owner-managed.
The core needs none. Existing host tools are discoverable without reading account
contents using `hermes continuity capabilities` (registered tool metadata plus
supported mapping types). An owner reviews and explicitly maps a tool and scope.
The model never selects a tool or modifies its parameters.

Use a JSON file with a list of entries, and pass it to `setup --adapters FILE`:

```json
[
  {"type":"markdown","role":"notes","scope":"selected","root":"/absolute/path/to/selected-notes"},
  {"type":"host-read","role":"calendar","scope":"work","mapping":{
    "tool":"approved_calendar_read",
    "args":{"calendar":"selected","action":"list"},
    "schema":{"type":"object","required":["events"],"additionalProperties":false,
      "properties":{"events":{"type":"array","maxItems":100,"items":{
        "type":"object","required":["id"],"additionalProperties":false,
        "properties":{"id":{"type":"string","maxLength":256}}
      }}}
    }
  }}
]
```

The Markdown root must already exist. Only relative `.md` paths contained within
that root work; traversal, symlinks and results above 64,000 bytes fail. No note is
written. Host read mappings additionally require the tool's registered schema to
advertise `annotations.readOnlyHint: true` without `destructiveHint: true`.
Absent tools, missing capability declarations, ambiguous results, response schema
errors and unknown operation selectors fail closed. Merely returning
`successful: true` is insufficient. Fixed arguments cannot be overridden at read
time. Read capability never permits create/update/delete. This intentionally
excludes mixed tools without a trustworthy read-only wrapper.

Schemas support closed object properties/required, arrays/items/maxItems,
strings/maxLength, enum and scalar type checking; unsupported schema forms are
rejected. Host reads have a ten-second caller timeout; an in-flight timed-out read does
not yield an artifact or trigger a concurrent retry of that mapping in the same
process. Providers must supply their own reliable cancellation/timeout behavior.
Maximum response JSON length is 64,000 characters, nested depth 20 and
array length 1000. Context is always marked context-only, even if it contains
claims of receipt, booking or task completion. Stale memories cannot reopen an
item or grant permissions.

`prepare ITEM --revision N --role ROLE --scope SCOPE [--path FILE]` queues bounded
read preparation. `tick` requires explicit prepare mode and configured read consent, checks item revision
before access and again before publication, and writes a local evidence artifact.
Failed connectors affect only dependent jobs; other queued work can continue.
Three failed attempts lead to a visible blocked job; the owner can create a new
item/revision or reconfigure before explicitly queuing new preparation. There is
no automatic credential prompt. Replacing the adapter list with `[]` revokes new
reads. In-flight reads may finish, but a pause or item revision prevents stale
artifact publication. Returning to shadow also fences read publication.

Tool metadata is a provider declaration, not a proof about arbitrary code. Only
map providers you trust. The native contract test registers an actual synthetic
host read tool and dispatches through PluginContext, and the ticker subprocess
performs an actual scoped Markdown read. No live account validation was run.


## Automatic drafting and native source provenance

`setup --preparation-context FILE` selects up to four exact role/scope/path entries
for automatic draft jobs. `[]` needs no integrations. In prepare mode with model
consent, explicit bounded writing tasks enqueue tool-free generation after
extraction. Notes, memory and calendar are context roles; no native account
connector is assumed. Missing selected adapters block that job, rather than
silently broadening scope. Generated Markdown files live in continuity/drafts,
mode 0600, with source/revision, context digests and content digest in SQLite.
Due reviews verify the referenced file's bytes; missing/altered promised files
produce an incident and next step, never completion. A local draft is not delivery.

Native hooks follow the documented `observer-hooks` pre/post contract:
session_id, turn_id, user_message, sender_id/platform (when supplied) and
assistant_response for post. Production imports no private state modules. The
SQL adapter opens only this profile's state.db using mode=ro/query_only and
requires `schema_version.version=32`, sessions, state_meta, session_admissions
and exact persisted messages with native ownership metadata. This is an explicitly
version-bound storage adapter, not a promise of a stable host API.

Live intake requires exactly one started admission for the named session, a
queue intent, canonical digest and `native_text_v1` server adapter envelope.
Producer, non-bot DM source, profile homes, connector, route, principal, platform
message ID, exact text and persistence ownership stamp must all verify. Explicit
identity aliases bind transport sender to the stored owner; a human session alone
is never authority. Missing/terminal-only admissions and classic CLI/TUI/GUI
without a supported per-turn protocol fail closed, even with a creation receipt.
Operator CLI management/diagnostic ingestion remains separate and explicit.

A hook's parent must match the persisted parent and pass canonical `_reset_from`
lineage validation, same route/origin and closed-ancestor checks. Arbitrary
children, branches, delegates, cron and automated producers remain rejected.
Post hooks retain configured aliases even when sender_id is omitted and require
an exact previously admitted PRE turn or a matching non-authoritative PRE witness.
The actual SDK invokes PRE before its turn-start SQLite flush. Only complete
absence of rows for that platform ID is deferrable; conflicting/duplicate rows,
invalid metadata and other proof failures are never pending witnesses. The
reference retains hashes, epoch and PRE time, not message text or admission
metadata in clear. POST reruns strict persisted-message proof and commits the
human/assistant pair plus jobs atomically, consuming that reference. Pause,
reconfiguration and forgetting revoke/purge references; expired references are
purged on Store access. Interrupted turns are not automatically replayed.
Inspecting zero events during the first new turn is not failure if its exact PRE
witness remains pending; confirm the pair after POST. Same-turn response
association is likewise unavailable until confirmation. Schema changes need an
adapter update.

Optional `NativeSource.recent(session_id, identities, principal=..., limit=20,
before_seq=None)` reads bounded started/terminal admission candidates from one
explicit session. Each returned user statement passes the same digest, producer
and exact persisted-message checks; it is not a transcript scan or backfill.
`host.recent_history(...)` additionally fences pause, epoch and identity changes,
and collection consent when a PluginContext is present. Consumers must retain
their own selected-read permission and recheck authorization before later work.
A history read or replay is not proof of a newly received automatic callback.

The adapter uses documented storage and the installed schema definition as its
compatibility contract. Native tests construct SessionDB through the host and
invoke actual PluginManager hooks; they do not substitute manual CLI ingest for
hook intake. Setup/cron use supported PluginContext and registry APIs. Runtime
children use HERMES_DISABLE_LAZY_INSTALLS=1 with already installed dependencies,
preventing concurrent dependency bootstrap rather than relaxing timeouts.

## 0.2.0 fixed Calendar CLI fallback

If the host registry cannot expose the existing connection, select the public
installed `composio execute` CLI. The sole code allowlisted action is
`GOOGLECALENDAR_FIND_EVENT`; create/update/delete, proxy, run, arbitrary executables
and arbitrary parameters are unavailable. No account discovery or linking occurs.
The parent/operator must confirm the action schema against their installed CLI;
this repository's tests simulate provider responses and do not verify a live account.

```json
[{"type":"composio-read","role":"calendar","scope":"personal-window",
  "mapping":{"action":"GOOGLECALENDAR_FIND_EVENT","account":"OWNER_ACCOUNT_ALIAS",
    "calendar_id":"primary","time_min":"2026-11-02T00:00:00Z",
    "time_max":"2026-11-03T00:00:00Z","max_results":5}}]
```

`setup --adapters FILE --preparation-context CONTEXT_FILE --mode prepare`
configures this for automatic Worker draft jobs, alongside an authorized Markdown
note. A context selector is `{"role":"calendar","scope":"personal-window"}`.
The adapter fixes account/calendar/window outside model control; the aware window
is at most 31 days and the result cap is 1–20. It passes calendar_id, time_min,
time_max, max_results through argv JSON, with shell=False and a 15-second timeout.
It requires exit 0, successful=true and bounded items/events or event_data data,
returning only summary/start/end/status. CLI-managed authentication is reused
without reading keys. Public executables are discovered through PATH and the
selected absolute executable is retained for the operation; no developer-specific
installation path is required.

## 0.2.0 own-destination delivery

The owner asserts that the exact `platform:chat_id` is their own chat. The program
cannot establish ownership from an identifier; it does not discover recipients.
No model output can set platform, target, tool, action, account or window.

```sh
hermes continuity setup --delivery-target whatsapp:OWN_CHAT_ID --allow-delivery --mode active
hermes continuity tick
hermes continuity setup --deny-delivery --mode prepare
```

These commands are a parent/operator runbook, not evidence of live execution.
Use an already prepared fresh artifact or a due review, during 09:00–21:00 in
the configured timezone. `active` permits the same read/draft work as `prepare`,
then automatically runs the coordinated outbox. `shadow` only proposes/reviews;
`prepare` reads/drafts locally; neither dispatches. Merely selecting active never
grants delivery consent. `send-pending` runs the same bounded delivery path
manually; it does not override consent, mode or policy.

At most one ordinary notice is acknowledged per tick (the separate daily check-in
may also deliver). There is no global daily send cap or global contact cooldown.
Only the initial wellbeing check-in has one attempted send per local day;
uncertain attempts consume it. Artifacts/notices expire
after 24 hours; item revision, artifact contents/file hash, pause epoch, destination
and consent are checked before the effect. Text is code-authored, includes a local
item reference and indicates review is due or a draft is ready. Model text is never
passed to the send CLI, avoiding attachment/mention/routing syntax. The public
command uses fixed argv, stdin text, shell=False and a 30-second timeout.
Success means acknowledged only, never delivery/read or task completion.

Outbox claims last 60 seconds. Recovery marks only expired sending claims unknown,
leaving another worker's live claim intact. Unknown outcomes are never blindly
retried for that item revision; reconcile with the host/provider before explicit
owner action. Pause or revoke cannot recall an effect already admitted/in flight.
The check-in attempt fence is plugin-local, not a cap on other Hermes routines. Deactivation
retains evidence and fences planned effects; default delivery is disabled.


Authorization starts at a generation-checked logical admission shared with revocation. An already admitted operation is in flight and cannot be cancelled by pause. Network execution holds no SQLite write transaction. Configured capabilities must match actual adapter type and exact arguments, not registry labels. Generic host-read tools require a trusted descriptor annotation `continuityScopedRead: {tool: NAME, args: EXACT_ARGS}` in addition to readOnlyHint; a hint alone is refused. This attestation is not a supported contract for arbitrary installed tools. Use code-reviewed bounded Composio reads or scoped Markdown where available. CLI Markdown configuration persists device/inode root identity. OS owner must protect data-directory ancestors; same-UID malicious code is not isolated.
