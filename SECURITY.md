# Security and privacy

The local OS owner is the administration boundary. No model-exposed management
tool, owner-id argument, inherited environment variable or hook text establishes
human intent. Native intake resolves only the named profile session through a
read-only SQL adapter bound to schema 32. Creation source, routing source, user,
parent and native receipts determine admission; hook/model producer labels do
not. Classic local CLI/TUI with no user ID requires explicit local-owner mapping.
Unknown sources fail closed; no history enumeration or private core import is used.
Callback epoch and identity are rechecked atomically at ingest, including across
forget followed by resume. Principal and human/assistant responsibility are separate.


Integration subprocess environments exclude inherited credentials and route
configuration; only essential OS environment keys are copied.

State is profile-local in `continuity/`: directory 0700, SQLite and managed exports
0600, secure-delete and explicit vacuum on forget. Symlink/traversal paths are
rejected. All release and integration fixtures are synthetic. SQLite coordinates
leases, dedupe, source revisions, pause epochs, artifacts and atomic budgets.
Network/model calls occur outside database transactions. Fresh fenced checks follow adapter selection immediately before read admission
and precede publication; closed records are not reopened by model proposals.

Extraction accepts only complete turns within 24,000 serialized characters,
including linked parent context. Larger admitted sources remain blocked intact for
explicit splitting; they are not silently truncated or marked processed. Admission
has a 1 MiB hard limit and 256 outstanding jobs. All attempted PluginLlm requests
across extraction/drafting/check-in synthesis share the execution's live Hermes
`agent.max_turns` limit. There is no plugin-owned daily inference quota.
Output remains bounded to at most 1500 requested tokens, four jobs per tick and
three claim attempts per job. Failed provider/parse calls consume inference
turns; exhausted work is durably blocked, not silently recycled in a new tick.
These are plugin invocation/agent-turn limits: host/provider internal network
retries and fallback may create additional billable attempts; no physical HTTP
attempt cap, monetary hard cap or provider token enforcement beyond the requested
maximum is claimed. JSON validation is mandatory in plugin
code regardless of host optional dependencies. Event text is untrusted data. Model consent can be
revoked independently of read preparation consent.

Shadow never dispatches connectors or sends. Prepare mode executes explicitly selected read adapters and generates local
Markdown drafts for bounded assistant writing tasks. The model has no tools. Host tool mappings require host metadata explicitly
advertising `annotations.readOnlyHint: true` and exact trusted
`continuityScopedRead: {tool: NAME, args: EXACT_ARGS}`, no destructive hint, a fixed name
and arguments, approved read selectors, bounded results and recursive closed
schemas. A tool's implementation and its capability declaration are a trust
boundary; review the host provider before authorizing it. Tools lacking this
metadata are unsupported rather than presumed safe from their names. No automatic
credential discovery or secret config is added. Source text can reach the host
model only after explicit model consent. No native memories are modified.

The outbox protocol is verified with injected synthetic transports. Version 0.2
includes an explicitly opted-in fixed own-destination send route; live acceptance
remains separate. The policy uses quiet hours 09:00–21:00 and has no global
daily delivery cap or contact cooldown. The initial autonomous wellbeing check-in
alone retains one delivery reservation per local day, including uncertain attempts. Receipt acknowledgment never marks an item done, delivered or read.
Recovery changes unresolved `sending` to `unknown`, never a blind retry. An
external call already in flight cannot be recalled atomically with SQLite pause;
future live transport work must preserve that uncertainty boundary and obtain
separate authorization. No exactly-once external delivery claim is made.

Forget pauses/revokes and purges managed logical data, draft files, exports,
references and mappings before scheduler cleanup. Scheduler list/remove failure
cannot prevent purge; only a cleanup ownership token is retained on failure. It does
not erase Hermes history, host cron logs, provider records, copied exports,
backups or forensic storage remnants. Budgets are purged; pause epoch and a
technical pending ticker cleanup token remain. File cleanup errors are reported
after logical purge commits. Markdown reads walk every ancestor from a root descriptor
with O_NOFOLLOW and verify the configured instance root identity. Retention is explicit owner forget in this release; there
is no automatic 30-day retention policy. Keep synthetic evaluation data separate
from personal profiles and apply your own backup retention.

POSIX file locks protect ticker lifecycle operations; concurrent processes share
SQLite claims and budgets. Only the exact owned job name, unpredictable token,
and script match cleanup identity; setup additionally validates local delivery,
no-agent mode, enabled state, empty prompt and the exact schedule. Disable first makes the script inert;
remove before uninstall cleans ownership. Removing a package manually cannot
unschedule a host job, so use the documented cleanup procedure. A down host cannot
report its own outage; no external watchdog is included.

Version 0.2.0 adds an explicit own-destination opt-in. Active Worker ticks call the
coordinated outbox automatically; shadow/prepare never send. CLI code owns the
fixed destination, executable/action allowlists and notice text. A send receipt is
only acknowledgment. Claims expire after 60 seconds and unknown outcomes require
owner reconciliation. A pre-effect revocation check cannot recall an effect already
in flight. See docs/INTEGRATIONS.md for bounds and deactivation.

Conversational extraction accepts a typed assertion as a contiguous subspan of
its proposal quote without rewriting the response. Assertion semantics alone
cannot authorize closure: a complete fresh authenticated ES/EN canonical
confirmation and one uniquely eligible live commitment are also required.
Quoted, negated, uncertain or unsupported language remains a proposal with
review retained. Multiple candidates require OS-owner CLI exact item/revision
control. There is no parent cascade or external side effect from closure.
Manual owner close remains available. No Astra approval is claimed.

Configuration permission changes advance the authorization generation. Queued
read/draft jobs retain their admission generation; stale jobs and registries are
denied. Actual capability parameters must match current config. Dispatch threads
and model disclosure recheck at a logical admission point shared with revocation;
SQLite write locks are released before network calls. Already admitted operations
are in flight and cannot be recalled by later revocation.

Direct native hook trust and per-turn provenance remain a host acceptance limit:
session creation alone does not prove every subsequent producer. Queued native
admissions without an authenticated producer class are rejected regardless of
principal names. The direct route relies on the trusted native event bus; see
[issue matrix](docs/REVIEW-MATRIX.md). OS owner controls data-directory ancestors;
malicious same-UID code, hardlinks and mount substitution are outside the local
file confinement guarantee.
