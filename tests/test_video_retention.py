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

    def test_video_retention_true_keeps_original(self):
        """When VIDEO_RETENTION=True, recording.audio_path is set to original filepath."""
        # The VIDEO_RETENTION=True branch should set recording.audio_path = filepath
        self.assertIn('recording.audio_path = filepath', self.content)

    def test_video_retention_true_extracts_without_cleanup(self):
        """When VIDEO_RETENTION=True, extract_audio_from_video is called with cleanup_original=False."""
        self.assertIn('extract_audio_from_video(filepath, cleanup_original=False)', self.content)

    def test_video_retention_false_extracts_with_cleanup(self):
        """When VIDEO_RETENTION=False, extract_audio_from_video is called with default cleanup."""
        self.assertIn('extract_audio_from_video(filepath)', self.content)

    def test_temp_audio_cleanup_after_transcription(self):
        """Temp audio from video retention is cleaned up after transcription."""
        self.assertIn('is_video and VIDEO_RETENTION and audio_filepath', self.content)
        self.assertIn('Cleaned up temp audio from video retention', self.content)

    def test_audio_filepath_initialized_to_none(self):
        """audio_filepath is initialized to None before the is_video check."""
        # Find the initialization line
        self.assertIn('audio_filepath = None', self.content)

    def test_video_mime_type_set_for_retention(self):
        """When retaining video, mime_type reflects actual video type."""
        self.assertIn("mimetypes.guess_type(filepath)[0] or 'video/mp4'", self.content)

    def test_duration_uses_recording_audio_path(self):
        """Duration lookup uses recording.audio_path (always valid), not filepath."""
        self.assertIn('chunking_service.get_audio_duration(recording.audio_path)', self.content)
        # Should NOT use bare filepath for duration (pre-existing bug was fixed)
        self.assertNotIn('chunking_service.get_audio_duration(filepath)', self.content)


class TestUploadHandlerVideoRetention(unittest.TestCase):
    """Test recordings.py upload handler video retention code paths."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(PROJECT_ROOT, 'src/api/recordings.py'), 'r') as f:
            cls.content = f.read()

    def test_upload_handler_skips_conversion_for_video_retention(self):
        """Upload handler skips convert_if_needed for videos when retention is on."""
        self.assertIn('VIDEO_RETENTION and has_video', self.content)
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
        # Should have the guard: if VIDEO_RETENTION and has_video: ... skip conversion
        self.assertIn('VIDEO_RETENTION and has_video', self.content)
        self.assertIn('skipping conversion', self.content)

    def test_no_double_extraction(self):
        """File monitor does NOT call convert_if_needed for videos when retention is on."""
        # The convert_if_needed call should be in the else branch
        lines = self.content.split('\n')
        in_retention_skip_block = False
        found_convert_in_else = False

        for i, line in enumerate(lines):
            if 'VIDEO_RETENTION and has_video' in line and 'if' in line:
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
        # Find the non-download send_file call
        self.assertIn('send_file(recording.audio_path, conditional=True)', content)

    def test_recordings_download_has_conditional(self):
        """Download send_file in recordings.py has conditional=True."""
        content = self._read_file('src/api/recordings.py')
        self.assertIn('as_attachment=True, download_name=filename, conditional=True', content)

    def test_shares_has_conditional(self):
        """send_file in shares.py has conditional=True."""
        content = self._read_file('src/api/shares.py')
        self.assertIn('send_file(recording.audio_path, conditional=True)', content)


class TestFrontendTemplates(unittest.TestCase):
    """Test that frontend templates correctly switch between video and audio."""

    TEMPLATE_FILES = [
        'templates/components/detail/desktop-right-panel.html',
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
            # Desktop right panel and audio player should have 0 bare audio tags
            if 'desktop-right-panel' in tmpl or 'audio-player' in tmpl:
                self.assertEqual(audio_count, 0, f"Unexpected bare <audio> in {tmpl}")

    def test_video_element_gets_visible_styling(self):
        """When mime_type is video/, the element should be visible (not hidden)."""
        for tmpl in self.TEMPLATE_FILES:
            content = self._read_template(tmpl)
            # Should have conditional class that shows video and hides audio
            self.assertIn("'w-full rounded-lg", content, f"Missing video styling in {tmpl}")
            self.assertIn("'hidden'", content, f"Missing hidden fallback for audio in {tmpl}")

    def test_template_div_balance(self):
        """Verify player-specific templates have balanced div tags."""
        # Only check templates we fully control (not share.html which has pre-existing imbalance)
        balanced_templates = [
            'templates/components/detail/desktop-right-panel.html',
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
        """processing.py has both VIDEO_RETENTION=True and False branches for video."""
        content = self._read_file('src/tasks/processing.py')
        # Should have if VIDEO_RETENTION: ... else: ... inside if is_video:
        self.assertIn('if VIDEO_RETENTION:', content)
        # After the retention block, should have else for the default behavior
        lines = content.split('\n')
        found_retention_if = False
        found_else_after = False
        for line in lines:
            if 'if VIDEO_RETENTION:' in line:
                found_retention_if = True
            elif found_retention_if and line.strip().startswith('else:'):
                found_else_after = True
                break
        self.assertTrue(found_else_after, "Missing else branch after VIDEO_RETENTION check in processing.py")

    def test_file_monitor_has_both_branches(self):
        """file_monitor.py has both video retention skip and normal conversion paths."""
        content = self._read_file('src/file_monitor.py')
        self.assertIn('VIDEO_RETENTION and has_video', content)
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
        """All entry points (upload, file monitor, processing) handle VIDEO_RETENTION."""
        for rel_path, marker in [
            ('src/api/recordings.py', 'VIDEO_RETENTION and has_video'),
            ('src/file_monitor.py', 'VIDEO_RETENTION and has_video'),
            ('src/tasks/processing.py', 'if VIDEO_RETENTION:'),
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
