"""Voice profiles: samples, variants, matching and embedding spaces.

A person's voice profile is the set of samples stored for them in
SpeakerVoiceSample: one L2-normalized embedding per (recording, diarization
label) they were named in. Nothing is averaged in place. Voice variants (for
example the same person on a phone and in a meeting room) are clustered from
the samples whenever they are needed, and a new voice is compared with every
variant, the closest one counting. Because the variants are always rebuilt
from the samples, removing a wrongly assigned sample undoes it completely.

Embeddings are only ever compared within one embedding space (one model; see
models/voice.py and voice_embedding_check.py). A NULL space means the legacy
space: everything stored before spaces existed.

Profiles built before samples existed live in Speaker.average_embedding. They
are read as one "legacy" sample, weighted by how many recordings built them,
and written out as a real sample the first time the person gets a new one.
Speaker.average_embedding, embedding_count and embeddings_history are kept up
to date from the samples for the code and API fields that still read them.
"""

import logging
import os
import time
from datetime import datetime

import numpy as np

from src.database import db

logger = logging.getLogger(__name__)

# A diarization label with less speech than this gives a noisy embedding; it
# neither trains a profile nor gets auto-labelled.
MIN_SPEECH_SECONDS = float(os.environ.get('VOICE_PROFILE_MIN_SPEECH_SECONDS', '15'))
# Samples kept per person and space; the oldest go first.
MAX_SAMPLES = 20
# Samples at least this similar belong to the same voice variant.
VARIANT_MERGE_SIMILARITY = 0.72
MAX_VARIANTS = 4
# A new sample less similar than this to every sample a well-established
# profile already has is almost certainly a different person: kept out and
# logged as a likely wrong name.
OUTLIER_SIMILARITY = 0.25
OUTLIER_MIN_SAMPLES = 3
# A variant backed by a single sample of a person who has others counts a
# little less than an established one.
SINGLETON_PENALTY = 0.03
AUTO_SAMPLE_WEIGHT = 0.5
LEGACY_MAX_WEIGHT = 5.0

DEFAULT_SUGGESTION_THRESHOLD = 0.60
DEFAULT_AUTO_LABEL_THRESHOLDS = {'low': 0.3, 'medium': 0.6, 'high': 0.8}
AMBIGUITY_MARGIN = 0.05

SETTING_CURRENT_SPACE = 'voice_space_current'
SETTING_LEGACY_SPACE = 'voice_space_legacy'


# ------------------------------------------------------------------- vectors

def normalize(vector):
    """Float32 unit vector, or None for anything that is not a usable embedding."""
    try:
        arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    except (TypeError, ValueError):
        return None
    if arr.size == 0 or not np.all(np.isfinite(arr)):
        return None
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        return None
    return arr / norm


def to_bytes(unit_vector):
    return np.asarray(unit_vector, dtype=np.float32).tobytes()


def from_bytes(data):
    return np.frombuffer(data, dtype=np.float32).copy()


def cosine(a, b):
    if a is None or b is None or a.shape != b.shape:
        return None
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def load_embeddings(recording):
    """The recording's {label: vector} map, whatever form it was stored in."""
    import json
    data = recording.speaker_embeddings
    if not data:
        return {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return {}
    return data if isinstance(data, dict) else {}


def speech_seconds_by_label(segments):
    """Seconds of speech per speaker value in a JSON transcript.

    Returns None when the segments carry no usable timing, so callers can
    tell "no timing" from "no speech".
    """
    totals = {}
    timed = False
    for seg in segments or []:
        start, end = seg.get('start_time', seg.get('startTime')), seg.get('end_time', seg.get('endTime'))
        spk = seg.get('speaker')
        if not spk or start is None or end is None:
            continue
        try:
            dur = max(0.0, float(end) - float(start))
        except (TypeError, ValueError):
            continue
        timed = True
        totals[spk] = totals.get(spk, 0.0) + dur
    return totals if timed else None


def enough_speech(seconds_by_label, label):
    """True when the label has enough speech, or when timing is unknown."""
    if seconds_by_label is None:
        return True
    return seconds_by_label.get(label, 0.0) >= MIN_SPEECH_SECONDS


# -------------------------------------------------------------------- spaces

def _disabled():
    return os.environ.get('DISABLE_VOICE_EMBEDDING_CHECK', 'false').lower() == 'true'


def _setting_int(key):
    from src.models import SystemSetting
    value = SystemSetting.get_setting(key, None)
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def current_space_id():
    """Space of the backend in use now, or None when it cannot be known
    (check disabled, backend without embeddings, or never probed)."""
    if _disabled():
        return None
    return _setting_int(SETTING_CURRENT_SPACE)


def legacy_space_id():
    """The space everything stored without a space belongs to."""
    return _setting_int(SETTING_LEGACY_SPACE)


def effective_space(space_id):
    return space_id if space_id is not None else legacy_space_id()


def register_space(vector, fingerprint=None, similarity_threshold=0.98):
    """Find the space whose canary embedding matches this one, or create it.

    Makes it the current space and returns its id. The first space ever
    registered becomes the legacy space. Called by the canary check.
    """
    from src.models import SystemSetting, VoiceEmbeddingSpace
    vec = normalize(vector)
    if vec is None:
        return None
    best, best_sim = None, -1.0
    for space in VoiceEmbeddingSpace.query.all():
        ref = normalize(space.canary_embedding or [])
        sim = cosine(vec, ref) if ref is not None else None
        if sim is not None and sim > best_sim:
            best, best_sim = space, sim
    if best is not None and best_sim >= similarity_threshold:
        space = best
        space.last_seen_at = datetime.utcnow()
        if fingerprint:
            space.backend_fingerprint = fingerprint
        db.session.commit()
    else:
        space = VoiceEmbeddingSpace(dimension=int(vec.shape[0]), canary_embedding=[float(x) for x in vector],
                                    backend_fingerprint=fingerprint)
        db.session.add(space)
        db.session.commit()
        logger.info(f"Registered voice embedding space {space.id} ({space.dimension} dimensions)")
    if legacy_space_id() is None:
        SystemSetting.set_setting(SETTING_LEGACY_SPACE, str(space.id), setting_type='string',
                                  description='Voice embedding space that profiles stored before spaces existed belong to')
    SystemSetting.set_setting(SETTING_CURRENT_SPACE, str(space.id), setting_type='string',
                              description='Voice embedding space of the transcription backend in use')
    _calibration_cache.clear()
    return space.id


def clear_current_space():
    """The backend returns no embeddings: nothing new gets a space."""
    from src.models import SystemSetting
    if _setting_int(SETTING_CURRENT_SPACE) is not None:
        SystemSetting.set_setting(SETTING_CURRENT_SPACE, '', setting_type='string',
                                  description='Voice embedding space of the transcription backend in use')


# ------------------------------------------------------------------- samples

class _Sample:
    """What matching needs from a sample; also stands in for a legacy average."""
    __slots__ = ('vector', 'weight', 'row', 'created_at')

    def __init__(self, vector, weight, row=None, created_at=None):
        self.vector, self.weight, self.row, self.created_at = vector, weight, row, created_at


def _legacy_sample(speaker):
    if not speaker.average_embedding:
        return None
    try:
        vec = normalize(from_bytes(speaker.average_embedding))
    except ValueError as e:
        # Unreadable stored bytes: skipped, but said so rather than silently.
        logger.warning("Skipping speaker %s: its stored embedding could not be compared: %s",
                       getattr(speaker, 'id', '?'), e)
        return None
    if vec is None:
        return None
    weight = float(min(max(speaker.embedding_count or 1, 1), LEGACY_MAX_WEIGHT))
    return _Sample(vec, weight, None, speaker.created_at)


def samples_for(speaker, space_id, rows=None):
    """The person's samples in one (effective) space, legacy average included."""
    from src.models import SpeakerVoiceSample
    target = effective_space(space_id)
    if rows is None:
        rows = SpeakerVoiceSample.query.filter_by(speaker_id=speaker.id).all()
    out = []
    for row in rows:
        if effective_space(row.space_id) != target:
            continue
        try:
            vec = from_bytes(row.embedding)
        except ValueError:
            continue
        if vec.size:
            out.append(_Sample(vec, float(row.weight or 1.0), row, row.created_at))
    if not rows:
        legacy = _legacy_sample(speaker)
        if legacy is not None and target == legacy_space_id():
            out.append(legacy)
    return out


def build_variants(samples):
    """Cluster samples into voice variants.

    Greedy and deterministic: samples are visited oldest first and join the
    closest variant when at least VARIANT_MERGE_SIMILARITY similar to its
    centroid, otherwise start a new one. Beyond MAX_VARIANTS the two closest
    variants merge. Returns [{'centroid', 'support', 'count'}].
    """
    variants = []
    for s in sorted(samples, key=lambda x: x.created_at or datetime.min):
        best, best_sim = None, -1.0
        for v in variants:
            sim = float(np.dot(s.vector, v['centroid']))
            if sim > best_sim:
                best, best_sim = v, sim
        if best is not None and best_sim >= VARIANT_MERGE_SIMILARITY and best['centroid'].shape == s.vector.shape:
            best['sum'] = best['sum'] + s.weight * s.vector
            best['support'] += s.weight
            best['count'] += 1
            best['centroid'] = best['sum'] / np.linalg.norm(best['sum'])
        else:
            variants.append({'sum': s.weight * s.vector, 'centroid': s.vector.copy(),
                             'support': s.weight, 'count': 1})
    while len(variants) > MAX_VARIANTS:
        pair, best_sim = None, -2.0
        for i in range(len(variants)):
            for j in range(i + 1, len(variants)):
                sim = float(np.dot(variants[i]['centroid'], variants[j]['centroid']))
                if sim > best_sim:
                    pair, best_sim = (i, j), sim
        i, j = pair
        merged_sum = variants[i]['sum'] + variants[j]['sum']
        merged = {'sum': merged_sum, 'centroid': merged_sum / np.linalg.norm(merged_sum),
                  'support': variants[i]['support'] + variants[j]['support'],
                  'count': variants[i]['count'] + variants[j]['count']}
        variants = [v for k, v in enumerate(variants) if k not in (i, j)] + [merged]
    return [{'centroid': v['centroid'], 'support': v['support'], 'count': v['count']} for v in variants]


def score_against(vector, variants, total_samples):
    """Similarity of a voice to a person: the closest variant counts."""
    best = None
    for v in variants:
        if v['centroid'].shape != vector.shape:
            continue
        sim = float(np.dot(vector, v['centroid']))
        if v['count'] == 1 and total_samples > 1:
            sim -= SINGLETON_PENALTY
        best = sim if best is None else max(best, sim)
    return best


def _materialize_legacy(speaker):
    """Write a speaker's pre-sample average out as a real sample, once."""
    from src.models import SpeakerVoiceSample
    if SpeakerVoiceSample.query.filter_by(speaker_id=speaker.id).count():
        return
    legacy = _legacy_sample(speaker)
    if legacy is None:
        return
    db.session.add(SpeakerVoiceSample(
        user_id=speaker.user_id, speaker_id=speaker.id, recording_id=None, label=None,
        embedding=to_bytes(legacy.vector), dimension=int(legacy.vector.shape[0]),
        space_id=None, speech_seconds=None, source='legacy', weight=legacy.weight,
        created_at=speaker.created_at or datetime.utcnow()))
    db.session.flush()


def record_sample(speaker, recording, label, vector, speech_seconds=None, source='confirmed'):
    """Assign one (recording, label) voice to a person.

    Replaces whatever sample that (recording, label) had, so re-saving or
    correcting a name never double-counts. Returns 'stored', 'rejected'
    (outlier) or 'invalid'. Does not commit.
    """
    from src.models import SpeakerVoiceSample
    vec = normalize(vector)
    if vec is None:
        return 'invalid'
    space = recording.speaker_embeddings_space_id
    existing = SpeakerVoiceSample.query.filter_by(
        user_id=speaker.user_id, recording_id=recording.id, label=label).first()

    _materialize_legacy(speaker)
    others = [s for s in samples_for(speaker, space)
              if not (existing is not None and s.row is not None and s.row.id == existing.id)]
    if len(others) >= OUTLIER_MIN_SAMPLES:
        best = max((float(np.dot(vec, s.vector)) for s in others if s.vector.shape == vec.shape), default=None)
        if best is not None and best < OUTLIER_SIMILARITY:
            logger.warning(
                "Voice of %s in recording %s is unlike every sample of '%s' (best similarity %.2f); "
                "not adding it to the profile. The name may be wrong.",
                label, recording.id, speaker.name, best)
            if existing is not None:
                db.session.delete(existing)
            return 'rejected'

    weight = AUTO_SAMPLE_WEIGHT if source == 'auto' else 1.0
    if existing is None:
        existing = SpeakerVoiceSample(user_id=speaker.user_id, recording_id=recording.id, label=label)
        db.session.add(existing)
    existing.speaker_id = speaker.id
    existing.embedding = to_bytes(vec)
    existing.dimension = int(vec.shape[0])
    existing.space_id = space
    existing.speech_seconds = speech_seconds
    existing.source = source
    existing.weight = weight
    existing.updated_at = datetime.utcnow()
    db.session.flush()
    _trim(speaker, space)
    _calibration_cache.clear()
    return 'stored'


def _trim(speaker, space):
    from src.models import SpeakerVoiceSample
    target = effective_space(space)
    rows = [r for r in SpeakerVoiceSample.query.filter_by(speaker_id=speaker.id).all()
            if effective_space(r.space_id) == target]
    if len(rows) <= MAX_SAMPLES:
        return
    rows.sort(key=lambda r: r.created_at or datetime.min)
    for r in rows[:len(rows) - MAX_SAMPLES]:
        db.session.delete(r)
    db.session.flush()


def refresh_speaker_summary(speaker):
    """Keep the pre-sample columns true to the samples.

    average_embedding: weighted mean of the samples in the current space (or
    the legacy space when there is no current one), for API fields and any
    code that still reads it. embedding_count: samples in all spaces.
    embeddings_history: the last ten sample sources, which the orphaned-speaker
    cleanup reads. Does not commit.
    """
    from src.models import SpeakerVoiceSample
    rows = SpeakerVoiceSample.query.filter_by(speaker_id=speaker.id).all()
    if not rows:
        speaker.average_embedding = None
        speaker.embedding_count = 0
        speaker.embeddings_history = None
        speaker.confidence_score = None
        return
    space = current_space_id()
    chosen = samples_for(speaker, space, rows) or samples_for(speaker, None, rows)
    if chosen:
        dims = chosen[0].vector.shape
        same = [s for s in chosen if s.vector.shape == dims]
        total = sum(s.weight * s.vector for s in same)
        norm = float(np.linalg.norm(total))
        speaker.average_embedding = to_bytes(total / norm) if norm else None
        centroid = total / norm if norm else None
        sims = [float(np.dot(s.vector, centroid)) for s in same] if centroid is not None else []
        cohesion = sum(sims) / len(sims) if sims else 0.5
        speaker.confidence_score = round(min(1.0, max(0.0, cohesion * min(1.0, len(same) / 5.0))), 3)
    speaker.embedding_count = len(rows)
    history = []
    for r in sorted(rows, key=lambda r: r.updated_at or r.created_at or datetime.min)[-10:]:
        history.append({'recording_id': r.recording_id,
                        'timestamp': (r.updated_at or r.created_at or datetime.utcnow()).isoformat(),
                        'source': r.source})
    speaker.embeddings_history = history


def voice_summary(speaker, rows=None):
    """Counts for the speakers list: samples and variants per space."""
    from src.models import SpeakerVoiceSample
    if rows is None:
        rows = SpeakerVoiceSample.query.filter_by(speaker_id=speaker.id).all()
    space = current_space_id()
    current = samples_for(speaker, space, rows)
    stored_current = len([s for s in current if s.row is not None])
    return {
        'sample_count': len(rows) if rows else (1 if current else 0),
        'current_space_samples': len(current),
        'variant_count': len(build_variants(current)) if current else 0,
        'other_space_samples': len(rows) - stored_current if rows else 0,
    }


# ------------------------------------------------------------------ matching

def _user_profiles(user_id, space_id, exclude_speaker_ids=()):
    from src.models import Speaker, SpeakerVoiceSample
    speakers = Speaker.query.filter_by(user_id=user_id).all()
    rows_by_speaker = {}
    for row in SpeakerVoiceSample.query.filter_by(user_id=user_id).all():
        rows_by_speaker.setdefault(row.speaker_id, []).append(row)
    profiles = []
    for sp in speakers:
        if sp.id in exclude_speaker_ids:
            continue
        rows = rows_by_speaker.get(sp.id, [])
        samples = samples_for(sp, space_id, rows)
        if not samples:
            continue
        profiles.append((sp, build_variants(samples), len(samples)))
    return profiles


def find_matches(vector, user_id, space_id=None, threshold=None):
    """People whose voice matches, best first.

    [{'speaker_id', 'name', 'similarity' (percent), 'confidence',
      'embedding_count', 'variant_count'}]. Only profiles in the same
    embedding space and dimension take part.
    """
    vec = normalize(vector)
    if vec is None:
        return []
    if threshold is None:
        threshold = suggestion_threshold(space_id)
    matches = []
    profiles = _user_profiles(user_id, space_id)
    mismatched_dims = set()
    mismatched = 0
    for sp, variants, count in profiles:
        if not any(v['centroid'].shape == vec.shape for v in variants):
            mismatched += 1
            mismatched_dims.update(int(v['centroid'].shape[0]) for v in variants)
            continue
        sim = score_against(vec, variants, count)
        if sim is not None and sim >= threshold:
            matches.append({'speaker_id': sp.id, 'name': sp.name, 'similarity': round(sim * 100, 1),
                            'confidence': sp.confidence_score or 0.5, 'embedding_count': count,
                            'variant_count': len(variants)})
    if mismatched:
        # One line per call, so it stays readable when a whole library is
        # affected, which is the usual case.
        sizes = ', '.join(str(d) for d in sorted(mismatched_dims))
        logger.error(
            "%d of %d stored voice profiles have %s dimensions and cannot be compared "
            "with the %d-dimensional embeddings this backend now returns. Voice matching "
            "will find nothing until the previous transcription backend is restored or "
            "the profiles are rebuilt.",
            mismatched, len(profiles), sizes, vec.shape[0])
    return sorted(matches, key=lambda m: m['similarity'], reverse=True)


def forget_recording_samples(recording):
    """Drop a recording's voice samples and label map (it was re-diarized)."""
    from src.models import Speaker, SpeakerVoiceSample
    rows = SpeakerVoiceSample.query.filter_by(recording_id=recording.id).all()
    touched = {r.speaker_id for r in rows}
    for r in rows:
        db.session.delete(r)
    recording.speaker_label_map = None
    db.session.flush()
    for sid in touched:
        sp = db.session.get(Speaker, sid)
        if sp is not None:
            refresh_speaker_summary(sp)
    if touched:
        _calibration_cache.clear()


def suggestions_for_recording(recording, user_id, threshold=None):
    """Voice-match suggestions per label, keyed the way the transcript shows it.

    A label already renamed in the transcript is keyed by its current name,
    so the speaker dialog can offer a confirmation for it.
    """
    import json
    embeddings = load_embeddings(recording)
    label_map = recording.speaker_label_map or {}
    present = set()
    try:
        present = {s.get('speaker') for s in json.loads(recording.transcription or '[]') if isinstance(s, dict)}
    except (TypeError, ValueError):
        pass
    out = {}
    for label, vector in embeddings.items():
        key = label if label in present else label_map.get(label, label)
        out[key] = find_matches(vector, user_id, recording.speaker_embeddings_space_id, threshold)
    return out


def auto_label_map(recording, user, threshold):
    """{label: name} for a new recording: one person per label, one label per person.

    Labels with too little speech are skipped, as is any label whose two best
    candidates are within AMBIGUITY_MARGIN. The remaining pairs are assigned
    greedily from the most similar down, so no person gets two labels.
    """
    import json
    embeddings = load_embeddings(recording)
    if not embeddings:
        return {}
    try:
        seconds = speech_seconds_by_label(json.loads(recording.transcription or '[]'))
    except (TypeError, ValueError):
        seconds = None
    candidates = []
    for label, vector in embeddings.items():
        if not enough_speech(seconds, label):
            continue
        matches = find_matches(vector, user.id, recording.speaker_embeddings_space_id, threshold)
        if not matches:
            continue
        if len(matches) >= 2 and (matches[0]['similarity'] - matches[1]['similarity']) / 100.0 <= AMBIGUITY_MARGIN:
            continue
        for m in matches:
            candidates.append((m['similarity'], label, m['speaker_id'], m['name']))
    result, used_people = {}, set()
    for sim, label, speaker_id, name in sorted(candidates, reverse=True):
        if label in result or speaker_id in used_people:
            continue
        result[label] = name
        used_people.add(speaker_id)
    return result


# ------------------------------------------------------------- calibration

_calibration_cache = {}
_CALIBRATION_TTL = 600
MIN_CALIBRATION_PAIRS = 10


def calibrated_threshold(space_id):
    """A match threshold learned from this space's own samples, or None.

    Genuine scores: each sample against its own person's other samples.
    Impostor scores: each sample against the other people of the same user.
    The threshold sits midway between the 10th percentile of genuine scores
    and the 95th percentile of impostor scores, within [0.45, 0.85]. None
    until there are MIN_CALIBRATION_PAIRS of each.
    """
    from src.models import Speaker, SpeakerVoiceSample
    target = effective_space(space_id)
    cached = _calibration_cache.get(target)
    if cached and time.time() - cached[0] < _CALIBRATION_TTL:
        return cached[1]
    by_user = {}
    for row in SpeakerVoiceSample.query.all():
        if effective_space(row.space_id) != target:
            continue
        vec = from_bytes(row.embedding)
        if vec.size:
            by_user.setdefault(row.user_id, {}).setdefault(row.speaker_id, []).append(vec)
    genuine, impostor = [], []
    for people in by_user.values():
        # One similarity matrix per user and dimension; only equal-size
        # vectors are ever compared.
        by_dim = {}
        for pid, vecs in people.items():
            for v in vecs:
                by_dim.setdefault(v.shape[0], ([], []))
                by_dim[v.shape[0]][0].append(v)
                by_dim[v.shape[0]][1].append(pid)
        for vecs, pids in by_dim.values():
            if len(vecs) < 2:
                continue
            m = np.vstack(vecs)
            sims = m @ m.T
            pids = np.asarray(pids)
            same = pids[:, None] == pids[None, :]
            np.fill_diagonal(same, False)
            other = pids[:, None] != pids[None, :]
            own_best = np.where(same, sims, -np.inf).max(axis=1)
            other_best = np.where(other, sims, -np.inf).max(axis=1)
            genuine.extend(float(x) for x in own_best[np.isfinite(own_best)])
            impostor.extend(float(x) for x in other_best[np.isfinite(other_best)])
    value = None
    if len(genuine) >= MIN_CALIBRATION_PAIRS and len(impostor) >= MIN_CALIBRATION_PAIRS:
        g10 = float(np.percentile(genuine, 10))
        i95 = float(np.percentile(impostor, 95))
        value = round(min(0.85, max(0.45, (g10 + i95) / 2.0)), 3)
    _calibration_cache[target] = (time.time(), value)
    return value


def suggestion_threshold(space_id):
    cal = calibrated_threshold(space_id)
    return round(cal - 0.10, 3) if cal is not None else DEFAULT_SUGGESTION_THRESHOLD


def auto_label_threshold(setting, space_id):
    cal = calibrated_threshold(space_id)
    if cal is None:
        return DEFAULT_AUTO_LABEL_THRESHOLDS.get(setting or 'medium', DEFAULT_AUTO_LABEL_THRESHOLDS['medium'])
    offsets = {'low': -0.10, 'medium': 0.0, 'high': 0.08}
    return round(min(0.95, max(0.3, cal + offsets.get(setting or 'medium', 0.0))), 3)


# ------------------------------------------------------ applying names

def _labels_for(key, embeddings, label_map):
    """The diarization labels a speaker value in the transcript stands for."""
    if key in embeddings:
        return [key]
    return [lab for lab, name in label_map.items() if name == key and lab in embeddings]


def record_label_names(recording, key_to_name):
    """Update only the recording's label map with the names given in a save.

    Used when training fails, so the map still follows the transcript and a
    later correction can reach the embedding. Does not commit.
    """
    embeddings = load_embeddings(recording)
    label_map = dict(recording.speaker_label_map or {})
    for key, name in key_to_name.items():
        for label in _labels_for(key, embeddings, label_map):
            label_map[label] = name
    recording.speaker_label_map = label_map or None


def apply_names_to_profiles(recording, key_to_name, seconds_by_key, user, source='confirmed'):
    """Train voice profiles from the names given in one save.

    key_to_name maps the speaker values the transcript showed before the save
    (a diarization label, or a name given earlier) to the name now assigned.
    Each key is traced back to its diarization label through the recording's
    label map, so a correction reaches the embedding even after the transcript
    shows names. Labels that no longer appear in the transcript (merged away)
    lose their sample. Only the saving user's samples are read or changed:
    each user who can edit a recording keeps their own. Updates the label map and the affected people's summary
    columns. Does not commit.

    Returns {'stored': n, 'rejected': n, 'skipped_short': n}.
    """
    import json
    from src.models import SpeakerVoiceSample
    from src.services.speaker import find_user_speaker

    stats = {'stored': 0, 'rejected': 0, 'skipped_short': 0}
    embeddings = load_embeddings(recording)
    label_map = dict(recording.speaker_label_map or {})

    touched = set()
    for key, name in key_to_name.items():
        for label in _labels_for(key, embeddings, label_map):
            label_map[label] = name
            if not embeddings.get(label):
                continue
            seconds = (seconds_by_key or {}).get(key) if seconds_by_key is not None else None
            if seconds_by_key is not None and (seconds or 0.0) < MIN_SPEECH_SECONDS:
                stats['skipped_short'] += 1
                old = SpeakerVoiceSample.query.filter_by(user_id=user.id, recording_id=recording.id, label=label).first()
                if old is not None:
                    touched.add(old.speaker_id)
                    db.session.delete(old)
                continue
            speaker = find_user_speaker(user.id, name)
            if speaker is None:
                continue
            old = SpeakerVoiceSample.query.filter_by(user_id=user.id, recording_id=recording.id, label=label).first()
            if old is not None and old.speaker_id != speaker.id:
                touched.add(old.speaker_id)
            result = record_sample(speaker, recording, label, embeddings[label], seconds, source)
            if result in stats:
                stats[result] += 1
            touched.add(speaker.id)

    # Labels whose voice no longer appears in the transcript lose their sample.
    try:
        present = {s.get('speaker') for s in json.loads(recording.transcription or '[]') if isinstance(s, dict)}
    except (TypeError, ValueError):
        present = None
    # A person renamed since the map was written still counts as present.
    if present is not None:
        from src.models import Speaker as _Speaker
        for row in SpeakerVoiceSample.query.filter_by(user_id=user.id, recording_id=recording.id).all():
            shown = label_map.get(row.label, row.label)
            person = db.session.get(_Speaker, row.speaker_id) if row.speaker_id else None
            if row.label not in present and shown not in present and \
                    not (person is not None and person.name in present):
                touched.add(row.speaker_id)
                db.session.delete(row)

    recording.speaker_label_map = label_map or None
    db.session.flush()
    from src.models import Speaker
    for sid in touched:
        sp = db.session.get(Speaker, sid)
        if sp is not None:
            refresh_speaker_summary(sp)
    return stats
