"""Generic follow-through decisions. Model metadata is not completion evidence.

Contracts are anchored to admitted source/quote. Review bookkeeping never creates
new subject evidence or renews a transport attempt. No volume or cooldown policy.
"""
import json
from .inference import fingerprint
from .adapters import validate_schema

CONTRACT = dict(type='object', additionalProperties=False,
    required=['goal', 'closure', 'contact', 'purpose', 'subject'], properties={
        'goal': dict(type='string', maxLength=500),
        'closure': dict(type='string', enum=['owner_confirmation', 'notice_acknowledged']),
        'contact': dict(type='string', enum=['explicit_reminder', 'contextual', 'none']),
        'purpose': dict(type='string', maxLength=100),
        'subject': dict(type='string', maxLength=200)})


def contract(c, item):
    row = c.execute('SELECT body FROM artifacts WHERE id=? AND item=?',
                    ('contract:'+item['id'], item['id'])).fetchone()
    body = json.loads(row[0]) if row else {}
    if body.get('source') != item['source'] or body.get('quote') != item['quote']:
        return None
    value = body.get('contract')
    validate_schema(value, CONTRACT)
    return value


def subject_key(item, value):
    return fingerprint([item['principal'], item['source'], item['quote'],
                        value if value else item['title'], item['actor'], item['expectation']])


def contacted(c, item, subject, exclude=None):
    # Includes UNKNOWN and in-flight attempts. Never reinterpret uncertainty as
    # permission to send another revision of the unchanged subject.
    for row in c.execute('''SELECT o.id,o.item,x.notice FROM outbox o JOIN outbox_claims x ON x.id=o.id JOIN items i ON i.id=o.item
            WHERE i.principal=? AND (o.state IN ('acknowledged','unknown','sending') OR x.token IS NOT NULL)''',
                         (item['principal'],)):
        if row['id'] == exclude: continue
        notice = json.loads(row['notice']) if row['notice'] else {}
        artifact = c.execute('SELECT body FROM artifacts WHERE id=?', (notice.get('artifact'),)).fetchone()
        previous=json.loads(artifact[0]) if artifact else {}
        if (subject and previous.get('subject_key') == subject) or (row['item']==item['id'] and not previous.get('subject_key')):
            return True
    # Legacy receipts without authored subject evidence cannot authorize a new
    # attempt merely because bookkeeping or classification acquired a revision.
    return bool(c.execute("SELECT 1 FROM outbox o LEFT JOIN outbox_claims x ON x.id=o.id WHERE o.item=? AND o.state IN ('acknowledged','unknown','sending') AND x.notice IS NULL AND o.id<>?", (item['id'],exclude or '')).fetchone())


def decision(c, item, *, missing, prepared, exclude=None):
    value = contract(c, item)
    subject = subject_key(item, value)
    action, reason = 'wait', 'no_meaningful_contact'
    if missing and item['task'] == 'draft' and item['actor'] == 'assistant':
        action, reason = 'prepare', 'assistant_obligation'
    elif item['actor'] == 'assistant':
        action, reason = 'ask', 'assistant_obligation'
    elif value and value['contact'] == 'explicit_reminder':
        action, reason = 'ask', 'explicit_reminder'
    if contacted(c, item, subject, exclude):
        action, reason = 'wait', 'unchanged_subject'
    return dict(next_action=action, contact_reason=reason, subject_key=subject,
                goal=value['goal'] if value else item['title'],
                closure_condition=value['closure'] if value else 'owner_confirmation',
                purpose=value['purpose'] if value else 'unspecified')


def evidence_state(c, item, *, prepared, verified, confirmed):
    value = contract(c, item)
    deliveries = [dict(r) for r in c.execute('SELECT * FROM outbox WHERE item=? ORDER BY rowid', (item['id'],))]
    responses = []
    for row in c.execute('SELECT body FROM artifacts WHERE item=? ORDER BY rowid', (item['id'],)):
        body = json.loads(row[0])
        if body.get('type') in ('semantic_association', 'semantic_link', 'purpose_response'):
            event = c.execute('SELECT body FROM events WHERE id=?', (body.get('event'),)).fetchone()
            event = json.loads(event[0]) if event else {}
            if event.get('role') == 'human' and event.get('principal') == item['principal']:
                responses.append(event['id'])
    sent = [d['id'] for d in deliveries if d['state']=='acknowledged' and d['receipt']]
    day=c.execute('SELECT state FROM checkin_days WHERE item=?',(item['id'],)).fetchone() if item['kind']=='checkin' else None
    purpose_resolved=bool(day and day['state']=='answered' and responses)
    resolved = bool(purpose_resolved or confirmed or (value and value['contact']=='explicit_reminder'
                                  and value['closure']=='notice_acknowledged' and sent))
    action = 'close' if resolved else 'wait' if responses else decision(c, item, missing=not prepared and item['expectation']=='local_draft', prepared=prepared)['next_action']
    return dict(goal=value['goal'] if value else item['title'],
                closure_condition='purpose_coverage' if purpose_resolved else value['closure'] if value else 'owner_confirmation',
                prepared=bool(prepared), executed=bool(verified), sent=bool(sent),
                execution_scope='private_file' if verified else 'not_established',
                delivery_scope='notice_only', delivery_evidence=sent,
                responded=bool(responses), resolved=resolved, response_evidence=responses,
                next_action=action)


def activity(store):
    """Local-owner diagnostics. Historical rows are read, never rewritten.

    Legacy transport rows have no acknowledgement timestamp: ordering is by
    attempt creation, explicitly reported rather than fabricated wall-clock time.
    """
    with store.tx() as c:
        row=c.execute("SELECT * FROM outbox WHERE state='acknowledged' AND receipt IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()
        delivery=dict(row) if row else None
        dated=[]
        for row in c.execute("SELECT o.*,a.body FROM outbox o JOIN artifacts a ON a.id='delivery-receipt:'||o.id WHERE o.state='acknowledged' AND o.receipt IS NOT NULL"):
            proof=json.loads(row['body'])
            if proof.get('type')=='delivery_receipt' and proof.get('receipt')==row['receipt']:
                entry={k:row[k] for k in row.keys() if k!='body'}
                entry['acknowledged_at']=proof['acknowledged_at']
                dated.append(entry)
        if dated: delivery=max(dated,key=lambda d:d['acknowledged_at'])
        responses=[]
        for row in c.execute('SELECT rowid,body,item FROM artifacts ORDER BY rowid'):
            body=json.loads(row['body'])
            if body.get('type') in ('semantic_association','semantic_link','purpose_response'):
                responses.append(dict(item=row['item'],event=body.get('event'),type=body['type']))
        for row in c.execute('SELECT r.event,d.item FROM checkin_responses r JOIN checkin_days d ON d.id=r.day_id ORDER BY r.rowid'):
            if not any(r['event']==row['event'] for r in responses):
                responses.append(dict(item=row['item'],event=row['event'],type='associated_response_not_coverage'))
        row=c.execute("SELECT * FROM lifecycle_events WHERE result_state='confirmed' ORDER BY rowid DESC LIMIT 1").fetchone()
        closure=dict(row) if row else None
    return dict(latest_delivery=delivery,latest_response=responses[-1] if responses else None,
                latest_closure=closure,delivery_order='acknowledgement_time_with_legacy_fallback')


def contact_current(c, item, body, exclude=None):
    if body.get('type')=='checkin' and body.get('subject_key'):
        return not contacted(c,item,body['subject_key'],exclude)
    if body.get('type') in ('local_draft', 'meeting_brief', 'checkin'):
        return True  # Their separate content/quality contracts own admission.
    expected = decision(c, item, missing=item['expectation']=='local_draft', prepared=False, exclude=exclude)
    return (body.get('next_action') == 'ask' and expected['next_action'] == 'ask'
            and body.get('subject_key') == expected['subject_key']
            and body.get('contact_reason') == expected['contact_reason'])
