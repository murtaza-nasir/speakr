"""The documented scopes match the routes (mailr spec G1, section 3.7).

Every API v1 request block in the API reference carries a **Scope:** line,
and every operation in /api/v1/openapi.json carries x-required-scopes, both
equal to the scopes the route declares with require_scope.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_norm = lambda path: re.sub(r'<[^>]+>|\{[^}]+\}', '{}', path.split('?')[0].rstrip('/'))


def _route_scopes():
    table = {}
    for rule in app.url_map.iter_rules():
        scopes = getattr(app.view_functions.get(rule.endpoint), '_required_scopes', None)
        if scopes is None or not rule.rule.startswith('/api/v1'):
            continue
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            table[(method, _norm(rule.rule))] = sorted(scopes)
    return table


def test_every_documented_request_states_its_scope():
    routes = _route_scopes()
    text = open(os.path.join(ROOT, 'docs/user-guide/api-reference.md'), encoding='utf-8').read()
    blocks = re.findall(r'```http\n(GET|POST|PUT|PATCH|DELETE) (/api/v1\S*)\n```\n\n(\*\*Scope:\*\* [^\n]*)?', text)
    assert len(blocks) > 40
    problems = []
    for method, path, line in blocks:
        scopes = routes.get((method, _norm(path)))
        if scopes is None:
            problems.append(f'{method} {path}: no such route')
            continue
        expected = 'none (any valid token' if not scopes else ', '.join(f'`{s}`' for s in scopes)
        if not line or expected not in line:
            problems.append(f'{method} {path}: documented {line!r}, route needs {scopes}')
    assert not problems, '\n'.join(problems)


def test_openapi_operations_carry_their_scopes():
    routes = _route_scopes()
    with app.test_request_context():
        from src.api.api_v1 import _openapi_with_scopes
        spec = _openapi_with_scopes()
    assert 'insufficient_scope' in spec['components']['securitySchemes']['bearerAuth']['description']
    for path, operations in spec['paths'].items():
        for method, operation in operations.items():
            expected = routes.get((method.upper(), _norm('/api/v1' + path)))
            assert expected is not None, f'{method} {path} is documented but has no route'
            assert operation.get('x-required-scopes') == expected, (method, path)
