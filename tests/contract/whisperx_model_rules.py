"""How whisperx-asr-service chooses the model for a request, vendored for tests.

Speakr's fake ASR server (tests/contract/fake_asr_server.py) answers with these
rules, so Speakr's tests fail when Speakr sends a request the real service
would reject. test_whisperx_contract.py checks this copy against the service's
own code (WHISPERX_REPO) so the two cannot drift apart unnoticed.

Two service generations are modelled:

- ``fixed`` (0.4.2 onward): an empty or whitespace request model uses
  DEFAULT_MODEL, which is DEFAULT_MODEL / PRELOAD_MODEL / large-v3 and never
  empty; unknown names are rejected with HTTP 400.
- ``legacy`` (0.2.0 to 0.4.1): the default is PRELOAD_MODEL as compose passes
  it, which is an empty string when .env does not set it (Speakr #409), and an
  unknown name reaches faster-whisper, which fails with HTTP 500.
"""

import os

BUILTIN_DEFAULT_MODEL = "large-v3"

# faster_whisper.available_models() in the service image (faster-whisper 1.2.1).
CANONICAL_MODELS = (
    "tiny.en", "tiny", "base.en", "base", "small.en", "small",
    "medium.en", "medium", "large-v1", "large-v2", "large-v3", "large",
    "distil-large-v2", "distil-medium.en", "distil-small.en",
    "distil-large-v3", "distil-large-v3.5", "large-v3-turbo", "turbo",
)

STATIC_ALIASES = {
    "whisper-large-v3": "large-v3",
    "whisper-large-v2": "large-v2",
    "whisper-medium": "medium",
    "whisper-small": "small",
    "whisper-base": "base",
    "whisper-tiny": "tiny",
}

_HF_REPO_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


class ModelRejected(Exception):
    """The service would refuse this request; ``status`` is its HTTP code."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _env_or_none(value):
    if value is None:
        return None
    value = value.strip()
    return value or None


def is_hf_repo_id(name):
    parts = name.split("/")
    return len(parts) == 2 and all(parts) and all(set(p) <= _HF_REPO_CHARS for p in parts)


def is_loadable_model(name):
    return bool(name) and (name in CANONICAL_MODELS or is_hf_repo_id(name) or os.path.isdir(name))


def _strip_whisper_prefix(name):
    if name.startswith("whisper-") and name[len("whisper-"):] in CANONICAL_MODELS:
        return name[len("whisper-"):]
    return name


def resolve_default(raw, openai_whisper1=None):
    """The fixed service's DEFAULT_MODEL for a configured value (never empty)."""
    name = (raw or "").strip()
    if name == "whisper-1":
        name = _env_or_none(openai_whisper1) or BUILTIN_DEFAULT_MODEL
    name = STATIC_ALIASES.get(name, name)
    name = _strip_whisper_prefix(name)
    return name if is_loadable_model(name) else BUILTIN_DEFAULT_MODEL


class ServiceModelRules:
    """The model decision the service makes for one /asr request.

    ``env`` holds the service's container environment as compose delivers it:
    a key present with value "" is how compose forwards a variable that the
    .env file does not set.
    """

    def __init__(self, env=None, generation="fixed"):
        env = dict(env or {})
        self.generation = generation
        if generation == "fixed":
            preload = _env_or_none(env.get("PRELOAD_MODEL"))
            configured = _env_or_none(env.get("DEFAULT_MODEL")) or preload or BUILTIN_DEFAULT_MODEL
            whisper1 = _env_or_none(env.get("OPENAI_WHISPER1_MODEL"))
            self.default_model = resolve_default(configured, whisper1)
            self.whisper1 = resolve_default(whisper1) if whisper1 else self.default_model
            allowed = []
            for entry in (_env_or_none(env.get("ALLOWED_MODELS")) or "").split(","):
                entry = entry.strip()
                if entry:
                    mapped = self._map_alias(entry)
                    if mapped not in allowed:
                        allowed.append(mapped)
            self.allowed = allowed
        elif generation == "legacy":
            # os.getenv("PRELOAD_MODEL", "large-v3"): the fallback only applies
            # when the variable is absent, not when compose forwards it empty.
            self.default_model = env["PRELOAD_MODEL"] if "PRELOAD_MODEL" in env else BUILTIN_DEFAULT_MODEL
            self.whisper1 = env.get("OPENAI_WHISPER1_MODEL", self.default_model)
            self.allowed = []
        else:
            raise ValueError(f"unknown service generation {generation!r}")

    def _map_alias(self, name):
        if name == "whisper-1":
            return self.whisper1
        if name in STATIC_ALIASES:
            return STATIC_ALIASES[name]
        return _strip_whisper_prefix(name)

    def resolve(self, model):
        """Name the service loads for a request's ``model``; raises ModelRejected."""
        if self.generation == "legacy":
            # No stripping; an empty model returns DEFAULT_MODEL unmapped.
            name = self._map_alias(model) if model else self.default_model
            if not is_loadable_model(name):
                raise ModelRejected(
                    500,
                    f"Invalid model size {name!r}, expected one of: {', '.join(CANONICAL_MODELS)}",
                )
            return name
        name = (model or "").strip()
        if not name:
            return self.default_model
        resolved = self._map_alias(name)
        if not is_loadable_model(resolved):
            raise ModelRejected(400, f"Unknown model {name!r}.")
        if self.allowed and resolved not in self.allowed and resolved != self.default_model:
            raise ModelRejected(400, f"Model {name!r} is not allowed on this server.")
        return resolved
