"""Synthetic server-owned v32 messaging turns; never opens the active profile."""
import hashlib
import json


def admit_message(c, home, session, text, turn, *, sender=None):
    """Persist one started adapter admission and its exact native user message.

    Works with the small unit schema and the real isolated SessionDB schema.
    Prior started turns become terminal, rather than erasing their history.
    """
    columns = [r[1] for r in c.execute('PRAGMA table_info(sessions)')]
    row = dict(zip(columns, c.execute('SELECT * FROM sessions WHERE id=?', (session,)).fetchone()))
    platform = row['created_source']
    sender = sender or row['user_id']
    message_id = f'platform:{session}:{turn}'
    native = dict(event=dict(message_id=message_id),
                  source=dict(platform=platform, chat_id=row['chat_id'], chat_type='dm',
                              thread_id=row['thread_id'], user_id=sender, message_id=message_id, is_bot=False),
                  provenance=dict(platform=platform, connector=f'adapter:{platform}:',
                                  runtime_home=str(home), transport_home=str(home), transport_profile=None),
                  route=row['session_key'])
    body = dict(text=text, native_text_v1=native)
    encoded = json.dumps(dict(target=session, payload=dict(input=body, intent='queue')),
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    principal = 'messaging:' + json.dumps([None, platform, row['chat_id'], row['thread_id'], sender], separators=(',', ':'))
    c.executescript('''CREATE TABLE IF NOT EXISTS session_admissions(
        admission_id TEXT,request_id TEXT,principal_id TEXT,target_session_id TEXT,
        lineage_json TEXT,payload_json TEXT,payload_digest TEXT,intent TEXT,status TEXT,
        owner_epoch INTEGER,generation INTEGER);
        CREATE TABLE IF NOT EXISTS messages(session_id TEXT,role TEXT,content TEXT,
        platform_message_id TEXT,display_metadata TEXT,timestamp REAL);''')
    existing = c.execute('SELECT payload_json FROM session_admissions WHERE target_session_id=? AND request_id=?',
                         (session, message_id)).fetchone()
    if existing:
        if json.loads(existing[0]) != body:
            raise AssertionError('fixture turn reused with different text or producer')
        return
    c.execute("UPDATE session_admissions SET status='terminal' WHERE target_session_id=? AND status='started'", (session,))
    c.execute('''INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,
        lineage_json,payload_json,payload_digest,intent,status,owner_epoch,generation)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (f'admission:{session}:{turn}', message_id, principal, session, json.dumps([session]),
         json.dumps(body), hashlib.sha256(encoded).hexdigest(), 'queue', 'started', 1, 1))
    c.execute('''INSERT INTO messages(session_id,role,content,platform_message_id,display_metadata,timestamp)
        VALUES(?,?,?,?,?,?)''', (session, 'user', text, message_id,
                               json.dumps(dict(gateway_input_owner=f'persistence:{session}:{turn}')), 100.0))
