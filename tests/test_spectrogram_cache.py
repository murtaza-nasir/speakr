"""Full-range spectra are prepared once; cache reads never spawn media work."""
import time
import json
import multiprocessing
import threading
import wave
import os
import io
from unittest.mock import patch

import pytest
import numpy as np
from PIL import Image

from src.services import segment_spectrogram as spectra


def wait_ready(cache, result, path, principal=1, recording=9):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = cache.metadata(principal, recording, result['id'], path, result['lease'])
        if result['status'] == 'ready':
            return result
        if result['status'] == 'failed':
            pytest.fail(str(result))
        time.sleep(.02)
    pytest.fail('preparation did not finish')


def wait_worker_ack(cache, identifier):
    # Public ready can precede the worker's finally block. Inactive-TTL tests
    # must wait for that acknowledgement before advancing the cached timestamp.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with cache._state() as state:
            if not state['jobs'][identifier]['worker_active']:
                return
        time.sleep(.01)
    pytest.fail('ready worker did not acknowledge stopping')


def test_gain_cache_identity_applied_manifest_reuse_and_wrong_existing_id(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'gain-cache')
    path = str(source)
    normal = wait_ready(cache, cache.prepare(1, 9, path, 0, 2, '8000', 2), path)
    # Ready metadata can precede the worker's finally acknowledgement. A new
    # gain is a different render, so wait until its per-principal slot is free.
    wait_worker_ack(cache, normal['id'])
    brighter = wait_ready(cache, cache.prepare(1, 9, path, 0, 2, '8000', 2, gain_db=20), path)
    assert normal['manifest']['gainDb'] == 0 and brighter['manifest']['gainDb'] == 20
    assert normal['id'] != brighter['id']
    renders = json.loads((cache.root / 'state.json').read_text())['renderCount']
    reused = cache.prepare(1, 9, path, 0, 2, '8000', 2, gain_db=20)
    assert reused['id'] == brighter['id']
    assert json.loads((cache.root / 'state.json').read_text())['renderCount'] == renders
    with pytest.raises(spectra.SpectrogramError) as error:
        cache.prepare(1, 9, path, 0, 2, '8000', 2, existing_id=normal['id'], gain_db=20)
    assert error.value.status == 410


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'stereo.wav'
    t = np.arange(16000 * 121) / 16000
    left = (np.sin(2 * np.pi * 440 * t) + .3 * np.sin(2 * np.pi * 1900 * t)) * .4
    right = np.sin(2 * np.pi * 2200 * t) * .4
    signal = np.column_stack((left, right))
    with wave.open(str(path), 'wb') as file:
        file.setnchannels(2)
        file.setsampwidth(2)
        file.setframerate(16000)
        file.writeframes((signal * 32767).astype('<i2').tobytes())
    return path


def test_full_stereo_range_quarter_second_detail_cache_only_reads(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    started = time.monotonic()
    with patch.object(spectra, '_run', wraps=spectra._run) as run:
        result = wait_ready(cache, cache.prepare(1, 9, str(source), 1, 121, '8000', .25), str(source))
        manifest = result['manifest']
        assert manifest['start'] == 1 and manifest['end'] == 121
        assert manifest['channels'] == 2 and len(manifest['tiles']) == 120
        assert manifest['tiles'][0]['width'] == 4096
        render_count = len([call for call in run.call_args_list if call.args[0][0] == 'ffmpeg'])
        assert render_count == 120
        for tile in manifest['tiles'][::12] + [manifest['tiles'][-1]]:
            png = cache.tile(1, 9, result['id'], str(source), result['lease'], tile['index'])
            assert png.startswith(b'\x89PNG')
        again = cache.prepare(1, 9, str(source), 1, 121, '8000', .25)
        assert again['id'] == result['id'] and again['status'] == 'ready'
        assert len(run.call_args_list) == render_count + 1  # one metadata probe
    state = json.loads((cache.root / 'state.json').read_text())
    print(json.dumps({'fixture': '120s-stereo-span0.25', 'seconds': time.monotonic() - started,
                      'renders': state['renderCount'], 'peakAccountedBytes': state['peakBytes'],
                      'readyBytes': state['jobs'][result['id']]['bytes'], 'panRenderDelta': 0}))
    assert time.monotonic() - started < 180


def test_tail_context_lease_source_and_cache_miss_are_explicit(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    result = wait_ready(cache, cache.prepare(1, 9, str(source), 120.999, 122, '4000', .25), str(source))
    manifest = result['manifest']
    assert manifest['end'] == 121 and manifest['start'] == 120.75
    assert manifest['segmentStart'] == 120.999 and manifest['segmentEnd'] == 121
    assert manifest['tiles'][0]['contentStart'] == 120.999
    cache.release(1, 9, result['id'], result['lease'])
    with patch.object(spectra, '_run') as run:
        with pytest.raises(spectra.SpectrogramError) as error:
            cache.tile(1, 9, result['id'], str(source), result['lease'], 0)
        assert error.value.status == 410
        run.assert_not_called()
    result = cache.prepare(1, 9, str(source), 120.999, 122, '4000', .25)
    source.touch()
    with pytest.raises(spectra.SpectrogramError) as error:
        cache.metadata(1, 9, result['id'], str(source), result['lease'])
    assert error.value.code == 'changed'


def test_tiny_proportional_tail_and_nonzero_range(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    result = wait_ready(cache, cache.prepare(1, 9, str(source), 2, 3.005, '8000', .25), str(source))
    tiles = result['manifest']['tiles']
    assert [(tile['start'], tile['end']) for tile in tiles] == [(2, 3), (3, 3.005)]
    assert tiles[-1]['width'] == 21 and tiles[-1]['contentEnd'] == 3.005
    # Absolute colors and channel axes remain the same across tile seams.
    images = [np.asarray(Image.open(io.BytesIO(cache.tile(1, 9, result['id'], str(source), result['lease'], index))).convert('RGB')) for index in range(2)]
    assert images[0].shape == (384, 4096, 3) and images[1].shape == (384, 21, 3)
    # A 5ms tail is represented proportionally; its physical FFT frequency
    # resolution is not sufficient to identify the low440Hz tone reliably.
    for image in images[:1]:
        peaks = image[:, image.shape[1] // 2, :].max(axis=1)
        assert peaks[:192].argmax() > 160  # left440Hz nearbottomof8kHz
        assert 130 < peaks[192:].argmax() < 150  # right2200Hz


def test_full_tile_seams_preserve_channel_order_and_absolute_color(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    result = wait_ready(cache, cache.prepare(1, 9, str(source), 2, 4, '8000', .25), str(source))
    images = [np.asarray(Image.open(io.BytesIO(cache.tile(1, 9, result['id'], str(source), result['lease'], index))).convert('RGB')) for index in range(2)]
    assert np.abs(images[0][:, 2048].astype(int) - images[1][:, 2048].astype(int)).max() < 10
    for image in images:
        peaks = image[:, 2048, :].max(axis=1)
        assert peaks[:192].argmax() > 160
        assert 130 < peaks[192:].argmax() < 150


def test_subpixel_eof_tail_is_valid_one_pixel_png(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    result = wait_ready(cache, cache.prepare(1, 9, str(source), 2, 62.005, '8000', 60), str(source))
    tail = result['manifest']['tiles'][-1]
    assert tail['start'] == 62 and tail['end'] == 62.005 and tail['width'] == 1
    png = cache.tile(1, 9, result['id'], str(source), result['lease'], tail['index'])
    assert Image.open(io.BytesIO(png)).size == (1, 384)


def test_existing_id_reacquire_is_cache_only_and_expiry_never_prepares(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    result = wait_ready(cache, cache.prepare(1, 9, str(source), 1, 2, '8000', 1), str(source))
    cache.release(1, 9, result['id'], result['lease'])
    with patch.object(spectra, '_run') as run, patch.object(threading.Thread, 'start') as start:
        reused = cache.prepare(1, 9, str(source), 1, 2, '8000', 1, existing_id=result['id'])
        assert reused['status'] == 'ready' and reused['lease'] != result['lease']
        for principal, recording, begin, finish, identifier in [(2,9,1,2,result['id']), (1,10,1,2,result['id']), (1,9,1,3,result['id']), (1,9,1,2,'missing')]:
            with pytest.raises(spectra.SpectrogramError) as error:
                cache.prepare(principal, recording, str(source), begin, finish, '8000', 1, existing_id=identifier)
            assert error.value.status == 410
        source.touch()
        with pytest.raises(spectra.SpectrogramError) as error:
            cache.prepare(1, 9, str(source), 1, 2, '8000', 1, existing_id=result['id'])
        assert error.value.status == 410
        run.assert_not_called(); start.assert_not_called()


def test_cancelled_waiter_retains_admission_until_worker_ack(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    # Model a daemon delayed before it can inspect cancellation. Releasing its
    # lease must not admit arbitrarily many additional physical waiters.
    with patch.object(threading.Thread, 'start') as start:
        pending = cache.prepare(1, 9, str(source), 1, 2, '8000', 1)
        cache.release(1, 9, pending['id'], pending['lease'])
        for _ in range(20):
            with pytest.raises(spectra.SpectrogramError) as error:
                cache.prepare(1, 9, str(source), 1, 2, '8000', 1)
            assert error.value.status == 429
        other = cache.prepare(2, 9, str(source), 1, 2, '8000', 1)
        cache.release(2, 9, other['id'], other['lease'])
        with pytest.raises(spectra.SpectrogramError) as error:
            cache.prepare(3, 9, str(source), 1, 2, '8000', 1)
        assert error.value.status == 429
        assert start.call_count == 2


def test_existing_id_nonready_statuses_never_spawn_or_probe(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    with patch.object(threading.Thread, 'start') as start, patch.object(spectra, '_run') as run:
        pending = cache.prepare(1, 9, str(source), 1, 2, '8000', 1)
        for status in ('queued', 'preparing', 'failed', 'cancelled'):
            with cache._state() as state:
                state['jobs'][pending['id']]['status'] = status
            with pytest.raises(spectra.SpectrogramError) as error:
                cache.prepare(1, 9, str(source), 1, 2, '8000', 1, existing_id=pending['id'])
            assert error.value.status == 410
        assert start.call_count == 1
        run.assert_not_called()


def test_existing_id_after_inactive_expiry_never_rebuilds(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache, TTL_SECONDS
    cache = SpectrogramCache(tmp_path / 'cache')
    result = wait_ready(cache, cache.prepare(1, 9, str(source), 1, 2, '8000', 1), str(source))
    cache.release(1, 9, result['id'], result['lease'])
    wait_worker_ack(cache, result['id'])
    with cache._state() as state:
        state['jobs'][result['id']]['used'] = time.time() - TTL_SECONDS - 1
    with patch.object(threading.Thread, 'start') as start, patch.object(spectra, '_run') as run:
        with pytest.raises(spectra.SpectrogramError) as error:
            cache.prepare(1, 9, str(source), 1, 2, '8000', 1, existing_id=result['id'])
        assert error.value.status == 410
        start.assert_not_called(); run.assert_not_called()


def _other_worker_read(root, source, result, output):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(root)
    with patch.object(spectra, '_run', side_effect=AssertionError('cache read rendered')):
        meta = cache.metadata(1, 9, result['id'], source, result['lease'])
        png = cache.tile(1, 9, result['id'], source, result['lease'], 0)
        output.put((meta['status'], len(png)))


def test_ready_cache_is_visible_to_second_process(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    result = wait_ready(cache, cache.prepare(1, 9, str(source), 1, 2, '8000', 1), str(source))
    context = multiprocessing.get_context('spawn')
    output = context.Queue()
    worker = context.Process(target=_other_worker_read, args=(str(cache.root), str(source), result, output))
    worker.start()
    assert output.get(timeout=20)[0] == 'ready'
    worker.join(timeout=20)
    assert worker.exitcode == 0


def _other_worker_prepare(root, source, principal, output):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(root)
    original = spectra.render_tile
    marker = cache.root / 'actual-renderer'
    def observed(*args, **kwargs):
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(descriptor)
        output.put(('rendering', principal))
        try:
            time.sleep(.15)
            return original(*args, **kwargs)
        finally:
            marker.unlink()
    with patch.object(spectra, 'render_tile', side_effect=observed):
        result = wait_ready(cache, cache.prepare(principal, 9, source, 1, 2, '8000', 1), source, principal=principal)
        output.put(('ready', principal, result['id']))


def test_two_processes_share_one_renderer_and_legacy_slot(source, tmp_path, monkeypatch):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    monkeypatch.setattr(spectra, 'cache_root', lambda: cache.root)
    context = multiprocessing.get_context('spawn')
    output = context.Queue()
    workers = [context.Process(target=_other_worker_prepare, args=(str(cache.root), str(source), principal, output)) for principal in (1, 2)]
    for worker in workers:
        worker.start()
    assert output.get(timeout=20)[0] == 'rendering'
    with pytest.raises(spectra.SpectrogramError) as error:
        spectra.render_spectrogram(str(source), 1, 2)
    assert error.value.status == 429
    events = [output.get(timeout=20) for _ in range(3)]
    assert sum(event[0] == 'ready' for event in events) == 2
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0
    with cache._state() as state:
        assert state['renderCount'] == 2
        assert state['peakBytes'] < 512 * 1024 * 1024


def test_global_queue_per_principal_and_cancel_are_bounded(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    with spectra.file_lock(cache.root / 'renderer.lock'):
        first = cache.prepare(1, 9, str(source), 1, 2, '8000', 1)
        with pytest.raises(spectra.SpectrogramError) as error:
            cache.prepare(1, 9, str(source), 1, 3, '8000', 1)
        assert error.value.code == 'busy'
        second = cache.prepare(2, 9, str(source), 1, 2, '8000', 1)
        with pytest.raises(spectra.SpectrogramError) as error:
            cache.prepare(3, 9, str(source), 1, 2, '8000', 1)
        assert error.value.status == 429
        cache.release(1, 9, first['id'], first['lease'])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with cache._state() as state:
                if not state['jobs'].get(first['id'], {}).get('worker_active'):
                    break
            time.sleep(.02)
        replacement = cache.prepare(1, 9, str(source), 1, 3, '8000', 1)
        with pytest.raises(spectra.SpectrogramError) as error:
            cache.metadata(1, 9, first['id'], str(source), first['lease'])
        assert error.value.status == 410
    wait_ready(cache, second, str(source), principal=2)
    wait_ready(cache, replacement, str(source))


def test_reservation_counts_old_pinned_bytes_and_cleans_failed_partial(source, tmp_path, monkeypatch):
    from src.services import spectrogram_cache as module
    cache = module.SpectrogramCache(tmp_path / 'cache')
    result = wait_ready(cache, cache.prepare(1, 9, str(source), 1, 2, '8000', 1), str(source))
    with cache._state() as state:
        original_bytes = state['jobs'][result['id']]['bytes']
        footprint = cache._footprint(state)
    monkeypatch.setattr(module, 'MAX_BYTES', spectra.MAX_TILE_PNG + footprint - 1)
    pending = cache.prepare(1, 9, str(source), 1, 3, '4000', 1)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        failed = cache.metadata(1, 9, pending['id'], str(source), pending['lease'])
        if failed['status'] == 'failed':
            break
        time.sleep(.02)
    assert failed['code'] == 'limit'
    assert not (cache.root / pending['id']).exists()
    assert cache.tile(1, 9, result['id'], str(source), result['lease'], 0).startswith(b'\x89PNG')
    cache.release(1, 9, result['id'], result['lease'])
    ready = wait_ready(cache, cache.prepare(1, 9, str(source), 1, 3, '4000', 1), str(source))
    assert ready['status'] == 'ready'
    assert not (cache.root / result['id']).exists()


def _other_worker_limited(root, source, budget, output):
    from src.services import spectrogram_cache as module
    module.MAX_BYTES = budget
    cache = module.SpectrogramCache(root)
    pending = cache.prepare(2, 9, source, 1, 2, '8000', 1)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = cache.metadata(2, 9, pending['id'], source, pending['lease'])
        if result['status'] == 'failed':
            with cache._state() as state:
                output.put((result['code'], sum(job['bytes'] + job['reserve'] for job in state['jobs'].values())))
            return
        time.sleep(.02)
    raise AssertionError('second worker did not enforce shared quota')


def test_second_process_counts_existing_pinned_artifacts_in_quota(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    result = wait_ready(cache, cache.prepare(1, 9, str(source), 1, 2, '8000', 1), str(source))
    with cache._state() as state:
        actual_bytes = state['jobs'][result['id']]['bytes']
        footprint = cache._footprint(state)
    context = multiprocessing.get_context('spawn')
    output = context.Queue()
    worker = context.Process(target=_other_worker_limited, args=(str(cache.root), str(source), footprint + spectra.MAX_TILE_PNG - 1, output))
    worker.start()
    assert output.get(timeout=20) == ('limit', actual_bytes)
    worker.join(timeout=20)
    assert worker.exitcode == 0
    assert cache.tile(1, 9, result['id'], str(source), result['lease'], 0).startswith(b'\x89PNG')


def test_queue_and_whole_prepare_timeouts_cleanup(source, tmp_path, monkeypatch):
    from src.services import spectrogram_cache as module
    cache = module.SpectrogramCache(tmp_path / 'cache')
    monkeypatch.setattr(module, 'QUEUE_SECONDS', .04)
    with spectra.file_lock(cache.root / 'renderer.lock'):
        pending = cache.prepare(1, 9, str(source), 1, 2, '8000', 1)
        time.sleep(.1)
        result = cache.metadata(1, 9, pending['id'], str(source), pending['lease'])
        assert result['status'] == 'failed' and result['code'] == 'timeout'
    # A terminal public status may precede physical worker acknowledgement.
    # Admission remains reserved until the daemon has actually stopped.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with cache._state() as state:
            if not state['jobs'][pending['id']]['worker_active']:
                break
        time.sleep(.01)
    else:
        pytest.fail('timed-out queue worker did not acknowledge stopping')
    monkeypatch.setattr(module, 'QUEUE_SECONDS', 30)
    monkeypatch.setattr(module, 'PREPARE_SECONDS', .001)
    pending = cache.prepare(1, 9, str(source), 1, 2, '8000', 1)
    time.sleep(.2)
    result = cache.metadata(1, 9, pending['id'], str(source), pending['lease'])
    assert result['status'] == 'failed' and result['code'] == 'timeout'
    assert not (cache.root / pending['id']).exists()


def test_lease_expiry_renewal_and_inactive_ttl_cleanup(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache, TTL_SECONDS
    cache = SpectrogramCache(tmp_path / 'cache')
    result = wait_ready(cache, cache.prepare(1, 9, str(source), 1, 2, '8000', 1), str(source))
    wait_worker_ack(cache, result['id'])
    cache.renew(1, 9, result['id'], str(source), result['lease'])
    with cache._state() as state:
        state['jobs'][result['id']]['leases'][result['lease']]['until'] = time.time() - 1
    with pytest.raises(spectra.SpectrogramError) as error:
        cache.metadata(1, 9, result['id'], str(source), result['lease'])
    assert error.value.status == 410
    with cache._state() as state:
        state['jobs'][result['id']]['used'] = time.time() - TTL_SECONDS - 1
    with cache._state() as state:
        assert result['id'] not in state['jobs']
    assert not (cache.root / result['id']).exists()


def test_cancel_while_rendering_does_not_publish_or_free_reservation_early(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'cache')
    entered, finish = threading.Event(), threading.Event()
    original = spectra.render_tile
    def delayed(*args, **kwargs):
        entered.set()
        assert finish.wait(10)
        return original(*args, **kwargs)
    with patch.object(spectra, 'render_tile', side_effect=delayed):
        pending = cache.prepare(1, 9, str(source), 1, 2, '8000', 1)
        assert entered.wait(10)
        cache.release(1, 9, pending['id'], pending['lease'])
        with cache._state() as state:
            assert state['jobs'][pending['id']]['reserve'] == spectra.MAX_TILE_PNG
        finish.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with cache._state() as state:
                if pending['id'] not in state['jobs'] or not state['jobs'][pending['id']]['reserve']:
                    break
            time.sleep(.02)
    with cache._state() as state:
        assert pending['id'] not in state['jobs'] or (state['jobs'][pending['id']]['bytes'] == 0 and state['jobs'][pending['id']]['status'] == 'cancelled')
    assert not (cache.root / pending['id']).exists()


@pytest.mark.parametrize('span,start,end', [(.249,0,2),(61,0,2),(float('nan'),0,2),(.25,0,4097),(.25,2,2)])
def test_invalid_count_or_density_never_renders(source, tmp_path, span, start, end):
    from src.services.spectrogram_cache import SpectrogramCache
    with patch.object(spectra, '_run') as run, pytest.raises(spectra.SpectrogramError):
        SpectrogramCache(tmp_path / 'cache').prepare(1, 9, str(source), start, end, '8000', span)
    run.assert_not_called()

def test_full_overview_coarse_span_retains_tiles_and_cache_only_reads(source, tmp_path):
    from src.services.spectrogram_cache import SpectrogramCache
    cache = SpectrogramCache(tmp_path / 'overview')
    with patch.object(spectra, '_run', wraps=spectra._run) as run:
        result = wait_ready(cache, cache.prepare(1, 9, str(source), 1, 121, '8000', 120), str(source))
        manifest = result['manifest']
        assert manifest['span'] == 120 and manifest['start'] == 1 and manifest['end'] == 121
        assert len(manifest['tiles']) == 2 and all(t['width'] == 512 for t in manifest['tiles'])
        assert all(t['end']-t['start'] <= 60 for t in manifest['tiles'])
        calls = len(run.call_args_list)
        for tile in manifest['tiles']:
            Image.open(io.BytesIO(cache.tile(1, 9, result['id'], str(source), result['lease'], tile['index']))).verify()
        cache.release(1, 9, result['id'], result['lease'])
        reused = cache.prepare(1, 9, str(source), 1, 121, '8000', 120, existing_id=result['id'])
        assert reused['id'] == result['id'] and reused['status'] == 'ready'
        assert len(run.call_args_list) == calls

@pytest.mark.parametrize('span', [120.0001, float('inf'), -1])
def test_overview_span_cannot_exceed_selected_range_or_be_nonfinite(source, tmp_path, span):
    from src.services.spectrogram_cache import SpectrogramCache
    with patch.object(spectra, '_run') as run, pytest.raises(spectra.SpectrogramError):
        SpectrogramCache(tmp_path / 'cache').prepare(1, 9, str(source), 1, 121, '8000', span)
    run.assert_not_called()
