# Configuration

Start with a separate Hermes profile. The plugin defaults to shadow mode and no delivery. Loading does not create a schedule or scan chat history.

## Separate permissions

1. Map exact authenticated surface identities to a principal with `setup --identity SURFACE:IDENTITY=PRINCIPAL`. Explicitly selected aliases may share a principal; display names are not authentication.
2. Choose a timezone with `setup --timezone UTC` or another IANA timezone.
3. Authorize model inference separately with `setup --allow-model`. Conversation and selected context can reach the configured model provider.
4. Select read adapters and bounded context through the documented JSON mappings. An installed connector is not blanket read consent.
5. Opt into automatic hook intake through the profile setting `plugins.entries.hermes-continuity.settings.collect_references`, only after mapping actual native identities. Unsupported native producers remain rejected.
6. Enable the single ticker with `setup --ticker` only when ready. Its 15-minute cadence is review precision, not an exact-time delivery promise.
7. Set an exact owner delivery target and separately enable delivery only after reviewing the outbox behavior. Never select a recipient from model text.

Use `hermes continuity setup --help` for the installed host's command options. Check `status`, `why`, `capabilities`, and `checkin-status` after changes. Do not clear counters or toggle permissions to retry unknown sends.

## Workflow-specific quality

A contextual check-in configuration may add a `quality_contract` with a purpose and selected facets. For example:

```json
{
  "purpose": "wellbeing",
  "any_of": ["emotional_state", "energy", "rest", "support"]
}
```

Place that object inside your operator-authored check-in JSON; it is not a complete check-in configuration by itself. Apply the full reviewed file with `hermes continuity checkin-config ./approved-checkin.json`. Keep your principal, sources, calendars, and read permissions explicit.

Other workflows can select other purpose/facet values. No wellbeing setting is required by the generic lifecycle engine. Requirements enforce structured purpose and grounded quotations, not a guarantee that the model understood the human correctly.

See [CHECKINS.md](CHECKINS.md) for the complete configuration shape and [INTEGRATIONS.md](INTEGRATIONS.md) for adapter contracts. Use `remove` before disabling the plugin so owned scheduling is removed. Revocation fences new effects; an already admitted in-flight operation may still finish.
