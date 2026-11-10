# Reproducible acceptance

The release harness verifies archive hashes, executes the complete suite from a
fresh extraction, runs native doctor/validate and CLI lifecycle commands, and
records each result in evidence/acceptance.json. All profiles, config, scheduling
and state in these tests are synthetic and isolated. Providers and transport
receipts are synthetic; no real account, history or secret is consulted.

`tests/native_vertical_case.py` runs via the actual native PluginManager and
PluginLlm structured serialization. Actual SessionDB sessions establish human,
cron and delegated provenance. Pre/post hooks admit the human request and linked
assistant promise. Extraction persists actor separately from principal, deadline,
review and artifact expectation; automatic preparation reads selected Markdown
and registered synthetic host calendar/memory providers. A separate tool-free model
call synthesizes useful Markdown into a private local file with provenance.
A GUI human hook then links a completion to the existing task/revision and closes
both linked records by CAS. No helper calls CLI ingest/prepare/close in that loop.
No work on the following tick makes a model call. Native ticker subprocess and
concurrent setup tests separately verify one owned cron and unrelated-job survival.

Additional regressions cover no-integration drafting, missing/altered promised
files, due open records, snooze, ambiguous/stale/receipt closures, malformed model
JSON, quote and ID spoofing, parent-task linkage, untrusted origins, machine
admissions inside human sessions, immutable creation-receipt mismatch,
epoch fencing across forget/resume, principal revocation at hook commit, lease
takeover, closure/pause during model work, pause during adapter selection, root
ancestor replacement, disconnected adapters, bounded budgets/backpressure,
unknown delivery recovery, and scheduler list/remove failure during forget.

RED and GREEN outputs are retained under evidence. The native repeated-setup
40-second child timeout is unchanged. Children use the selected runtime venv
and supported HERMES_DISABLE_LAZY_INSTALLS to avoid dependency provisioning and
bootstrap lock contention. Public launcher `--run-module unittest` and `timeit`
exercise the installed runtime, rather than a legacy incomplete environment.
The release excludes local evidence homes and installed dependency copies.

## Verification limits

A green synthetic suite establishes the controller behavior and integration
contracts, not broad language understanding. Fixture/model responses are labeled
synthetic. The production code calls host ctx.llm; it does not substitute a canned
product draft. Semantic linking, date interpretation and usefulness over multiple
days remain unverified by a human trial. Current native source support is schema
32, known mapped surfaces without parent linkage; no universal origin support is
claimed. Unsupported origins remain inert instead of gaining human authority.

The 0.1.0 verification performed no production external sends or writes to native
memory and exercised no live booking confirmation adapters. Receipt acceptance is not booking/delivery/read proof.
WhatsApp live delivery and cross-routine global caps remain unverified and require
separate consent. No default-profile installation, cron, config, vault or account
change was performed. POSIX is required; optional plugin-host isolation skips
native CLI registration and is unsupported for this management surface.

## 0.2.0 verification scope

The 0.1.0 results above are preserved historical evidence. Version 0.2.0 adds
synthetic public-send subprocess tests, automatic Worker outbox dispatch and
revoke, claim-safe recovery, artifact freshness and multilingual typed assertion
regressions, plus the fixed read-only Composio Calendar adapter in an active
Worker draft. No production mutation or real provider execution is part of these
checks. Parent owns the real provider corpus, note/Calendar integration and live
own-chat trial. Live verification remains open until that parent reports results.
The recovered architecture review and finding dispositions are recorded in
REVIEW-MATRIX.md; they do not certify a later tree or constitute final approval.

Delivery failures now emit a bounded code-owned reason and outbox ID. Raw
transport output, exception text and destination are not logged. A disconnected
bridge, timeout, malformed response or missing exact receipt leaves the intent
UNKNOWN; no diagnostic implies delivered/read or permits automatic resend.
The historical live canary remains unresolved and is not part of distributable
fixtures. A working archive is not permission to enable production sends.


Runtime audit and outstanding real acceptance: see [REVIEW-MATRIX.md](REVIEW-MATRIX.md). Synthetic coverage does not constitute Astra approval or real provider quality acceptance. Natural closure supports whole fresh ES/EN canonical completion declarations only when one live commitment is eligible. With multiple commitments or semantic uncertainty, review remains pending and local authenticated `continuity close ID --revision N` controls the exact item. There is no automatic parent cascade.
