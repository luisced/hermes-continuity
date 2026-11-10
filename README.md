# Hermes Continuity

Follow through on conversations in Hermes. Keep the goal, the next useful action, replies, and completion evidence together instead of turning every pending item into another reminder.

Continuity is a native, profile-local plugin. It uses a private SQLite queue and one optional ticker. The core does not require Composio, Obsidian, a particular messaging platform, or a wellbeing workflow.

## Follow through, not just notify

- Capture explicit requests and linked assistant promises from authorized conversations.
- Keep prepared, executed, sent, responded, and resolved evidence separate.
- Prepare a local draft, ask a useful question, wait for new evidence, or expose a supported closure.
- Preserve explicit reminders without sending a generic notice for every due item.
- Link replies across explicitly mapped surfaces; ambiguity does not silently close a task.
- Avoid another attempt for an unchanged subject after an acknowledged, in-flight, or unknown send.

A transport receipt is not proof of human reading or completion. An uncertain send is not automatically retried.

## Optional workflows

### Local drafts and meeting briefs

Bounded writing tasks can create private Markdown artifacts with source references and content hashes. Selected read adapters acquire context before the tool-free model synthesizes it. A ready notice does not mean the artifact itself was delivered.

### Contextual check-ins

Check-ins are opt-in. Timing follows selected calendar evidence and recent conversation within the configured local window. Purpose requirements live outside the general lifecycle engine: wellbeing is one example, not a requirement for other follow-ups.

The [wellbeing example](docs/examples/wellbeing-quality.json) requires grounded coverage of a selected facet. A shopping question or an arbitrary caller label does not establish verified wellbeing coverage. Semantic classification still depends on the model and can be wrong.

## Install

Requires an existing Hermes installation with native plugin hooks, CLI commands, scoped reads, PluginLlm, and cron support. Native conversational intake explicitly supports Hermes state schemas **32 and 33**. Unknown schemas fail closed. POSIX is required for the current lock and process implementation.

```sh
hermes plugins install luisced/hermes-continuity
hermes plugins enable hermes-continuity
hermes continuity status
hermes continuity capabilities
```

Installing or enabling the plugin does not authorize capture, model inference, source reads, messages, or a scheduler. Review [configuration](docs/CONFIGURATION.md) and [security](SECURITY.md) before opting in. Test in a separate Hermes profile first.

Loading creates no ticker, scans no historical conversations, and invokes no model. The default mode is shadow, with delivery disabled.

## Configure deliberately

Identity mappings, inference consent, selected adapters, and the owner's fixed delivery destination are separate controls. Never substitute a display name for authenticated identity.

```sh
# Local operator setup only; this is not automatic conversation authority.
hermes continuity setup --identity cli:local-owner=person --timezone UTC --mode shadow
hermes continuity list
hermes continuity why

# Later, after reviewing the model/data boundary:
hermes continuity setup --allow-model --mode prepare --ticker
```

The optional ticker reviews every 15 minutes. It is not a minute-exact reminder guarantee. One owned ticker handles all queued work; no extra cron is created per item.

Automatic capture requires a genuine native human admission for each turn, an exact persisted message, and an explicitly mapped identity. PRE can retain a non-authoritative reference; POST confirms it. A configuration alias alone does not make an unsupported surface automatically captured. See the [source contract](docs/INTEGRATIONS.md).

## Inspect and control

```sh
hermes continuity status
hermes continuity list
hermes continuity why
hermes continuity checkin-status
hermes continuity close ITEM --revision 0
hermes continuity snooze ITEM --revision 0 --until 1800000000
hermes continuity pause
hermes continuity resume
```

`status` separates actual transport acknowledgements, associated replies, and confirmed closure. Each goal has an evidence projection and a next action. See [general follow-through](docs/GENERAL-FOLLOWTHROUGH.md) for exact semantics and limitations.

Before disabling or uninstalling, remove owned scheduling:

```sh
hermes continuity remove
hermes plugins disable hermes-continuity
```

`forget` purges plugin-owned local data after pausing. It does not erase native chat history, provider records, copied exports, or backups. See [SECURITY.md](SECURITY.md).

## Repository layout

```text
plugin.yaml                 Native Hermes plugin manifest
__init__.py                 Plugin registration
continuity/                 Intake, lifecycle, linking, workflows, quality, outbox
scripts/                    Reproducible package build and acceptance harness
tests/                      Unit, integration, native-runtime and security cases
docs/                       Configuration, integration and acceptance guides
  examples/                 Optional workflow quality contracts
```

This repository follows the installable-plugin layout used by [Composio Connections](https://github.com/luisced/hermes-composio-connections). Continuity currently exposes CLI controls; it does not claim a Desktop or dashboard page.

## Development

Use an already installed Hermes runtime. No credentials, live providers, or real message destinations are needed for the default suite.

```sh
HERMES_DISABLE_LAZY_INSTALLS=1 hermes --run-module unittest discover -s tests -t . -q
python3 scripts/build_release.py
```

Tests use temporary stores and fixture model/transport boundaries. Optional provider tests are skipped unless explicitly enabled. The native harness exercises real host helpers and persistence; fixture-green results do not prove a particular deployment's live delivery or human usefulness.

See [development](docs/DEVELOPMENT.md) for package checks and public-source boundaries.

## Limits

- No external booking, purchase, refund, or account-mutation executor is added.
- Automatic intake is version-bound and requires verified per-turn provenance. Unsupported CLI, GUI, or messaging producers fail closed.
- No global daily send quota or global contact cooldown is imposed. Quiet hours, consent, deduplication, claim fences, and host execution limits still apply.
- The initial autonomous check-in has one attempted send per configured local day. An unknown outcome consumes that attempt.
- Workflow quality labels are structured model judgments, not independent proof of semantic correctness.
- Only selected, authorized integrations are read. Source text and model output never grant permissions.
- This is an installable Git repository, not a claim of admission to the official Hermes plugin catalog.

## Documentation

- [Configuration](docs/CONFIGURATION.md)
- [General follow-through](docs/GENERAL-FOLLOWTHROUGH.md)
- [Read adapters and source contracts](docs/INTEGRATIONS.md)
- [Contextual check-ins](docs/CHECKINS.md)
- [Acceptance harness](docs/ACCEPTANCE.md)
- [Security](SECURITY.md)

## License

[MIT](LICENSE)
