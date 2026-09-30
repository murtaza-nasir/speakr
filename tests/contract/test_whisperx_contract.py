"""The vendored whisperx model rules agree with the service's own code.

tests/contract/whisperx_model_rules.py is what Speakr's fake ASR server
answers with. This test imports the real app.pipeline from a
whisperx-asr-service checkout (WHISPERX_REPO) and compares its decisions with
the vendored copy on a table of service settings and request models, so a
change in the service's rules fails Speakr's CI and not a user's install.

Each comparison runs in a subprocess: the service's pipeline needs its
stand-in ML packages (tests/e2e/stubs in the service repo) or the real ones,
and neither may leak into Speakr's own test process.

Skipped when WHISPERX_REPO is not set. CI checks the service out, sets it,
and sets WHISPERX_PYTHON to the interpreter with the service's CI
requirements installed (tests/requirements-ci.txt in the service repo).
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.contract.whisperx_model_rules import ModelRejected, ServiceModelRules, is_loadable_model

WHISPERX_REPO = os.environ.get("WHISPERX_REPO", "").strip()

pytestmark = pytest.mark.skipif(
    not WHISPERX_REPO, reason="set WHISPERX_REPO to a whisperx-asr-service checkout")

MODEL_ENV = ("PRELOAD_MODEL", "DEFAULT_MODEL", "OPENAI_WHISPER1_MODEL", "ALLOWED_MODELS",
             "MAX_LOADED_MODELS", "ASR_BACKEND")

# Service environments as compose delivers them: a key with "" is a variable
# the .env file does not set.
SERVICE_ENVS = {
    "unset": {},
    "compose-empty (issue #409)": {"PRELOAD_MODEL": ""},
    "whitespace": {"PRELOAD_MODEL": "  "},
    "documented large-v3": {"PRELOAD_MODEL": "large-v3"},
    "preload medium": {"PRELOAD_MODEL": "medium"},
    "no preload, default small": {"PRELOAD_MODEL": "", "DEFAULT_MODEL": "small"},
    "alias as preload": {"PRELOAD_MODEL": "whisper-large-v2"},
    "typo as preload": {"PRELOAD_MODEL": "large-v4"},
    "whisper-1 pointing at itself": {"PRELOAD_MODEL": "", "OPENAI_WHISPER1_MODEL": "whisper-1"},
    "allowlist": {"PRELOAD_MODEL": "large-v3", "ALLOWED_MODELS": "small, whisper-medium"},
}

REQUEST_MODELS = [
    None, "", "   ", "large-v3", " small ", "turbo", "distil-large-v3.5", "medium.en",
    "whisper-1", "whisper-large-v3", "whisper-medium", "whisper-small.en",
    "Systran/faster-whisper-small", "large-v4", "whisper-2", "org/repo/extra",
]

_PROBE = r"""
import json, os, sys
repo = os.environ["WHISPERX_REPO"]
sys.path.insert(0, repo)
try:
    import whisperx, faster_whisper  # noqa: F401  (real packages present: use them)
except Exception:
    sys.path.insert(0, os.path.join(repo, "tests", "e2e", "stubs"))
    for name in [m for m in sys.modules if m.split(".")[0] in ("torch", "whisperx", "faster_whisper")]:
        del sys.modules[name]
import app.pipeline as p
models = json.loads(os.environ["CONTRACT_MODELS"])
generation = "fixed" if hasattr(p, "InvalidModelError") else "legacy"
out = {"generation": generation, "default": p.DEFAULT_MODEL, "results": []}
for model in models:
    try:
        out["results"].append({"ok": p.resolve_model_name(model)})
    except Exception as e:
        out["results"].append({"error": type(e).__name__})
print("CONTRACT" + json.dumps(out))
"""


def _real_decisions(env):
    child_env = {k: v for k, v in os.environ.items() if k not in MODEL_ENV}
    child_env.update(env)
    child_env["WHISPERX_REPO"] = os.path.abspath(WHISPERX_REPO)
    child_env["CONTRACT_MODELS"] = json.dumps(REQUEST_MODELS)
    child_env.setdefault("DEVICE", "cpu")
    python = os.environ.get("WHISPERX_PYTHON") or sys.executable
    proc = subprocess.run([python, "-c", _PROBE], env=child_env, capture_output=True,
                          text=True, timeout=180)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("CONTRACT")]
    assert proc.returncode == 0 and lines, f"could not import the service's pipeline:\n{proc.stderr[-3000:]}"
    return json.loads(lines[-1][len("CONTRACT"):])


@pytest.mark.parametrize("label", list(SERVICE_ENVS))
def test_vendored_rules_match_the_service(label):
    env = SERVICE_ENVS[label]
    real = _real_decisions(env)
    rules = ServiceModelRules(env, generation=real["generation"])
    assert rules.default_model == real["default"], (label, "default model")

    for model, decision in zip(REQUEST_MODELS, real["results"]):
        try:
            expected = rules.resolve(model)
            rejected = None
        except ModelRejected as e:
            expected, rejected = None, e
        if real["generation"] == "fixed":
            if rejected is None:
                assert decision == {"ok": expected}, (label, model, decision)
            else:
                assert decision == {"error": "InvalidModelError"}, (label, model, decision)
        else:
            # The legacy service resolves any name and only faster-whisper
            # refuses it later (HTTP 500), which the vendored rules model as
            # a rejection of a name that is not loadable.
            assert "ok" in decision, (label, model, decision)
            if rejected is None:
                assert decision["ok"] == expected, (label, model, decision)
            else:
                assert rejected.status == 500 and not is_loadable_model(decision["ok"]), (label, model, decision)


def test_the_service_default_is_never_empty_on_current_releases():
    """The #409 regression, checked against the service itself."""
    real = _real_decisions({"PRELOAD_MODEL": ""})
    if real["generation"] == "legacy":
        pytest.skip("service checkout predates 0.4.2; its empty default is the #409 bug")
    assert real["default"] == "large-v3"
