"""Code-owned delivery eligibility; no global daily cap or contact cooldown."""
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str

def evaluate(snapshot,now):
    reason='eligible'
    if snapshot.get('paused'): reason='paused'
    elif snapshot.get('mode','shadow')!='active': reason='shadow'
    elif snapshot.get('state')!='open': reason='not open'
    elif snapshot.get('revision')!=snapshot.get('expected_revision'): reason='stale revision'
    elif snapshot.get('kind') in ('opportunity','pending_close'): reason='review only'
    elif snapshot.get('review') is not None and snapshot['review']>now: reason='snoozed'
    elif not 9<=datetime.fromtimestamp(now,ZoneInfo(snapshot.get('timezone','UTC'))).hour<21: reason='quiet hours'
    return Decision(reason=='eligible',reason)
