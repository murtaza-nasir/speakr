"""Every operation in /api/v1/openapi.json has a unique operationId (#443).

Tools that turn an OpenAPI document into LLM tools, Open WebUI among them,
create a tool only for an operation that has one.
"""

from src.app import app
from src.api.api_v1 import _openapi_with_scopes


def _operations():
    with app.test_request_context():
        spec = _openapi_with_scopes()
    return [(method, path, op) for path, ops in spec['paths'].items()
            for method, op in ops.items() if isinstance(op, dict)]


def test_every_operation_has_a_unique_operation_id():
    ops = _operations()
    ids = [op.get('operationId') for _, _, op in ops]
    assert all(ids)
    assert len(ids) == len(set(ids))


def test_operation_ids_come_from_the_view_functions():
    ids = {(m, p): op['operationId'] for m, p, op in _operations()}
    assert ids[('get', '/recordings')] == 'list_recordings'
    assert ids[('get', '/search')] == 'search_recordings_v1'
