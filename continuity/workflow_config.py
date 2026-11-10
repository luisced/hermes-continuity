"""Operator-authored workflow presets. Never model-selected tools or arguments."""
import json
from pathlib import PurePosixPath
import re

TYPES = {'local_draft', 'meeting_brief'}


def validate_workflow_config(value: dict) -> dict:
    if type(value) is not dict or set(value) != {'enabled', 'presets'} or type(value['enabled']) is not bool:
        raise ValueError('workflow configuration')
    presets = value['presets']
    if type(presets) is not dict or len(presets) > 32:
        raise ValueError('workflow presets bound')
    for name, preset in presets.items():
        if type(name) is not str or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name):
            raise ValueError('explicit preset name required')
        if type(preset) is not dict or set(preset) != {'type', 'principal', 'selectors', 'completion_condition', 'delivery_policy'}:
            raise ValueError('workflow preset fields')
        if preset['type'] not in TYPES or type(preset['principal']) is not str or not preset['principal']:
            raise ValueError('workflow type or principal')
        if preset['completion_condition'] != 'private_file_verified' or preset['delivery_policy'] != 'owner_review':
            raise ValueError('unsupported completion or delivery policy')
        selectors = preset['selectors']
        if type(selectors) is not list or not 1 <= len(selectors) <= 4:
            raise ValueError('bounded selected sources required')
        seen = set()
        for s in selectors:
            if type(s) is not dict or set(s)-{'role', 'scope', 'path', 'required'} or not {'role', 'scope', 'required'} <= set(s):
                raise ValueError('workflow selector')
            if any(type(s[k]) is not str or not s[k] or len(s[k]) > 128 or ':' in s[k] for k in ('role', 'scope')) or type(s['required']) is not bool:
                raise ValueError('workflow scope')
            if 'path' in s:
                p = s['path']
                if type(p) is not str or not p or len(p) > 250 or '\\' in p or ':' in p or PurePosixPath(p).is_absolute() or '..' in PurePosixPath(p).parts or PurePosixPath(p).suffix.lower() != '.md':
                    raise ValueError('workflow path')
            if s['role'] not in ('notes', 'documents', 'calendar') or (preset['type'] == 'local_draft' and s['role'] == 'calendar'):
                raise ValueError('workflow read role')
            key = (s['role'], s['scope'], s.get('path'))
            if key in seen: raise ValueError('duplicate workflow source')
            seen.add(key)
        required = {s['role'] for s in selectors if s['required']}
        if not required or (preset['type'] == 'meeting_brief' and ('calendar' not in required or not required & {'notes', 'documents'})):
            raise ValueError('required workflow sources missing')
    return json.loads(json.dumps(value))
