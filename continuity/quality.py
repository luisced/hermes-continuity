"""Workflow purpose requirements, independent from lifecycle and transports.

Coverage is a structured, grounded model classification, not an assertion that
an objective happened. This validator cannot prove semantic correctness.
"""
import json
from .adapters import validate_schema

REQUIREMENTS = dict(type='object', additionalProperties=False,
    required=['purpose', 'any_of'], properties={
        'purpose': dict(type='string', maxLength=100),
        'any_of': dict(type='array', maxItems=16, items=dict(type='string', maxLength=100))})
QUALITY = dict(type='object', additionalProperties=False,
    required=['purpose', 'coverage'], properties={
        'purpose': dict(type='string', maxLength=100),
        'coverage': dict(type='array', maxItems=16, items=dict(type='object', additionalProperties=False,
            required=['facet', 'source', 'quote'], properties={
                'facet': dict(type='string', maxLength=100),
                'source': dict(type='string', maxLength=512),
                'quote': dict(type='string', maxLength=1200)}))})


def requirements(value):
    validate_schema(value, REQUIREMENTS)
    if not value['purpose'].strip() or not value['any_of'] or any(not v.strip() for v in value['any_of']):
        raise ValueError('purpose requirements missing')
    return value


def validate_quality(value, required, evidence, *, message=None):
    requirements(required)
    validate_schema(value, QUALITY)
    if value['purpose'] != required['purpose']:
        raise ValueError('purpose mismatch')
    covered = set()
    for entry in value['coverage']:
        source = evidence.get(entry['source'], {})
        quote = entry['quote']
        if (source.get('status') not in ('OK', 'PARTIAL') or len(quote.strip()) < 8
                or quote not in json.dumps(source.get('content'), ensure_ascii=False)
                or (message is not None and quote not in message)):
            raise ValueError('purpose coverage not grounded')
        covered.add(entry['facet'])
    if not covered.intersection(required['any_of']):
        raise ValueError('purpose coverage missing')
    return value
