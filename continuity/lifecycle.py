"""Internal, principal-scoped lifecycle projection; not an authentication boundary.

Callers must supply an already authenticated principal. Transport remains owned
by the outbox, and the compatible obligation state remains owned by items.
"""
import json
import base64
import binascii
import hmac
import hashlib
import uuid
import math
import re
from .store import Stale


STAGES = {'open', 'running', 'waiting_owner', 'waiting_third_party', 'blocked', 'done', 'dismissed'}
COMPATIBLE = dict(open='open', running='open', waiting_owner='waiting',
                  waiting_third_party='waiting', blocked='blocked', done='done', dismissed='dismissed')
DEFAULT_STAGE = dict(open='open', waiting='waiting_owner', snoozed='waiting_owner',
                     blocked='blocked', done='done', dismissed='dismissed')
NEXT_ACTION = dict(open='review', running='await_execution', waiting_owner='await_owner',
                   waiting_third_party='await_third_party', blocked='resolve_block', done='none', dismissed='none')


class Lifecycle:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _item(c, item_id, principal):
        row = c.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        if not row or not principal or row['principal'] != principal:
            raise PermissionError('item not accessible')
        return dict(row)

    def snapshot(self, item_id: str, principal: str) -> dict:
        with self.store.tx() as c:
            return self._snapshot(c, self._item(c, item_id, principal))

    def transition(self, item_id: str, expected_revision: int, principal: str, *, stage: str,
                   causal_event: str | None, evidence_ids: list[str], reason: str, now: float) -> dict:
        with self.store.tx() as c:
            item = self._item(c, item_id, principal)
            if type(expected_revision) is not int or item['revision'] != expected_revision:
                raise Stale('item revision changed')
            if type(stage) is not str or stage not in STAGES:
                raise ValueError('stage')
            if item['state'] in ('done', 'dismissed'):
                raise ValueError('closed item is a tombstone')
            self._validate_metadata(reason, now)
            self._evidence(c, item, evidence_ids)
            event = self._event(c, causal_event, principal) if causal_event is not None else None
            result_state = self._snapshot(c, item)['result_state']
            if stage == 'done':
                self._confirm(c, item, event, evidence_ids, now)
                result_state = 'confirmed'
            artifact, _ = self._artifact(c, item, item['artifact'])
            authorized = artifact is not None and self.store.artifact_current(c, item, artifact['id'], artifact['body'])
            review = None if stage in ('done', 'dismissed') else item['review']
            self.store._transition(c, item_id, expected_revision, COMPATIBLE[stage], reason, review)
            if artifact is not None and authorized and stage not in ('done', 'dismissed'):
                # Carry only already-registered, unchanged selected evidence. A
                # stale anchor cannot be revived by a later lifecycle transition.
                c.execute('''UPDATE lifecycle_results SET revision=? WHERE item=? AND revision=?
                    AND artifact=? AND artifact_digest=?''',
                          (expected_revision+1, item_id, expected_revision, artifact['id'],
                           hashlib.sha256(artifact['body'].encode()).hexdigest()))
            item = self._item(c, item_id, principal)
            self._write(c, item, stage, result_state, causal_event, evidence_ids, reason, now,
                        actor=event['role'] if event else 'unknown')
            return self._snapshot(c, item)

    def page(self, principal: str, *, stage: str | None, after: str | None, limit: int = 50) -> dict:
        """Keyset page, scoped to one principal/filter and this store's cursor key.

        The cursor is not authentication. No fixed page is proof that a complete
        candidate set is unique; callers must consume has_more as appropriate.
        """
        if type(principal) is not str or not principal:
            raise ValueError('principal required')
        if stage is not None and (type(stage) is not str or stage not in STAGES):
            raise ValueError('stage')
        if type(limit) is not int or limit <= 0:
            raise ValueError('positive page limit required')
        limit = min(limit, 100)
        scope = hashlib.sha256(json.dumps([principal, stage]).encode()).hexdigest()
        with self.store.tx() as c:
            c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',
                      ('lifecycle_cursor_key', json.dumps(uuid.uuid4().hex+uuid.uuid4().hex)))
            key = json.loads(c.execute("SELECT value FROM settings WHERE key='lifecycle_cursor_key'").fetchone()[0]).encode()
            last = ''
            if after is not None:
                try:
                    if type(after) is not str or len(after) > 2048:
                        raise ValueError('cursor')
                    encoded, signature = after.split('.')
                    if not hmac.compare_digest(hmac.new(key, encoded.encode(), hashlib.sha256).hexdigest(), signature):
                        raise ValueError('cursor')
                    payload = json.loads(base64.b64decode(encoded, altchars=b'-_', validate=True))
                    if (type(payload) is not dict or payload.get('v') != 1 or payload.get('scope') != scope
                            or type(payload.get('last')) is not str or not payload['last']):
                        raise ValueError('cursor')
                    last = payload['last']
                except (ValueError, TypeError, binascii.Error):
                    raise ValueError('invalid or mismatched lifecycle cursor') from None
            compatible = 'CASE l.stage '+''.join("WHEN '"+s+"' THEN '"+v+"' " for s,v in COMPATIBLE.items())+'END'
            fallback = 'CASE i.state '+''.join("WHEN '"+s+"' THEN '"+v+"' " for s,v in DEFAULT_STAGE.items())+'END'
            effective = 'CASE WHEN l.revision=i.revision AND ('+compatible+')=i.state THEN l.stage ELSE ('+fallback+') END'
            query = 'SELECT i.* FROM items i LEFT JOIN lifecycle_states l ON l.item=i.id WHERE i.principal=? AND i.id>?'
            params = [principal, last]
            if stage is not None:
                query += ' AND ('+effective+')=?'
                params.append(stage)
            rows = c.execute(query+' ORDER BY i.id LIMIT ?', (*params, limit+1)).fetchall()
            more = len(rows) > limit
            cursor = None
            if more:
                encoded = base64.urlsafe_b64encode(json.dumps(dict(v=1, scope=scope, last=rows[limit-1]['id'])).encode()).decode()
                cursor = encoded+'.'+hmac.new(key, encoded.encode(), hashlib.sha256).hexdigest()
            return dict(items=[self._snapshot(c, dict(r)) for r in rows[:limit]], next_cursor=cursor, has_more=more)

    def record_result(self, item_id: str, expected_revision: int, principal: str, result: dict, *, now: float) -> str:
        """Record metadata for an existing artifact, never caller-supplied state.

        Currently accepts {'artifact_id': str, 'verify': bool (optional)} for
        existing local_draft/meeting_brief artifacts. verify performs a managed-file
        digest check (and typed workflow source-evidence checks); it is not an
        assertion of delivery or objective completion.
        """
        with self.store.tx() as c:
            return self._record_result(c, item_id, expected_revision, principal, result, now=now)

    def _record_result(self, c, item_id, expected_revision, principal, result, *, now):
        item = self._item(c, item_id, principal)
        if type(expected_revision) is not int or item['revision'] != expected_revision:
            raise Stale('item revision changed')
        if not self.store._enabled(c) or item['state'] in ('done', 'dismissed'):
            raise ValueError('result publication disabled or closed')
        self._validate_metadata('result_recorded', now)
        if (type(result) is not dict or set(result)-{'artifact_id', 'verify'}
                or type(result.get('artifact_id')) is not str or type(result.get('verify', False)) is not bool):
            raise ValueError('invalid result reference')
        artifact, body = self._artifact(c, item, result['artifact_id'])
        if (artifact is None or body['created'] > now
                or not self.store.artifact_current(c, item, artifact['id'], artifact['body'])):
            raise ValueError('invalid result artifact')
        verify = result.get('verify', False)
        if verify and not self._file_valid(artifact['id'], body):
            raise ValueError('result verification failed')
        digest = hashlib.sha256(artifact['body'].encode()).hexdigest()
        rid = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([item_id, artifact['id'], digest])).hex
        prior = c.execute('SELECT * FROM lifecycle_results WHERE id=?', (rid,)).fetchone()
        if prior and (not verify or prior['verified_at'] is not None):
            return rid
        snapshot = self._snapshot(c, item)
        self.store._transition(c, item_id, expected_revision, item['state'], 'result_recorded', item['review'])
        item = self._item(c, item_id, principal)
        sources = [body['source']] + [p['source'] for p in body.get('provenance', [])]
        c.execute('''INSERT INTO lifecycle_results VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            revision=excluded.revision,verified_at=excluded.verified_at''',
                  (rid, item_id, item['revision'], body['type'], artifact['id'], digest,
                   body['sha256'], json.dumps(sources), body['created'], now if verify else None))
        self._write(c, item, snapshot['stage'], 'verified' if verify else 'prepared', None,
                    [rid, artifact['id']], 'result_verified' if verify else 'result_prepared', now)
        return rid

    @staticmethod
    def _artifact(c, item, artifact_id):
        row = c.execute('SELECT * FROM artifacts WHERE id=? AND item=?', (artifact_id, item['id'])).fetchone()
        try:
            body = json.loads(row['body']) if row else {}
            valid = (isinstance(body, dict) and body.get('type') in ('local_draft', 'meeting_brief')
                     and body.get('item') == item['id'] and body.get('source') == item['source']
                     and type(body.get('revision')) is int and 0 <= body['revision'] <= item['revision']
                     and type(body.get('created')) in (int, float) and math.isfinite(body['created']) and body['created'] >= 0
                     and type(body.get('markdown')) is str and bool(body['markdown'].strip())
                     and body.get('sha256') == hashlib.sha256(body['markdown'].encode()).hexdigest()
                     and type(body.get('provenance', [])) is list
                     and len(body.get('provenance', [])) <= 32
                     and all(type(p) is dict and type(p.get('source')) is str and 0 < len(p['source']) <= 512
                             for p in body.get('provenance', [])))
            if valid and ('workflow_config_digest' in body or body['type'] == 'meeting_brief'):
                from .inference import fingerprint
                request = c.execute('SELECT body FROM events WHERE id=?', (body['source'],)).fetchone()
                valid = (request is not None and json.loads(request[0]).get('principal') == item['principal']
                         and body.get('content') == body['markdown'] and body.get('content_digest') == body['sha256']
                         and body.get('completion_condition') == 'private_file_verified'
                         and body.get('delivery_policy') == 'owner_review'
                         and 1 <= len(body['provenance']) <= 4
                         and body.get('evidence_ids') == [body['source']]+[p['evidence_id'] for p in body['provenance']])
                coverage = []
                for p in body['provenance']:
                    source = c.execute('SELECT item,body FROM artifacts WHERE id=?', (p['evidence_id'],)).fetchone()
                    snapshot = json.loads(source['body']) if source else {}
                    valid = valid and bool(source) and source['item'] == item['id'] and (
                        snapshot.get('type') == 'workflow_source' and snapshot.get('item') == item['id']
                        and snapshot.get('revision') == body['revision'] and snapshot.get('source') == p['source']
                        and snapshot.get('content_digest') == p['sha256'] == fingerprint(snapshot.get('content'))
                        and snapshot.get('status') in ('available', 'unavailable'))
                    coverage.append(dict(source=p['source'], status=snapshot.get('status')))
                valid = valid and body.get('coverage') == coverage
        except (ValueError, TypeError, AttributeError, KeyError):
            valid = False
            body = {}
        return (row, body) if valid else (None, {})

    def typed_artifact_valid(self, c, item, artifact_id, serialized):
        """Transaction-local typed result guard, shared with readiness effects.

        Untagged legacy drafts retain their existing content-only contract.
        Typed validity is structural evidence/private-file validity, not truth,
        human receipt, or completion of the underlying obligation.
        """
        body = json.loads(serialized)
        if 'workflow_config_digest' not in body and body.get('type') != 'meeting_brief':
            return True
        artifact, validated = self._artifact(c, item, artifact_id)
        return bool(artifact is not None and artifact['body'] == serialized
                    and self.store.artifact_current(c, item, artifact_id, serialized)
                    and self._file_valid(artifact_id, validated))

    def _file_valid(self, artifact_id, body):
        from .adapters import MarkdownAdapter
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', artifact_id):
            return False
        if body.get('path') != 'drafts/'+artifact_id+'.md':
            return False
        try:
            path = self.store.root/'drafts'/(artifact_id+'.md')
            if 'workflow_config_digest' in body and path.stat(follow_symlinks=False).st_mode & 0o777 != 0o600:
                return False
            text = MarkdownAdapter(self.store.root/'drafts').read(artifact_id+'.md')['text']
            return hashlib.sha256(text.encode()).hexdigest() == body['sha256']
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def _validate_metadata(reason, now):
        if type(reason) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', reason):
            raise ValueError('stable reason code required')
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError('invalid observation time')

    @staticmethod
    def _event(c, event_id, principal):
        row = c.execute('SELECT body FROM events WHERE id=?', (event_id,)).fetchone()
        event = json.loads(row[0]) if row else None
        if not event or event.get('principal') != principal:
            raise ValueError('invalid causal evidence')
        return event

    def _evidence(self, c, item, evidence_ids):
        if (type(evidence_ids) is not list or len(evidence_ids) > 32
                or any(type(e) is not str or not e or len(e) > 512 for e in evidence_ids)
                or len(set(evidence_ids)) != len(evidence_ids)):
            raise ValueError('invalid evidence list')
        for eid in evidence_ids:
            found = False
            for table in ('artifacts', 'outbox', 'lifecycle_results'):
                row = c.execute('SELECT item FROM '+table+' WHERE id=?', (eid,)).fetchone()
                if row:
                    if row['item'] != item['id']:
                        raise ValueError('invalid evidence scope')
                    found = True
            if c.execute('SELECT 1 FROM events WHERE id=?', (eid,)).fetchone():
                self._event(c, eid, item['principal'])
                found = True
            if not found:
                raise ValueError('missing evidence')

    def _linked_close(self, c, item, event, evidence_ids, now):
        """Transaction-local composition for Linker; all evidence is revalidated."""
        self._validate_metadata('semantic_owner_confirmed', now)
        self._evidence(c, item, evidence_ids)
        self._confirm(c, item, event, evidence_ids, now)
        # Retain the legacy item_events evidence format; the lifecycle ledger
        # below remains metadata-only and uses a stable reason code.
        legacy_reason = json.dumps(dict(source=event['id'], quote=event['text'],
                                        principal=item['principal'], actor='human',
                                        decision='explicit conversational close'))
        self.store._transition(c, item['id'], item['revision'], 'done', legacy_reason, None)
        item = self._item(c, item['id'], item['principal'])
        self._write(c, item, 'done', 'confirmed', event['id'], evidence_ids,
                    'semantic_owner_confirmed', now, actor='human')
        return self._snapshot(c, item)

    def _confirm(self, c, item, event, evidence_ids, now):
        from .linking import Linker
        if event and Linker(self.store, None, None)._approved(c, item, event, evidence_ids, now):
            reused = c.execute("SELECT 1 FROM lifecycle_events WHERE causal_event=? AND result_state='confirmed'",
                               (event['id'],)).fetchone()
            if reused: raise ValueError('reused confirmation')
            return
        # Existing direct callers retain the conservative owner control grammar.
        from .extraction import explicit_confirmation
        if (not event or event.get('role') != 'human' or event['id'] not in evidence_ids
                or event.get('parent') != item['source'] or event['id'] == item['source']
                or not explicit_confirmation(event.get('text', ''))):
            raise ValueError('human confirmation required')
        candidates = c.execute("""SELECT id FROM items WHERE principal=? AND source=?
            AND state NOT IN ('done','dismissed')""", (item['principal'], item['source'])).fetchall()
        reused = c.execute("SELECT 1 FROM lifecycle_events WHERE causal_event=? AND result_state='confirmed'",
                           (event['id'],)).fetchone()
        if len(candidates) != 1 or candidates[0]['id'] != item['id'] or reused:
            raise ValueError('ambiguous or reused confirmation')
        auth = c.execute('SELECT * FROM event_auth WHERE id=?', (event['id'],)).fetchone()
        configured = c.execute("SELECT value FROM settings WHERE key='identities'").fetchone()
        identities = json.loads(configured[0]) if configured else {}
        if (not auth or identities.get(auth['identity']) != item['principal']
                or event.get('authorization_epoch') != self.store._epoch(c)
                or not 0 <= now-auth['received'] <= 86400 or not self.store._enabled(c)):
            raise ValueError('confirmation authority expired')

    @staticmethod
    def _write(c, item, stage, result_state, causal_event, evidence_ids, reason, now, actor='unknown'):
        c.execute('''INSERT INTO lifecycle_states VALUES(?,?,?,?,?,?,?) ON CONFLICT(item) DO UPDATE SET
            revision=excluded.revision,stage=excluded.stage,result_state=excluded.result_state,
            next_action=excluded.next_action,reason=excluded.reason,changed_at=excluded.changed_at''',
                  (item['id'], item['revision'], stage, result_state, NEXT_ACTION[stage], reason, now))
        c.execute('''INSERT INTO lifecycle_events(item,revision,principal,thread,actor,stage,result_state,
            causal_event,evidence,closure_condition,next_action,reason,changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (item['id'], item['revision'], item['principal'], item['parent'] or item['source'], actor,
                   stage, result_state, causal_event, json.dumps(evidence_ids),
                   'owner_confirmation' if result_state == 'confirmed' else 'not_established',
                   NEXT_ACTION[stage], reason, now))

    def _snapshot(self, c, item):
        state = c.execute('SELECT * FROM lifecycle_states WHERE item=?', (item['id'],)).fetchone()
        state = dict(state) if state is not None else {}
        current = bool(state) and state['revision'] == item['revision'] and COMPATIBLE[state['stage']] == item['state']
        stage = state['stage'] if current else DEFAULT_STAGE[item['state']]
        artifact, body = self._artifact(c, item, item['artifact'])
        prepared = artifact is not None and self.typed_artifact_valid(c, item, artifact['id'], artifact['body'])
        if not prepared: artifact, body = None, {}
        result = c.execute('SELECT * FROM lifecycle_results WHERE item=? AND artifact=? AND artifact_digest=?',
                           (item['id'], artifact['id'], hashlib.sha256(artifact['body'].encode()).hexdigest())).fetchone() if artifact else None
        verified = bool(result and result['verified_at'] is not None and self._file_valid(artifact['id'], body))
        out = c.execute('SELECT * FROM outbox WHERE item=? ORDER BY revision DESC,rowid DESC LIMIT 1',
                        (item['id'],)).fetchone()
        confirmed = current and state['stage'] == 'done' and state['result_state'] == 'confirmed'
        history = c.execute('SELECT evidence FROM lifecycle_events WHERE item=? AND revision=?',
                            (item['id'], item['revision'])).fetchone() if current else None
        evidence = json.loads(history[0]) if history else []
        if prepared and artifact['id'] not in evidence:
            evidence.append(artifact['id'])
        # The current transport sends readiness/review notices, never the result
        # payload or accessible attachment. Even artifact-bound receipts establish
        # transport only. A future result transport needs its own authored evidence.
        from .followthrough import evidence_state
        return dict(followthrough=evidence_state(c,item,prepared=prepared,verified=verified,confirmed=confirmed),
                    item=item, revision=item['revision'], stage=stage,
                    result_state='confirmed' if confirmed else 'verified' if verified else 'prepared' if prepared else 'absent',
                    transport_state=out['state'] if out else 'absent',
                    next_action=NEXT_ACTION[stage], reason=state['reason'] if current else 'compatible_state',
                    evidence=evidence, changed_at=state['changed_at'] if current else None)
