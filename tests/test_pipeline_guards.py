"""Guard: pipeline steps are only run through their shared functions (#412).

#412 came from a route that called the AI title generator directly and skipped
the naming template the processing task applied. Each step below has one home;
a new caller elsewhere fails this test, so a reprocess or regenerate path
cannot quietly re-implement a step and miss a setting added later.

- AI title + naming template: src/services/titling.py (compute_title). The
  template preview endpoint may apply a template to sample values.
- Events and team-tag auto-shares: finish_processing in src/tasks/processing.py,
  the last step of every processing path.
- Multi-event calendar files: src/services/calendar.py (generate_combined_ics).
"""

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

# call name -> allowed (relative file, enclosing function or None for any)
ALLOWED = {
    "_generate_ai_title": {("src/services/titling.py", "compute_title")},
    "extract_events_from_transcript": {("src/tasks/processing.py", "finish_processing")},
    "apply_team_tag_auto_shares": {("src/tasks/processing.py", "finish_processing")},
}
NAMING_TEMPLATE_APPLY = {("src/services/titling.py", "compute_title"),
                         ("src/api/naming_templates.py", "test_naming_template")}


def _calls():
    for dirpath, _, files in os.walk(SRC):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, ROOT)
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=rel)
            stack = []

            def visit(node):
                is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                if is_fn:
                    stack.append(node.name)
                if isinstance(node, ast.Call):
                    f = node.func
                    called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                    receiver = f.value if isinstance(f, ast.Attribute) else None
                    yield_ = (rel, stack[-1] if stack else None, called, receiver)
                    found.append(yield_)
                for child in ast.iter_child_nodes(node):
                    visit(child)
                if is_fn:
                    stack.pop()

            found = []
            visit(tree)
            yield from found


def test_steps_are_only_called_from_their_shared_home():
    offenders = []
    for rel, fn, called, _ in _calls():
        if called in ALLOWED and (rel, fn) not in ALLOWED[called]:
            offenders.append(f"{rel}:{fn} calls {called}")
    assert not offenders, "Call the shared pipeline step instead:\n" + "\n".join(offenders)


def test_naming_templates_are_applied_only_by_the_title_step():
    offenders = []
    for rel, fn, called, receiver in _calls():
        if called != "apply" or receiver is None:
            continue
        text = ast.unparse(receiver)
        if "template" in text.lower() and (rel, fn) not in NAMING_TEMPLATE_APPLY:
            offenders.append(f"{rel}:{fn} calls {text}.apply")
    assert not offenders, "Use src.services.titling.compute_title:\n" + "\n".join(offenders)


def test_calendar_files_are_built_in_one_place():
    for rel in ("src/api/api_v1.py", "src/api/events.py"):
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert "BEGIN:VEVENT" not in text, f"{rel} builds ICS itself; use generate_combined_ics"
