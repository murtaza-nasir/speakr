"""python src/app.py loads the application file once.

Run as a script, the file used to go on as __main__ and was loaded a second
time, as src.app, by the first module that imports the application: two
application objects, the startup steps twice and two logger names in one log.
The script start now hands over to the module src.app before anything else.
"""

import ast
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'src', 'app.py')


def _is_main_check(node):
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == '__name__')


def test_the_script_start_is_the_first_statement_and_the_only_one():
    tree = ast.parse(open(APP, encoding='utf-8').read())
    checks = [i for i, node in enumerate(tree.body) if _is_main_check(node)]
    assert checks == [0]
    block = tree.body[0]
    # It ends by leaving, so nothing below it runs under the name __main__.
    assert isinstance(block.body[-1], ast.Raise)
    imported = [n for n in ast.walk(block) if isinstance(n, ast.ImportFrom)]
    assert [(n.module, [a.name for a in n.names]) for n in imported] == [('src.app', ['main'])]


def test_help_is_answered_without_loading_the_application():
    done = subprocess.run([sys.executable, APP, '--help'], capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert done.returncode == 0
    assert '--debug' in done.stdout
    assert 'Starting Up' not in done.stdout + done.stderr
