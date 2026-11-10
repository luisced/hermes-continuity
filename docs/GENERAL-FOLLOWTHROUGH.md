# General follow-through

## Implemented architecture

The existing Worker, Lifecycle, Linker, Checkins, typed WorkflowRunner and Outbox remain the execution path. `followthrough.py` supplies generic request contracts, meaningful-contact decisions, subject admission and readable evidence. `quality.py` validates workflow-selected purpose/coverage requirements; it does not require wellbeing. The wellbeing policy is an opt-in example in `examples/wellbeing-quality.json`, not a mandatory engine rule.

A request contract has `goal`, `closure`, `contact`, `purpose`, `subject`. Contracts are persisted separately, anchored to the admitted source and exact task quote. New extraction can supply them; authenticated legacy human requests are enriched by a real guarded Worker job when reviewed, with explicit model consent. Enrichment does not create a replacement task or alter its scheduled next review.

Review actions are `prepare`, `ask`, `wait`; evidence projection adds `close` when the selected closure criterion is established. Ordinary due items remain open and scheduled without manufacturing a generic notice. Assistant obligations prepare their promised draft or ask for needed information/approval. Explicit reminder contracts retain a task-specific reminder. Unknown transport outcomes never authorize retries.

`Lifecycle.snapshot` and `lifecycle-list` retain their compatible fields and add a `followthrough` projection: prepared, executed, sent, responded, resolved, evidence references, execution/delivery scope, goal, closure condition and next action. Verified execution currently means a checked private file, not an external reservation/refund. A send receipt establishes transport of a notice, never human receipt or goal completion. Reminder-only notice goals and grounded purpose responses have distinct closure projections; they do not manufacture an owner-confirmed `done` item.

`status` adds actual acknowledged delivery, associated response and owner-confirmed closure separately, plus item projections. New acknowledgements append `delivery-receipt:<outbox-id>` evidence with acknowledgement time; existing attempts and receipts are not rewritten. Legacy rows without timestamps fall back to historical attempt ordering. This does not reconstruct missing historical acknowledgement times.

## Acceptance matrix

All named tests below are in `tests/test_general_followthrough.py` and exercise persisted SQLite plus real Worker/Outbox paths, except the explicitly local CLI/receipt inspection slices. Fakes are at synthesis/transport boundaries; historical receipt fixtures model pre-existing rows.

| Acceptance | Exact test suffix / verified outcome |
|---|---|
| Initialized without integrations | `ordinary_due_item_has_no_generic_notice_without_integrations`: review retained, no send, wait |
| Explicit reminder | `explicit_reminder_survives_and_unchanged_subject_does_not_repeat`: one contact, unchanged subject waits after restart |
| Actionable content | `reminder_delivery_is_actionable_not_generic_due_notice`: task-specific notice, old unverified-completion boilerplate absent |
| Reusable non-wellness contract | `nonwellness_purpose_contract_uses_real_checkin_worker`: reservation purpose delivered |
| No label-only wellbeing coverage | `wellbeing_label_alone_does_not_turn_shopping_reply_into_coverage`: reply inspectable, not answered coverage |
| Cross-authorized-surface response | `refund_contextual_reply_from_another_authorized_surface_is_response_not_closure`: associated human update, goal open |
| Distinct evidence stages | `work_draft_execution_notice_and_goal_are_distinct_evidence`: prepared vs checked file vs notice sent vs response vs closure |
| Reminder-only closure | `notice_only_goal_resolves_on_receipt_but_not_an_underlying_reservation`: close projection, no invented owner confirmation |
| Structured response coverage | `grounded_purpose_response_closes_only_checkin_coverage`: grounded purpose evidence retained; underlying commitment open |
| CLI inspectability | `cli_status_separates_actual_delivery_reply_and_closure`: planned rows are not deliveries |
| Live synthesis instructions | `real_worker_requests_structured_contracts_not_implicit_generic_due_contact`: contract metadata actually requested |
| Cross-day subject dedupe | `unchanged_checkin_subject_does_not_repeat_on_new_day`: unchanged grounds do not become fresh contact just because date changes |
| Negative/future reply | `negative_future_contextual_reply_is_still_response_not_resolution`: response can be negative/future without completion |
| Explicit closure fence | `response_proposal_without_explicit_closure_cannot_close_goal`: completion-like response proposal cannot silently close |
| Automatic purpose integration | `worker_links_structured_purpose_response_without_wellness_special_case`: Worker pending_response applies generic reservation coverage |
| Legacy identity preservation | `old_receipt_without_subject_metadata_cannot_gain_new_attempt`: old receipt and identity unchanged |
| Old generic notices | `legacy_review_notice_receipt_is_not_renewed_by_contract_metadata`: metadata does not refresh a historical send |
| Wellbeing example negatives | `wellbeing_example_rejects_food_shopping_or_uncovered_purpose`: structured purpose/facet rejection, no keyword matching |
| Unknown-never-retry | `unknown_reminder_never_retries_after_review_or_restart`: one uncertain attempt remains unchanged |
| Concurrent ambiguity | `new_ambiguous_request_during_response_model_blocks_automatic_coverage`: current candidate set rechecked before publication |
| Actual delivery ordering | `latest_actual_delivery_orders_receipts_not_attempt_creation`: acknowledgements ordered by receipt time, append-only evidence |
| Legacy explicit reminder integration | `legacy_explicit_reminder_is_enriched_in_worker_without_losing_review_or_identity`: same task, same pending review, one send |

Existing suites retain authorization epoch, identity, pause, revision, provenance, unknown-recovery, typed workflow and live `agent.max_turns` checks. Existing unlimited general delivery volume remains unchanged: no global cooldown or daily quota was added. The pre-existing opt-in daily checkin scheduling policy remains.

## Purpose requirements

Set `quality_contract` on the operator-authored checkin configuration to a requirements object, for example the wellbeing JSON example. Synthesis must return `quality: {purpose, coverage: [{facet, source, quote}]}`. Purpose must match, at least one selected facet must be covered, and quotes must be grounded in available evidence and in the question. The same generic requirement is rechecked before delivery. `pending_response` uses exact candidate/revision plus grounded `response_quality`; it is published transactionally through Checkins, not a detached helper. Legacy `record_response(..., kind, ...)` associates a response but a kind label alone no longer marks coverage answered.

## Remaining limitations

- Purpose/facet assignment is model classification, not an independent semantic proof. A model can mislabel text despite schema and quotation checks.
- No new external mutation capabilities were added. Reservations/refunds still require admitted owner completion evidence; preparation and notice transport are not those outcomes.
- Legacy items without authenticated retained source evidence cannot be safely enriched. They remain open and visible with scheduled reviews; no source content is invented.
- Checkin purpose contracts are optional for backward compatibility. Untyped questions cannot claim verified wellbeing coverage; operators must opt into the example to require it.
- Checkin subject identity uses cited source/quote plus purpose requirements. It is intentionally conservative: changed surrounding context with identical cited grounds can still be suppressed.
- A resolved projection supplies `close`; it does not automatically tombstone the compatible item state. Explicit owner closure remains supported.
- Historical receipt timestamps cannot be reconstructed. Mixed legacy/new ordering has the documented fallback; status is local-owner JSON, not a new remote authenticated API.
- Status currently materializes all local item projections. No new large-ledger pagination contract was added.

## Verification

Native command (no installations):

```sh
HERMES_DISABLE_LAZY_INSTALLS=1 hermes --run-module unittest discover -s tests -t . -q
```

Final result: **350 tests, 2 skipped, no failures/errors**. The source/host compatibility work is a separate commit; these improvements do not claim to have repaired the original 324-test baseline's 14 failures and 6 errors. No installed plugin, active configuration, cron, core files, credentials or private production records were accessed or changed. All sends in these tests were synthetic transport fixtures.
