"""Read-only, version-bound Hermes state.db v32/v33 provenance adapter.

No transcript enumeration, migrations, private imports, or producer claims from
model data. Only an explicitly named session, its canonical lineage, and bounded
admissions joined to exact persisted platform messages are queried.
"""
import contextlib
import hashlib
import json
import sqlite3
from pathlib import Path

HUMAN_SOURCES={'cli','tui','gui','telegram','discord','slack','whatsapp','signal','matrix','sms','email'}

# v33 only re-tags delegate model_config; the admission digest, producer
# envelope and message ownership protocols are unchanged. Never infer support
# for future versions from compatible-looking columns or the installed SDK.
SUPPORTED_NATIVE_SCHEMAS=frozenset({32,33})

class NativeSourceError(ValueError):
    """Source rejection with a constant, transcript-free diagnostic reason."""
    @property
    def code(self):
        return str(self).replace(';','').replace(' ','_')


class NativeSource:
    def __init__(self,home): self.home=Path(home).absolute()
    def _lineage(self,c,row):
        """Only gateway reset edges, never generic parent/compression edges.

        Older gateway roots can lack created_source and a closure stamp. The
        reset marker and unchanged stored routing identity still bind that edge.
        transport_profile is deliberately not an identity: gateway refreshes it.
        """
        tip=row; seen=set()
        route=('source','user_id','session_key','chat_id','chat_type','thread_id','profile_name')
        while True:
            if row['id'] in seen or len(seen)>=64: raise NativeSourceError('cyclic or excessive native lineage')
            seen.add(row['id'])
            config=json.loads(row['model_config'] or '{}') if 'model_config' in row.keys() else {}
            if not isinstance(config,dict): raise NativeSourceError('invalid native metadata')
            if any(k in config for k in ('_branched_from','_delegate_from')): raise NativeSourceError('branched or delegated session')
            parent=row['parent_session_id']
            if not parent:
                if '_reset_from' in config: raise NativeSourceError('orphan reset marker')
                return
            required={'model_config','end_reason',*route}
            if not required.issubset(row.keys()) or config.get('_reset_from')!=parent:
                raise NativeSourceError('unverified native lineage')
            previous=c.execute('SELECT * FROM sessions WHERE id=?',(parent,)).fetchone()
            if not previous: raise NativeSourceError('missing reset ancestor')
            if any(previous[k]!=tip[k] for k in route): raise NativeSourceError('foreign reset identity or route')
            if 'origin_json' not in row.keys(): raise NativeSourceError('missing reset origin')
            origins=[json.loads(r['origin_json'] or '{}') for r in (tip,previous)]
            if not all(isinstance(o,dict) and o for o in origins): raise NativeSourceError('invalid reset origin')
            origin_keys=('platform','user_id','chat_id','chat_type','thread_id','profile','scope_id','guild_id')
            if any(origins[0].get(k)!=origins[1].get(k) for k in origin_keys): raise NativeSourceError('foreign reset origin')
            legacy_root=not previous['parent_session_id'] and previous['created_source'] is None
            if previous['created_source']!=tip['created_source'] and not legacy_root: raise NativeSourceError('foreign reset surface')
            if previous['end_reason']!='session_reset' and not (legacy_root and previous['end_reason'] is None):
                raise NativeSourceError('unclosed reset ancestor')
            if not tip['user_id'] or not tip['session_key'] or not tip['chat_id']: raise NativeSourceError('unbound reset route')
            # A local receipt has a separate canonical lineage protocol; until
            # that protocol is supported, do not adopt its physical children.
            if tip['source']=='local': raise NativeSourceError('unsupported local reset lineage')
            row=previous

    def _verify_messaging_admission(self,c,row,admission,body,identities,*,pending=False):
        if row['created_source'] in {'cli','tui','gui'}:
            # Local creation/operator receipts are not this adapter's per-turn
            # human-producer proof. Do not infer authority from a GUI label.
            raise NativeSourceError('unsupported_native_turn_provenance')
        native=body.get('native_text_v1')
        if not isinstance(native,dict): raise NativeSourceError('queued admission has no verified human producer')
        source=native.get('source',{}); provenance=native.get('provenance',{}); event=native.get('event',{})
        if 'automation' in native: raise NativeSourceError('automated native producer')
        platform=row['created_source']
        if not isinstance(source,dict) or not isinstance(provenance,dict) or not isinstance(event,dict): raise NativeSourceError('unsupported native producer')
        if source.get('is_bot') is not False or source.get('chat_type')!='dm': raise NativeSourceError('nonhuman messaging producer')
        transport=provenance.get('transport_profile')
        connector='adapter:'+platform+':'+(transport or '')
        if provenance.get('connector')!=connector or provenance.get('platform')!=platform or provenance.get('runtime_home')!=str(self.home) or provenance.get('transport_home')!=str(self.home): raise NativeSourceError('foreign native provenance')
        if source.get('platform')!=platform or source.get('chat_id')!=row['chat_id'] or source.get('thread_id')!=row['thread_id'] or native.get('route')!=row['session_key']: raise NativeSourceError('foreign native route')
        sender=source.get('user_id'); message_id=source.get('message_id')
        if not isinstance(sender,str) or not sender or not isinstance(message_id,str) or not message_id or event.get('message_id')!=message_id: raise NativeSourceError('unbound native event')
        principal=admission['principal_id']
        try: principal_fields=json.loads(principal[len('messaging:'):]) if isinstance(principal,str) and principal.startswith('messaging:') else None
        except (TypeError,ValueError): principal_fields=None
        if principal_fields!=[transport,platform,row['chat_id'],row['thread_id'],sender]: raise NativeSourceError('native principal mismatch')
        aliases=identities if isinstance(identities,dict) else {}
        if not aliases.get(platform+':'+sender) or aliases.get(platform+':'+sender)!=aliases.get(platform+':'+row['user_id']): raise NativeSourceError('unlinked native sender')
        if not {'admission_id','request_id'}<=set(admission.keys()) or type(admission['admission_id']) is not str or not admission['admission_id'] or admission['request_id']!=message_id: raise NativeSourceError('unbound native admission')
        # Absence is the only deferrable condition. Contradictory text, producer,
        # ownership or duplicates under this platform ID are never PRE witnesses.
        messages=c.execute("SELECT role,content,display_metadata FROM messages WHERE session_id=? AND platform_message_id=?",(row['id'],message_id)).fetchall()
        if not messages:
            if pending: return
            raise NativeSourceError('native user message not persisted')
        if len(messages)!=1 or messages[0]['role']!='user' or messages[0]['content']!=body['text']:
            raise NativeSourceError('native user message ambiguous or contradictory')
        try: metadata=json.loads(messages[0]['display_metadata'] or '{}')
        except (TypeError,ValueError): raise NativeSourceError('invalid native message ownership')
        # gateway_input_owner is the turn-persistence reservation, not the
        # admission_id (the host deliberately uses distinct identities). The
        # exact platform message ID + text + session bind the persisted message
        # to this digest-validated adapter admission; require its native stamp.
        if not isinstance(metadata,dict) or not isinstance(metadata.get('gateway_input_owner'),str) or not metadata['gateway_input_owner']: raise NativeSourceError('native message ownership stamp missing')

    @contextlib.contextmanager
    def _connection(self):
        path=self.home/'state.db'
        if not path.is_file() or any(p.is_symlink() for p in (path,*path.parents)): raise NativeSourceError('unsafe native state')
        with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as c:
            c.row_factory=sqlite3.Row
            c.execute('PRAGMA query_only=ON')
            version=c.execute('SELECT version FROM schema_version LIMIT 1').fetchone()
            if not version or version[0] not in SUPPORTED_NATIVE_SCHEMAS: raise NativeSourceError('unsupported native schema; requires v32 or v33')
            c.execute('BEGIN')
            yield c

    def _admission_body(self,row,admission):
        if admission['target_session_id']!=row['id']: raise NativeSourceError('foreign native admission target')
        body=json.loads(admission['payload_json'])
        if not isinstance(body,dict) or type(body.get('text')) is not str or not body['text'] or len(body['text'])>1048576:
            raise NativeSourceError('invalid native admission body')
        encoded=json.dumps({'target':row['id'],'payload':{'input':body,'intent':admission['intent']}},ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8',errors='surrogatepass')
        if hashlib.sha256(encoded).hexdigest()!=admission['payload_digest']: raise NativeSourceError('native admission digest mismatch')
        if admission['intent']!='queue': raise NativeSourceError('unsupported native admission intent')
        return body

    def recent(self,session_id,identities,*,principal,limit=20,before_seq=None):
        """Read verified human text on ONE exact native session/route, newest first.

        No ingestion, callbacks, model, or delivery. Terminal (finished/interrupted)
        admissions remain evidence only when their native producer, digest, exact
        platform ID and persisted message still verify. Unadmitted transcript rows
        are never selected. Limits bound candidate admissions, not accepted results;
        invalid candidates are omitted rather than scanning farther into history.
        """
        if type(limit) is not int or not 1<=limit<=100: raise NativeSourceError('invalid native history limit')
        if before_seq is not None and (type(before_seq) is not int or before_seq<1): raise NativeSourceError('invalid native history cursor')
        with self._connection() as c:
            row=c.execute('SELECT * FROM sessions WHERE id=?',(session_id,)).fetchone()
            if not row: raise NativeSourceError('unknown native session')
            self._lineage(c,row)
            surface=row['created_source']; identity=row['user_id']
            if surface not in HUMAN_SOURCES or row['source']!=surface: raise NativeSourceError('nonhuman source')
            if not isinstance(identities,dict) or not principal or identities.get(surface+':'+(identity or ''))!=principal:
                raise NativeSourceError('unbound native history principal')
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'session_admissions' not in tables: raise NativeSourceError('native turn admission missing')
            # rowid is the v32 admission sequence (seq INTEGER PRIMARY KEY).
            sql="SELECT rowid AS native_seq,* FROM session_admissions WHERE target_session_id=? AND status IN ('started','terminal')"
            params=[session_id]
            if before_seq is not None:
                sql+=' AND rowid<?'; params.append(before_seq)
            rows=c.execute(sql+' ORDER BY rowid DESC LIMIT ?',(*params,limit)).fetchall()
            result=[]
            for admission in rows:
                try:
                    body=self._admission_body(row,admission)
                    self._verify_messaging_admission(c,row,admission,body,identities)
                except (ValueError,TypeError,KeyError):
                    continue
                result.append(dict(session_id=session_id,surface=surface,identity=identity,principal=principal,
                                   seq=admission['native_seq'],admission_id=admission['admission_id'],
                                   payload_digest=admission['payload_digest'],status=admission['status'],
                                   platform_message_id=admission['request_id'],text=body['text']))
            return result

    def resolve(self,payload,identities=None):
        return self.reference(payload,identities)[:2]

    def reference(self,payload,identities=None,*,pending=False):
        """Bind one started native admission; pending is NOT event authority.

        Only PRE may request an unpersisted reference. POST must call the strict
        default again, then compare its digest with the epoch-fenced PRE witness.
        No text, sender token or identity mapping is retained in the reference.
        """
        with self._connection() as c:
            row=c.execute('SELECT * FROM sessions WHERE id=?',(payload.get('session_id'),)).fetchone()
            if not row: raise NativeSourceError('unknown native session')
            # The real hook reports the persisted reset parent as well as delegate
            # parents. Only the DB lineage protocol can distinguish the two.
            if payload.get('parent_session_id') and payload['parent_session_id']!=row['parent_session_id']:
                raise NativeSourceError('foreign native hook parent')
            self._lineage(c,row)
            created=row['created_source']; source=row['source']
            if created not in HUMAN_SOURCES or source not in HUMAN_SOURCES|{'local'}: raise NativeSourceError('nonhuman source')
            receipt=c.execute('SELECT value FROM state_meta WHERE key=?',('gateway.local_policy.v1:'+row['id'],)).fetchone()
            identity=row['user_id']; surface=created
            if receipt:
                r=json.loads(receipt[0]); policy=r.get('policy',{})
                if r.get('profile_id')!=str(self.home) or r.get('session_id')!=row['id'] or r.get('entry',{}).get('session_id')!=row['id'] or policy.get('source')!=created: raise NativeSourceError('invalid immutable creation receipt')
                identity=r.get('principal_id')
                if not identity or (row['user_id'] and row['user_id']!=identity): raise NativeSourceError('native identity mismatch')
            elif source=='local': raise NativeSourceError('local session needs native receipt')
            # Current native admissions override creation identity for shared
            # operators. Automation admitted into a human session stays machine
            # work, even when routing source still says local/cli.
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'session_admissions' not in tables: raise NativeSourceError('native turn admission missing')
            admissions=c.execute("SELECT * FROM session_admissions WHERE target_session_id=? AND status='started' LIMIT 2",(row['id'],)).fetchall()
            if not admissions: raise NativeSourceError('active native turn admission missing')
            if len(admissions)!=1: raise NativeSourceError('ambiguous native admission')
            a=admissions[0]; body=self._admission_body(row,a)
            if body['text']!=payload.get('user_message'): raise NativeSourceError('native admission mismatch')
            # Only server-persisted adapter envelopes bound to an actual user
            # message establish producer authority, never the session alone.
            self._verify_messaging_admission(c,row,a,body,identities,pending=pending)
            identity=row['user_id']
            if not identity and source in {'cli','tui'} and not payload.get('sender_id'): identity='local-owner'
            if not identity: raise NativeSourceError('sender mismatch')
            sender=payload.get('sender_id')
            if sender and sender!=identity:
                # Explicit surface-scoped aliases only; never strip/normalize a
                # transport token or accept an alias supplied by the hook.
                aliases=identities if isinstance(identities,dict) else {}
                principal=aliases.get(surface+':'+identity)
                if not principal or aliases.get(surface+':'+sender)!=principal: raise NativeSourceError('sender mismatch')
            platform=payload.get('platform')
            if platform and platform not in {surface,'local','terminal' if surface=='cli' else surface,'desktop' if surface=='gui' else surface}: raise NativeSourceError('surface mismatch')
            def digest(value):
                return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8',errors='surrogatepass')).hexdigest()
            # Include lease/generation when supplied by this SDK, and immutable
            # route/lineage facts, but not mutable session timestamps/model state.
            binding={k:a[k] for k in a.keys() if k not in {'status','started_at','terminal_at','updated_at'}}
            route={k:row[k] for k in ('id','source','created_source','parent_session_id','user_id','session_key','chat_id','chat_type','thread_id','profile_name') if k in row.keys()}
            return surface,identity,dict(source=digest([row['id'],a['admission_id']]),
                binding=digest([binding,route,identities,payload.get('turn_id')]))
