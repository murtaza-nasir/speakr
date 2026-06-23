"""
Test suite for the VIDEO_RETENTION feature.

Tests code paths, configuration, and template correctness for video retention.
Does NOT require a running server or real video files - uses static analysis
and mocking where possible.

Run with: python tests/test_video_retention.py
"""

import os
import re
import sys
import json
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

# Find project root
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, PROJECT_ROOT)


class TestVideoRetentionConfig(unittest.TestCase):
    """Test that VIDEO_RETENTION env var is read correctly everywhere."""

    ALL_FILES = [
        'src/app.py',
        'src/tasks/processing.py',
        'src/api/system.py',
        'src/api/recordings.py',
        'src/file_monitor.py',
    ]

    def _read_file(self, rel_path):
        with open(os.path.join(PROJECT_ROOT, rel_path), 'r') as f:
            return f.read()

    def test_env_var_read_in_all_entry_points(self):
        """VIDEO_RETENTION env var is read in all files that need it."""
        for rel_path in self.ALL_FILES:
            content = self._read_file(rel_path)
            self.assertIn("VIDEO_RETENTION", content, f"VIDEO_RETENTION missing from {rel_path}")

    def test_exposed_in_api_config(self):
        """VIDEO_RETENTION is exposed in the /api/config response."""
        content = self._read_file('src/api/system.py')
        self.assertIn("'video_retention': VIDEO_RETENTION", content)

    def test_default_is_false(self):
        """All VIDEO_RETENTION reads default to 'false'."""
        for rel_path in self.ALL_FILES:
            content = self._read_file(rel_path)
            match = re.search(r"VIDEO_RETENTION\s*=\s*os\.environ\.get\('VIDEO_RETENTION',\s*'(\w+)'\)", content)
            if match:
                self.assertEqual(match.group(1), 'false', f"Default should be 'false' in {rel_path}")


class TestProcessingPipelineVideoRetention(unittest.TestCase):
    """Test processing.py video retention code paths via static analysis."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(PROJECT_ROOT, 'src/tasks/processing.py'), 'r') as f:
            cls.content = f.read()

    def test_video_retention_true_preserves_stored_media_reference(self):
        """When VIDEO_RETENTION=True, the persistent media reference is left untouched."""
        self.assertIn('Video retained (media path unchanged)', self.content)
        self.assertNotIn('recording.audio_path = filepath', self.content)

    def test_video_retention_true_extracts_without_cleanup(self):
        """When VIDEO_RETENTION=True, extract_audio_from_video is called with cleanup_original=False."""
        self.assertIn('extract_audio_from_video(filepath, cleanup_original=False)', self.content)

    def test_video_retention_false_extracts_with_cleanup(self):
        """When VIDEO_RETENTION=False, extract_audio_from_video is called with default cleanup."""
        self.assertIn('extract_audio_from_video(filepath)', self.content)

    def test_effective_video_retention_combines_env_and_per_recording_flag(self):
        """The processing task derives `effective_video_retention` from
        BOTH the env var AND the per-recording keep_audio_only flag.
        This is what lets a single upload opt out of retention even when
        VIDEO_RETENTION is on globally."""
        self.assertIn(
            "effective_video_retention = VIDEO_RETENTION and not getattr(recording, 'keep_audio_only', False)",
            self.content,
        )

    def test_temp_audio_cleanup_after_transcription(self):
        """Temp audio from video retention is cleaned up after transcription.

        After the per-upload keep_audio_only override was added, the
        cleanup branch checks the effective (per-recording) retention
        flag rather than the global env var directly.
        """
        self.assertIn('is_video and effective_video_retention and audio_filepath', self.content)
        self.assertIn('Cleaned up temp audio from video retention', self.content)

    def test_audio_filepath_initialized_to_none(self):
        """audio_filepath is initialized to None before the is_video check."""
        # Find the initialization line
        self.assertIn('audio_filepath = None', self.content)

    def test_video_mime_type_set_for_retention(self):
        """When retaining video, mime_type is derived from the file's actual
        container via the shared probe-driven resolver (not a guess, which
        mislabels ambiguous containers like .webm as audio/webm and hid the
        video player in the UI)."""
        self.assertIn('recording.mime_type = resolve_media_mime(filepath, has_video=True)', self.content)
        self.assertNotIn("mimetypes.guess_type(filepath)[0] or 'video/mp4'", self.content)

    def test_duration_uses_recording_audio_path(self):
        """Duration logic prefers cached DB values and local probe candidates."""
        self.assertIn('recording.audio_duration_seconds = float(cached_audio_duration)', self.content)
        self.assertIn('duration_probe_candidates = []', self.content)
        self.assertIn('chunking_service.get_audio_duration(candidate_path)', self.content)


class TestUploadHandlerVideoRetention(unittest.TestCase):
    """Test recordings.py upload handler video retention code paths."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(PROJECT_ROOT, 'src/api/recordings.py'), 'r') as f:
            cls.content = f.read()

    def test_upload_handler_parses_keep_audio_only_form_field(self):
        """The new per-upload `keep_audio_only` form field is parsed at
        the top of the upload handler. Without this parse, the override
        is silently ignored."""
        self.assertIn(
            "request.form.get('keep_audio_only', 'false').lower() == 'true'",
            self.content,
        )
        self.assertIn('keep_audio_only_flag', self.content)
        self.assertIn('effective_audio_only', self.content)

    def test_upload_handler_uses_separate_size_limit_for_audio_only_video(self):
        """Per option-B semantics, the size gate uses the larger video
        cap for ANY video file (by extension) regardless of the
        keep_audio_only flag; the post-extraction guard still rejects
        when the *extracted* audio exceeds the regular limit AND
        chunking is off (chunking-on lets large audio through to the
        chunking pipeline)."""
        # The audio-only-video limit must be read from the SystemSetting,
        # not hardcoded.
        self.assertIn(
            "SystemSetting.get_setting('max_audio_only_video_size_mb'",
            self.content,
        )
        # Effective limit splits purely on file type, not on audio-only mode.
        self.assertIn('if is_likely_video_by_ext:', self.content)
        # And the post-extraction size guard still caps the stored audio
        # against the regular limit, but only when chunking won't handle
        # it (chunking pipeline accepts large audio).
        self.assertIn('regular_limit_mb * 1024 * 1024', self.content)
        self.assertIn('extracted_audio_mb', self.content)
        self.assertIn('chunking_will_handle_large_audio', self.content)

    def test_upload_handler_persists_keep_audio_only_on_recording(self):
        """The recording row stores the effective audio-only flag so the
        processing task and reprocess flows honour it."""
        self.assertIn('keep_audio_only=effective_audio_only', self.content)

    def test_upload_handler_skips_conversion_for_video_retention(self):
        """Upload handler skips convert_if_needed for videos when retention
        is on AND the per-upload keep_audio_only override is off.

        The exact decision string changed when the per-upload override
        landed; this test pins the new shape.
        """
        # The decision must factor in keep_audio_only_flag so a single
        # upload can opt out of retention even when VIDEO_RETENTION=True.
        self.assertIn('VIDEO_RETENTION and not keep_audio_only_flag', self.content)
        # And the "skip conversion" log line is still the marker that
        # the keep-video branch was taken.
        self.assertIn('skipping conversion', self.content)

    def test_upload_handler_has_video_from_codec_info(self):
        """Upload handler reads has_video from codec_info probe."""
        self.assertIn("has_video = codec_info.get('has_video', False)", self.content)

    def test_convert_if_needed_still_in_else_branch(self):
        """convert_if_needed still runs for non-video files or when retention is off."""
        self.assertIn('convert_if_needed(', self.content)

    def test_processing_pipeline_still_converts_audio(self):
        """Processing pipeline runs convert_if_needed on extracted audio (the safety net)."""
        proc_content = open(os.path.join(PROJECT_ROOT, 'src/tasks/processing.py')).read()
        # After the video extraction block, convert_if_needed runs on actual_filepath
        self.assertIn('conversion_result = convert_if_needed(\n'
                      '                    filepath=actual_filepath,', proc_content)


class TestFileMonitorVideoRetention(unittest.TestCase):
    """Test file_monitor.py video retention code paths."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(PROJECT_ROOT, 'src/file_monitor.py'), 'r') as f:
            cls.content = f.read()

    def test_video_retention_skips_conversion(self):
        """When VIDEO_RETENTION=True and has_video=True, convert_if_needed is skipped."""
        # Should have the guard: if (VIDEO_PASSTHROUGH_ASR or VIDEO_RETENTION) and has_video: ... skip conversion
        self.assertIn('VIDEO_PASSTHROUGH_ASR or VIDEO_RETENTION) and has_video', self.content)
        self.assertIn('skipping conversion', self.content)

    def test_no_double_extraction(self):
        """File monitor does NOT call convert_if_needed for videos when retention is on."""
        # The convert_if_needed call should be in the else branch
        lines = self.content.split('\n')
        in_retention_skip_block = False
        found_convert_in_else = False

        for i, line in enumerate(lines):
            if 'VIDEO_PASSTHROUGH_ASR or VIDEO_RETENTION) and has_video' in line and 'if' in line:
                in_retention_skip_block = True
            elif in_retention_skip_block and 'else:' in line:
                in_retention_skip_block = False
                found_convert_in_else = True
            elif in_retention_skip_block and 'convert_if_needed' in line:
                self.fail(f"convert_if_needed called inside VIDEO_RETENTION skip block at line {i+1}")

        self.assertTrue(found_convert_in_else, "Should have else branch after video retention skip")

    def test_no_video_retention_param_in_convert_call(self):
        """convert_if_needed should NOT receive a video_retention parameter."""
        # Ensure the old video_retention parameter isn't being passed
        self.assertNotIn('video_retention=VIDEO_RETENTION', self.content)


class TestAudioConversionNotModified(unittest.TestCase):
    """Verify audio_conversion.py was fully reverted (no video_retention parameter)."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(PROJECT_ROOT, 'src/utils/audio_conversion.py'), 'r') as f:
            cls.content = f.read()

    def test_no_video_retention_parameter(self):
        """convert_if_needed should not have a video_retention parameter."""
        self.assertNotIn('video_retention', self.content)

    def test_no_should_delete_original(self):
        """No should_delete_original variable should exist."""
        self.assertNotIn('should_delete_original', self.content)


class TestSendFileConditional(unittest.TestCase):
    """Test that send_file calls use conditional=True for range request support."""

    def _read_file(self, rel_path):
        with open(os.path.join(PROJECT_ROOT, rel_path), 'r') as f:
            return f.read()

    def test_recordings_streaming_has_conditional(self):
        """Streaming send_file in recordings.py has conditional=True."""
        content = self._read_file('src/api/recordings.py')
        self.assertIn('return send_file(delivery.local_path, mimetype=recording.mime_type, conditional=True)', content)

    def test_recordings_download_has_conditional(self):
        """Download send_file in recordings.py has conditional=True."""
        content = self._read_file('src/api/recordings.py')
        self.assertIn('as_attachment=True,', content)
        self.assertIn('download_name=download_name,', content)
        self.assertIn('mimetype=recording.mime_type,', content)
        self.assertIn('conditional=True,', content)

    def test_shares_has_conditional(self):
        """send_file in shares.py has conditional=True."""
        content = self._read_file('src/api/shares.py')
        self.assertIn('send_file(delivery.local_path, mimetype=(recording.mime_type or delivery.mimetype), conditional=True)', content)


class TestFrontendTemplates(unittest.TestCase):
    """Test that frontend templates correctly switch between video and audio."""

    TEMPLATE_FILES = [
        # The recording-detail desktop audio player now lives in
        # desktop-bottom-player.html (persistent bottom bar), pulled
        # out of desktop-right-panel.html during the UX redesign so it
        # spans the full content width below the columns.
        'templates/components/detail/desktop-bottom-player.html',
        'templates/components/detail/audio-player.html',
        'templates/modals/speaker-modal.html',
        'templates/share.html',
    ]

    def _read_template(self, rel_path):
        with open(os.path.join(PROJECT_ROOT, rel_path), 'r') as f:
            return f.read()

    def test_all_templates_use_dynamic_component(self):
        """All player templates use <component :is> for video/audio switching."""
        for tmpl in self.TEMPLATE_FILES:
            content = self._read_template(tmpl)
            self.assertIn("<component :is=", content, f"Missing dynamic component in {tmpl}")
            self.assertIn("startsWith('video/')", content, f"Missing video/ check in {tmpl}")
            self.assertIn("</component>", content, f"Missing </component> in {tmpl}")

    def test_no_bare_audio_elements_in_main_players(self):
        """Main player templates should not have bare <audio elements (replaced by component)."""
        for tmpl in self.TEMPLATE_FILES:
            content = self._read_template(tmpl)
            # Count <audio and <component :is occurrences
            audio_count = content.count('<audio ')
            component_count = content.count('<component :is=')

            # Each template should have component :is but no bare <audio for the main player
            self.assertGreater(component_count, 0, f"No <component :is> in {tmpl}")
            # Desktop bottom player and the mobile audio-player both
            # should have 0 bare audio tags (the dynamic component
            # carries video/audio switching).
            if 'desktop-bottom-player' in tmpl or 'audio-player' in tmpl:
                self.assertEqual(audio_count, 0, f"Unexpected bare <audio> in {tmpl}")

    def test_video_element_gets_visible_styling(self):
        """When mime_type is video/, the element should be visible (not hidden).

        desktop-bottom-player.html is a single-row controls bar with no
        room for inline video; video playback there is fullscreen-only,
        so the inline-styling assertion doesn't apply to it.
        """
        for tmpl in self.TEMPLATE_FILES:
            content = self._read_template(tmpl)
            # Should always have a hidden fallback for the audio case
            self.assertIn("'hidden'", content, f"Missing hidden fallback for audio in {tmpl}")
            # Inline video styling only applies to templates that render
            # video inline (not the bottom-bar player).
            if 'desktop-bottom-player' not in tmpl:
                self.assertIn("'w-full rounded-lg", content, f"Missing video styling in {tmpl}")

    def test_template_div_balance(self):
        """Verify player-specific templates have balanced div tags."""
        # Only check templates we fully control (not share.html which has pre-existing imbalance)
        balanced_templates = [
            'templates/components/detail/desktop-right-panel.html',
            'templates/components/detail/desktop-bottom-player.html',
            'templates/components/detail/audio-player.html',
            'templates/modals/speaker-modal.html',
        ]
        for tmpl in balanced_templates:
            content = self._read_template(tmpl)
            opens = content.count('<div')
            closes = content.count('</div>')
            self.assertEqual(opens, closes, f"Unbalanced divs in {tmpl}: {opens} opens, {closes} closes")


class TestLocalization(unittest.TestCase):
    """Test that video retention localization keys exist in all locale files."""

    LOCALE_DIR = os.path.join(PROJECT_ROOT, 'static', 'locales')

    def test_video_retained_key_in_all_locales(self):
        """upload.videoRetained key exists in all locale files."""
        locale_files = [f for f in os.listdir(self.LOCALE_DIR) if f.endswith('.json')]
        self.assertGreater(len(locale_files), 0, "No locale files found")

        for locale_file in locale_files:
            filepath = os.path.join(self.LOCALE_DIR, locale_file)
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.assertIn('upload', data, f"No 'upload' section in {locale_file}")
            self.assertIn('videoRetained', data['upload'],
                         f"Missing 'videoRetained' key in upload section of {locale_file}")
            self.assertIsInstance(data['upload']['videoRetained'], str,
                               f"'videoRetained' should be a string in {locale_file}")
            self.assertGreater(len(data['upload']['videoRetained']), 0,
                             f"'videoRetained' is empty in {locale_file}")

    def test_locale_files_are_valid_json(self):
        """All locale files are valid JSON."""
        locale_files = [f for f in os.listdir(self.LOCALE_DIR) if f.endswith('.json')]
        for locale_file in locale_files:
            filepath = os.path.join(self.LOCALE_DIR, locale_file)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                self.fail(f"Invalid JSON in {locale_file}: {e}")


class TestVideoRetentionMatrix(unittest.TestCase):
    """
    Test the complete 2x2 matrix of (VIDEO_RETENTION x is_video) scenarios
    by analyzing the code flow statically.
    """

    def _read_file(self, rel_path):
        with open(os.path.join(PROJECT_ROOT, rel_path), 'r') as f:
            return f.read()

    def test_processing_has_both_branches(self):
        """processing.py has both retention=on and retention=off branches
        for video. After the per-upload keep_audio_only override landed,
        the decision is `effective_video_retention` (derived from the env
        var AND the recording's keep_audio_only field) rather than
        VIDEO_RETENTION directly.
        """
        content = self._read_file('src/tasks/processing.py')
        # The effective flag must be derived from both the env var and
        # the per-recording override.
        self.assertIn('effective_video_retention = VIDEO_RETENTION and not', content)
        # And it must be the gate inside the is_video block.
        self.assertIn('elif effective_video_retention:', content)
        # The else branch (extract audio + delete original) must still
        # follow somewhere.
        lines = content.split('\n')
        found_retention_branch = False
        found_else_after = False
        for line in lines:
            if 'elif effective_video_retention:' in line:
                found_retention_branch = True
            elif found_retention_branch and line.strip().startswith('else:'):
                found_else_after = True
                break
        self.assertTrue(found_else_after, "Missing else branch after effective_video_retention check in processing.py")

    def test_file_monitor_has_both_branches(self):
        """file_monitor.py has both video retention skip and normal conversion paths."""
        content = self._read_file('src/file_monitor.py')
        self.assertIn('VIDEO_PASSTHROUGH_ASR or VIDEO_RETENTION) and has_video', content)
        # convert_if_needed should still exist in the else path
        self.assertIn('convert_if_needed(', content)

    def test_incognito_not_affected(self):
        """Incognito processing path should NOT reference VIDEO_RETENTION."""
        content = self._read_file('src/tasks/processing.py')
        # Find the incognito section (marked with [Incognito])
        incognito_section = content[content.find('[Incognito]'):]
        # VIDEO_RETENTION should not appear in incognito section
        # (incognito always strips video per the plan)
        self.assertNotIn('VIDEO_RETENTION', incognito_section,
                        "VIDEO_RETENTION should not be referenced in incognito processing")

    def test_all_three_entry_points_skip_for_video_retention(self):
        """All entry points (upload, file monitor, processing) honour
        video retention. The web upload and the processing task also
        honour the per-recording keep_audio_only override; file monitor
        does not (it has no per-file override surface)."""
        for rel_path, marker in [
            # Upload handler: per-upload override factored in.
            ('src/api/recordings.py', 'VIDEO_RETENTION and not keep_audio_only_flag'),
            # File monitor: still global-only, no per-file override.
            ('src/file_monitor.py', '(VIDEO_PASSTHROUGH_ASR or VIDEO_RETENTION) and has_video'),
            # Processing task: effective flag (env var AND per-recording).
            ('src/tasks/processing.py', 'elif effective_video_retention:'),
        ]:
            content = self._read_file(rel_path)
            self.assertIn(marker, content, f"Missing video retention guard in {rel_path}")

    def test_convert_if_needed_always_runs_on_transcription_audio(self):
        """Processing pipeline always runs convert_if_needed on audio before transcription."""
        content = self._read_file('src/tasks/processing.py')
        # The convert_if_needed call on actual_filepath happens AFTER the video
        # extraction block, regardless of VIDEO_RETENTION setting
        video_block_pos = content.find('if is_video:')
        convert_pos = content.find('convert_if_needed(\n                    filepath=actual_filepath,')
        self.assertGreater(convert_pos, video_block_pos,
                          "convert_if_needed must run after video extraction block")


class TestVideoMimeHelper(unittest.TestCase):
    """Behavioural tests for the shared video_mime_for_path helper.

    Pins the actual bug: a .webm with video must resolve to video/webm even
    though src/app.py registers .webm as audio/webm for in-app audio
    recordings (which makes mimetypes.guess_type return audio/webm).
    """

    @classmethod
    def setUpClass(cls):
        # Import app first so its mimetypes.add_type('audio/webm', '.webm')
        # registration is in effect — the exact condition that caused the bug.
        import src.app  # noqa: F401
        from src.utils.mime import video_mime_for_path
        cls.video_mime_for_path = staticmethod(video_mime_for_path)

    def test_webm_with_video_resolves_to_video_webm(self):
        # This is the regression: guess_type would say audio/webm here.
        import mimetypes
        self.assertEqual(mimetypes.guess_type('x.webm')[0], 'audio/webm')
        self.assertEqual(self.video_mime_for_path('/data/uploads/x.webm'), 'video/webm')

    def test_mp4_resolves_to_video_mp4(self):
        self.assertEqual(self.video_mime_for_path('/data/uploads/clip.mp4'), 'video/mp4')

    def test_known_video_containers(self):
        cases = {
            '/a/b.mkv': 'video/x-matroska',
            '/a/b.mov': 'video/quicktime',
            '/a/b.m4v': 'video/x-m4v',
            '/a/b.ts': 'video/mp2t',
        }
        for path, expected in cases.items():
            self.assertEqual(self.video_mime_for_path(path), expected, path)

    def test_unknown_extension_defaults_to_video_mp4(self):
        self.assertEqual(self.video_mime_for_path('/a/b.weirdext'), 'video/mp4')


class TestProbeDrivenMime(unittest.TestCase):
    """The probe-driven mapper: ffprobe format_name + has_video -> MIME.

    This is the real architectural fix — derive the MIME from the file's
    actual container/streams instead of guessing from the extension.
    """

    @classmethod
    def setUpClass(cls):
        from src.utils.mime import _mime_from_format
        cls.f = staticmethod(_mime_from_format)

    def test_webm_container_audio_vs_video(self):
        # Same container, decided by has_video — the exact bug case.
        self.assertEqual(self.f('matroska,webm', True, '/x.webm'), 'video/webm')
        self.assertEqual(self.f('matroska,webm', False, '/x.webm'), 'audio/webm')

    def test_matroska_vs_webm_by_extension(self):
        self.assertEqual(self.f('matroska,webm', True, '/x.mkv'), 'video/x-matroska')

    def test_mp4_family(self):
        self.assertEqual(self.f('mov,mp4,m4a,3gp,3g2,mj2', True, '/x.mp4'), 'video/mp4')
        self.assertEqual(self.f('mov,mp4,m4a,3gp,3g2,mj2', False, '/x.m4a'), 'audio/mp4')

    def test_audio_only_containers(self):
        self.assertEqual(self.f('mp3', False, '/x.mp3'), 'audio/mpeg')
        self.assertEqual(self.f('flac', False, '/x.flac'), 'audio/flac')
        self.assertEqual(self.f('wav', False, '/x.wav'), 'audio/wav')
        self.assertEqual(self.f('ogg', False, '/x.ogg'), 'audio/ogg')

    def test_video_only_containers(self):
        self.assertEqual(self.f('avi', True, '/x.avi'), 'video/x-msvideo')
        self.assertEqual(self.f('flv', True, '/x.flv'), 'video/x-flv')
        self.assertEqual(self.f('mpegts', True, '/x.ts'), 'video/mp2t')

    def test_asf_audio_vs_video(self):
        self.assertEqual(self.f('asf', True, '/x.wmv'), 'video/x-ms-wmv')
        self.assertEqual(self.f('asf', False, '/x.wma'), 'audio/x-ms-wma')

    def test_unknown_container_returns_none(self):
        self.assertIsNone(self.f('some_weird_format', False, '/x.bin'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
