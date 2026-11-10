"""Channel admission contracts; native provenance remains NativeSource's authority."""
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .source import HUMAN_SOURCES, NativeSource, NativeSourceError


# Metadata only. The presence of these fields never proves a human turn.
_NATIVE_COLUMNS = {
    'sessions': {'id', 'source', 'created_source', 'user_id', 'parent_session_id',
                 'model_config', 'end_reason', 'session_key', 'chat_id', 'chat_type',
                 'thread_id', 'profile_name', 'origin_json'},
    'messages': {'session_id', 'role', 'content', 'platform_message_id', 'display_metadata'},
    'session_admissions': {'admission_id', 'request_id', 'principal_id', 'target_session_id',
                           'payload_json', 'payload_digest', 'intent', 'status'},
    'state_meta': {'key', 'value'},
}


def surface_coverage(home: Path, settings: dict) -> list[dict]:
    """Report adapter capability and identity configuration without reading chats.

    configured means an explicit surface-scoped principal mapping exists, not
    that a transport is connected, consent is enabled, or hooks were exercised.
    There is no trusted live-acceptance ledger yet: automatic_verified stays
    false, including when a caller supplies a similarly named settings value.
    """
    identities = settings.get('identities', {})
    if not isinstance(identities, dict):
        identities = {}
    reason = None
    try:
        with NativeSource(home)._connection() as c:
            if any(not columns <= {r[1] for r in c.execute('PRAGMA table_info(' + table + ')')}
                   for table, columns in _NATIVE_COLUMNS.items()):
                reason = 'unsupported_native_schema'
    except NativeSourceError as exc:
        reason = ('unsafe_native_state' if exc.code == 'unsafe_native_state'
                  else 'unsupported_native_schema')
    except sqlite3.Error:
        reason = 'native_metadata_unavailable'
    coverage = []
    for surface in sorted(HUMAN_SOURCES):
        configured = any(type(k) is str and k.startswith(surface + ':') and k != surface + ':'
                         and type(v) is str and bool(v) for k, v in identities.items())
        blocked = 'unsupported_native_turn_provenance' if surface in {'cli', 'tui', 'gui'} else reason
        coverage.append(dict(surface=surface, configured=configured, supported=blocked is None,
                             automatic_verified=False,
                             reason=blocked or ('automatic_capture_not_verified' if configured
                                                else 'identity_not_configured')))
    return coverage


@dataclass(frozen=True, init=False)
class OwnerControl:
    """Internal request context, never a renderer DTO or a serialized credential.

    Only a trusted backend adapter holding the live authenticated host connection
    may call from_host. The host, not renderer JSON or a display name, grants the
    operator capability. In-process Python is trusted; this type is not a sandbox.
    Future domain entry points must take this context and fence effects with the
    current consent/epoch, not accept a caller-selected principal instead.
    """
    principal: str
    profile_scope: str
    request_id: str

    def __init__(self, *args, **kwargs):
        raise TypeError('authenticated host context required')

    @classmethod
    def from_host(cls, connection, home: Path, identities: dict, *, request_id: str):
        """Derive from one registered operator connection in the exact profile.

        identities is operator configuration, never request data. request_id is
        the host adapter's validated request correlation ID, not an authority.
        No remote endpoint is installed by this foundation.
        """
        from gateway.session_controls import AuthorityConnection
        from gateway.owner_identity import principal_subject
        if cls is not OwnerControl or type(connection) is not AuthorityConnection:
            raise ValueError('authenticated host context required')
        actor, authority = connection.actor, connection.authority
        scope = str(Path(home).absolute())
        if (connection._operator is not True or 'session:operator' not in actor.capabilities
                or connection.transport is None or actor.transport_id not in authority.events
                or authority.events[actor.transport_id] is not connection.transport
                or actor.subject != principal_subject(connection._identity)
                or connection._identity.get('instance_id', authority.instance_id) != authority.instance_id):
            raise ValueError('authenticated host context required')
        if actor.profile_id != scope or authority.profile_id != scope:
            raise ValueError('owner control profile mismatch')
        principal = identities.get('host:' + actor.subject) if isinstance(identities, dict) else None
        if type(principal) is not str or not principal:
            raise ValueError('unmapped host principal')
        if type(request_id) is not str or not request_id or len(request_id) > 256:
            raise ValueError('invalid owner control request id')
        control = object.__new__(cls)
        for key, value in (('principal', principal), ('profile_scope', scope), ('request_id', request_id)):
            object.__setattr__(control, key, value)
        return control


def require_owner_control(control: OwnerControl, home: Path) -> OwnerControl:
    """Domain-boundary type/scope guard; never upgrades JSON into authentication."""
    if type(control) is not OwnerControl:
        raise ValueError('authenticated host context required')
    if control.profile_scope != str(Path(home).absolute()):
        raise ValueError('owner control profile mismatch')
    return control


def verify_native_turn(home: Path, payload: dict, identities: dict, *, phase: str) -> dict:
    """Verify one turn, not an ingest permission or a replacement for epoch fencing.

    PRE may return an absence-only reference. POST always re-proves the exact
    persisted message. Consumers must still bind/consume the PRE witness through
    the store's epoch-fenced native pair operation (as host.capture does).
    """
    if phase not in {'pre', 'post'}:
        raise ValueError('unknown native phase')
    source = NativeSource(home)
    persisted = True
    try:
        surface, identity, reference = source.reference(payload, identities)
    except NativeSourceError as exc:
        if phase != 'pre' or exc.code != 'native_user_message_not_persisted':
            raise
        surface, identity, reference = source.reference(payload, identities, pending=True)
        persisted = False
    return dict(surface=surface, identity=identity, reference=reference, persisted=persisted)
