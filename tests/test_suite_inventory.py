"""Every test module under tests/ yields at least one test (#642).

pytest imports any tests/**/test_*.py and collects the functions whose name
starts with `test`. A module named like a test module that only holds a
script (its assertions under `if __name__ == "__main__"`) collects nothing,
and the suite stays green without running it. A name imported from another
test module counts, as in tests/e2e/test_image_smoke.py.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def collected_names(source):
    """The names of the tests a module defines or imports."""
    names = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
            names.append(node.name)
        elif isinstance(node, ast.ImportFrom):
            names.extend(alias.asname or alias.name for alias in node.names
                         if (alias.asname or alias.name).startswith('test'))
    return names


def modules_without_tests(paths):
    return [path for path in paths if not collected_names(path.read_text(encoding='utf-8'))]


def test_every_test_module_yields_at_least_one_case():
    paths = sorted(TESTS.rglob('test_*.py'))
    assert len(paths) > 100
    empty = [str(path.relative_to(TESTS.parent)) for path in modules_without_tests(paths)]
    assert empty == [], f'test modules without a test function: {empty}'


def test_a_module_with_tests_is_not_reported(tmp_path):
    good = tmp_path / 'test_good.py'
    good.write_text('def test_ok():\n    pass\n\n\nclass TestThing:\n    def test_method(self):\n        pass\n')
    imported = tmp_path / 'test_imported.py'
    imported.write_text('from test_good import test_ok  # noqa: F401\n')
    script = tmp_path / 'test_script.py'
    script.write_text('def run_tests():\n    assert True\n\n\nif __name__ == "__main__":\n    run_tests()\n')

    assert modules_without_tests([good, imported, script]) == [script]
