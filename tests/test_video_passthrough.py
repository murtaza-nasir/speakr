"""
Test suite for the VIDEO_PASSTHROUGH_ASR feature.

Tests configuration, code path correctness, and interaction with VIDEO_RETENTION
across all entry points (processing pipeline, upload handler, file monitor, incognito).
Uses static analysis — no running server or real video files required.

Run with: python tests/test_video_passthrough.py
"""

import os
import re
import sys
import unittest
from pathlib import Path

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, PROJECT_ROOT)


def read_file(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), 'r') as f:
        return f.read()


# Cache file contents once — they don't change during the run
PROCESSING = read_file('src/tasks/processing.py')
RECORDINGS = read_file('src/api/recordings.py')
FILE_MONITOR = read_file('src/file_monitor.py')
APP_CONFIG = read_file('src/config/app_config.py')
ENV_EXAMPLE = read_file('config/env.transcription.example')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_function_body(source, func_name):
    """Extract the body of a top-level function from source code."""
    pattern = rf'^def {func_name}\('
    lines = source.split('\n')
    start = None
    for i, line in enumerate(lines):
        if re.match(pattern, line):
            start = i
            break
    if start is None:
        return ''
    # Collect until next top-level def or class or EOF
    body_lines = [lines[start]]
    for line in lines[start + 1:]:
        if line and not line[0].isspace() and (line.startswith('def ') or line.startswith('class ')):
            break
        body_lines.append(line)
    return '\n'.join(body_lines)


def split_at_incognito(source):
    """Split processing.py into main and incognito sections."""
    marker = 'def transcribe_incognito('
    idx = source.find(marker)
    if idx == -1:
        return source, ''
    return source[:idx], source[idx:]


PROCESSING_MAIN, PROCESSING_INCOGNITO = split_at_incognito(PROCESSING)


# ===========================================================================
# 1. Configuration
# ===========================================================================

class TestPassthroughConfig(unittest.TestCase):
    """VIDEO_PASSTHROUGH_ASR env var is defined and defaults to false."""

    FILES_THAT_NEED_IT = [
        ('src/config/app_config.py', APP_CONFIG),
        ('src/tasks/processing.py', PROCESSING),
        ('src/api/recordings.py', RECORDINGS),
        ('src/file_monitor.py', FILE_MONITOR),
    ]

    def test_defined_in_all_files(self):
        for rel_path, content in self.FILES_THAT_NEED_IT:
            with self.subTest(file=rel_path):
                self.assertIn('VIDEO_PASSTHROUGH_ASR', content,
                              f"VIDEO_PASSTHROUGH_ASR missing from {rel_path}")

    def test_default_is_false_everywhere(self):
        for rel_path, content in self.FILES_THAT_NEED_IT:
            match = re.search(
                r"VIDEO_PASSTHROUGH_ASR\s*=\s*os\.environ\.get\('VIDEO_PASSTHROUGH_ASR',\s*'(\w+)'\)",
                content
            )
            if match:
                with self.subTest(file=rel_path):
                    self.assertEqual(match.group(1), 'false',
                                     f"Default should be 'false' in {rel_path}")

    def test_canonical_definition_in_app_config(self):
        self.assertIn(
            "VIDEO_PASSTHROUGH_ASR = os.environ.get('VIDEO_PASSTHROUGH_ASR', 'false').lower() == 'true'",
            APP_CONFIG
        )

    def test_documented_in_env_example(self):
        self.assertIn('VIDEO_PASSTHROUGH_ASR', ENV_EXAMPLE)

    def test_processing_imports_from_config(self):
        self.assertIn('VIDEO_PASSTHROUGH_ASR', PROCESSING)
        # Should import from app_config, not read os.environ directly
        self.assertIn('import', PROCESSING)
        # Verify it's in an import line from app_config
        import_lines = [l for l in PROCESSING.split('\n')
                        if 'from src.config.app_config import' in l]
        found = any('VIDEO_PASSTHROUGH_ASR' in l for l in import_lines)
        self.assertTrue(found, "processing.py should import VIDEO_PASSTHROUGH_ASR from app_config")


# ===========================================================================
# 2. Processing pipeline — main transcription path
# ===========================================================================

class TestProcessingMainPath(unittest.TestCase):
    """Test transcribe_with_connector() video passthrough code paths."""

    def test_passthrough_branch_exists_before_retention(self):
        """VIDEO_PASSTHROUGH_ASR is checked before VIDEO_RETENTION in the is_video block."""
        # Inside the `if is_video:` block, passthrough should be the first check
        video_block_start = PROCESSING_MAIN.find('if is_video:')
        self.assertNotEqual(video_block_start, -1)

        after_video = PROCESSING_MAIN[video_block_start:]
        passthrough_pos = after_video.find('if VIDEO_PASSTHROUGH_ASR:')
        retention_pos = after_video.find('elif VIDEO_RETENTION:')

        self.assertNotEqual(passthrough_pos, -1, "Missing VIDEO_PASSTHROUGH_ASR check in is_video block")
        self.assertNotEqual(retention_pos, -1, "Missing elif VIDEO_RETENTION check")
        self.assertLess(passthrough_pos, retention_pos,
                        "VIDEO_PASSTHROUGH_ASR should be checked before VIDEO_RETENTION")

    def test_passthrough_does_not_call_extract_audio(self):
        """The passthrough branch must not call extract_audio_from_video."""
        video_block = PROCESSING_MAIN[PROCESSING_MAIN.find('if is_video:'):]
        # Find the passthrough branch (from `if VIDEO_PASSTHROUGH_ASR:` to `elif VIDEO_RETENTION:`)
        pt_start = video_block.find('if VIDEO_PASSTHROUGH_ASR:')
        pt_end = video_block.find('elif VIDEO_RETENTION:')
        passthrough_block = video_block[pt_start:pt_end]
        self.assertNotIn('extract_audio_from_video', passthrough_block,
                          "Passthrough branch should NOT extract audio")

    def test_passthrough_keeps_original_filepath(self):
        """Passthrough sets actual_filepath = filepath (the original video)."""
        video_block = PROCESSING_MAIN[PROCESSING_MAIN.find('if is_video:'):]
        pt_start = video_block.find('if VIDEO_PASSTHROUGH_ASR:')
        pt_end = video_block.find('elif VIDEO_RETENTION:')
        passthrough_block = video_block[pt_start:pt_end]
        self.assertIn('actual_filepath = filepath', passthrough_block)

    def test_passthrough_with_retention_sets_recording_path(self):
        """When both passthrough and retention are on, recording.audio_path is set."""
        video_block = PROCESSING_MAIN[PROCESSING_MAIN.find('if is_video:'):]
        pt_start = video_block.find('if VIDEO_PASSTHROUGH_ASR:')
        pt_end = video_block.find('elif VIDEO_RETENTION:')
        passthrough_block = video_block[pt_start:pt_end]
        self.assertIn('if VIDEO_RETENTION:', passthrough_block,
                       "Passthrough branch should conditionally handle retention")
        self.assertIn('recording.audio_path = filepath', passthrough_block)
        self.assertIn("mimetypes.guess_type(filepath)", passthrough_block)

    def test_video_passthrough_active_flag_set(self):
        """video_passthrough_active flag is computed from is_video and VIDEO_PASSTHROUGH_ASR."""
        self.assertIn('video_passthrough_active = is_video and VIDEO_PASSTHROUGH_ASR',
                       PROCESSING_MAIN)

    def test_conversion_skipped_when_passthrough(self):
        """convert_if_needed is inside an else block gated by video_passthrough_active."""
        self.assertIn('if video_passthrough_active:', PROCESSING_MAIN)
        # The conversion call should be in the else branch
        flag_pos = PROCESSING_MAIN.find('video_passthrough_active = is_video and VIDEO_PASSTHROUGH_ASR')
        after_flag = PROCESSING_MAIN[flag_pos:]
        passthrough_if = after_flag.find('if video_passthrough_active:')
        else_pos = after_flag.find('else:', passthrough_if)
        convert_pos = after_flag.find('convert_if_needed(', else_pos)
        self.assertGreater(convert_pos, else_pos,
                           "convert_if_needed should be in else branch after passthrough check")

    def test_chunking_skipped_when_passthrough(self):
        """Chunking evaluates to False when video_passthrough_active."""
        # Find the chunking decision area after the flag
        flag_pos = PROCESSING_MAIN.find('video_passthrough_active = is_video and VIDEO_PASSTHROUGH_ASR')
        after_flag = PROCESSING_MAIN[flag_pos:]
        self.assertIn('if video_passthrough_active:\n                should_chunk = False', after_flag)

    def test_conversion_still_runs_for_non_passthrough(self):
        """convert_if_needed still runs when passthrough is off or file is audio."""
        # The else branch of the passthrough check should contain convert_if_needed
        self.assertIn('conversion_result = convert_if_needed(', PROCESSING_MAIN)

    def test_chunking_still_evaluated_for_non_passthrough(self):
        """Chunking is still evaluated normally when passthrough is not active."""
        self.assertIn('chunking_service.needs_chunking(actual_filepath, False, connector_specs)',
                       PROCESSING_MAIN)


# ===========================================================================
# 3. Processing pipeline — VIDEO_RETENTION paths still intact
# ===========================================================================

class TestRetentionNotBroken(unittest.TestCase):
    """Existing VIDEO_RETENTION behavior must be preserved."""

    def test_retention_branch_still_extracts_audio(self):
        """elif VIDEO_RETENTION branch still calls extract_audio_from_video."""
        video_block = PROCESSING_MAIN[PROCESSING_MAIN.find('if is_video:'):]
        ret_start = video_block.find('elif VIDEO_RETENTION:')
        # Find next else: at the same indent level
        after_ret = video_block[ret_start:]
        else_pos = after_ret.find('\n                else:')
        retention_block = after_ret[:else_pos] if else_pos != -1 else after_ret[:500]
        self.assertIn('extract_audio_from_video(filepath, cleanup_original=False)',
                       retention_block)

    def test_default_branch_still_extracts_and_deletes(self):
        """The final else branch extracts audio with default cleanup (deletes video)."""
        video_block = PROCESSING_MAIN[PROCESSING_MAIN.find('if is_video:'):]
        # The last else in the is_video block
        self.assertIn('extract_audio_from_video(filepath)', video_block)

    def test_temp_audio_cleanup_still_present(self):
        """Temp audio from retention is still cleaned up after transcription."""
        self.assertIn('is_video and VIDEO_RETENTION and audio_filepath', PROCESSING_MAIN)
        self.assertIn('Cleaned up temp audio from video retention', PROCESSING_MAIN)


# ===========================================================================
# 4. Incognito path
# ===========================================================================

class TestIncognitoPassthrough(unittest.TestCase):
    """Test passthrough in the incognito transcription path."""

    def test_passthrough_flag_set_in_incognito(self):
        """video_passthrough_active is computed in incognito path."""
        self.assertIn('video_passthrough_active = is_video and VIDEO_PASSTHROUGH_ASR',
                       PROCESSING_INCOGNITO)

    def test_passthrough_skips_extraction_in_incognito(self):
        """When passthrough is on, incognito skips extract_audio_from_video."""
        # The passthrough branch logs and does NOT extract
        self.assertIn('[Incognito] Video passthrough: sending original video to ASR',
                       PROCESSING_INCOGNITO)

    def test_passthrough_skips_conversion_in_incognito(self):
        """When passthrough is on, incognito skips convert_if_needed."""
        self.assertIn('[Incognito] Video passthrough: skipping codec conversion',
                       PROCESSING_INCOGNITO)

    def test_passthrough_skips_chunking_in_incognito(self):
        """When passthrough is on, incognito chunking is False."""
        body = PROCESSING_INCOGNITO
        self.assertIn('if video_passthrough_active:\n            should_chunk = False', body)

    def test_incognito_does_not_reference_video_retention(self):
        """Incognito path should NOT reference VIDEO_RETENTION (no retention in incognito)."""
        self.assertNotIn('VIDEO_RETENTION', PROCESSING_INCOGNITO)

    def test_incognito_still_extracts_without_passthrough(self):
        """Without passthrough, incognito still extracts audio from video."""
        self.assertIn('extract_audio_from_video(filepath, cleanup_original=False)',
                       PROCESSING_INCOGNITO)

    def test_incognito_still_converts_without_passthrough(self):
        """Without passthrough, incognito still runs convert_if_needed."""
        self.assertIn('convert_if_needed(', PROCESSING_INCOGNITO)


# ===========================================================================
# 5. Upload handler (recordings.py)
# ===========================================================================

class TestUploadHandlerPassthrough(unittest.TestCase):
    """Test recordings.py upload handler respects VIDEO_PASSTHROUGH_ASR."""

    def test_skip_conversion_for_passthrough_video(self):
        """Upload handler skips conversion when passthrough or retention + video."""
        self.assertIn('VIDEO_RETENTION or VIDEO_PASSTHROUGH_ASR) and has_video', RECORDINGS)

    def test_extension_fallback_checks_passthrough(self):
        """Extension-based video detection also fires for VIDEO_PASSTHROUGH_ASR."""
        self.assertIn('VIDEO_RETENTION or VIDEO_PASSTHROUGH_ASR', RECORDINGS)

    def test_convert_if_needed_still_in_else(self):
        """convert_if_needed still runs for audio files or when both flags are off."""
        self.assertIn('convert_if_needed(', RECORDINGS)

    def test_passthrough_log_message(self):
        """Upload handler logs which mode caused the skip."""
        self.assertIn("'VIDEO_PASSTHROUGH_ASR'", RECORDINGS)


# ===========================================================================
# 6. File monitor
# ===========================================================================

class TestFileMonitorPassthrough(unittest.TestCase):
    """Test file_monitor.py respects VIDEO_PASSTHROUGH_ASR."""

    def test_passthrough_defined(self):
        self.assertIn('VIDEO_PASSTHROUGH_ASR', FILE_MONITOR)

    def test_skip_conversion_for_passthrough_or_retention(self):
        """File monitor skips conversion when passthrough or retention + video."""
        self.assertIn('VIDEO_PASSTHROUGH_ASR or VIDEO_RETENTION) and has_video', FILE_MONITOR)

    def test_convert_if_needed_in_else_branch(self):
        """convert_if_needed is in the else branch, not inside the skip block."""
        lines = FILE_MONITOR.split('\n')
        in_skip_block = False
        found_else = False
        for i, line in enumerate(lines):
            if 'VIDEO_PASSTHROUGH_ASR or VIDEO_RETENTION) and has_video' in line:
                in_skip_block = True
            elif in_skip_block and line.strip().startswith('else:'):
                in_skip_block = False
                found_else = True
            elif in_skip_block and 'convert_if_needed' in line:
                self.fail(f"convert_if_needed inside skip block at line {i + 1}")
        self.assertTrue(found_else, "Should have else branch after passthrough/retention skip")

    def test_log_distinguishes_passthrough_from_retention(self):
        """Log message indicates whether passthrough or retention caused the skip."""
        self.assertIn("'passthrough'", FILE_MONITOR)
        self.assertIn("'retention'", FILE_MONITOR)


# ===========================================================================
# 7. Audio files unaffected by passthrough
# ===========================================================================

class TestAudioUnaffected(unittest.TestCase):
    """VIDEO_PASSTHROUGH_ASR must only affect video files, never audio."""

    def test_passthrough_flag_gated_on_is_video(self):
        """video_passthrough_active is always `is_video and VIDEO_PASSTHROUGH_ASR`."""
        # Main path
        self.assertIn('video_passthrough_active = is_video and VIDEO_PASSTHROUGH_ASR',
                       PROCESSING_MAIN)
        # Incognito path
        self.assertIn('video_passthrough_active = is_video and VIDEO_PASSTHROUGH_ASR',
                       PROCESSING_INCOGNITO)

    def test_upload_handler_gated_on_has_video(self):
        """Upload handler skip is gated on `has_video`."""
        self.assertIn('and has_video', RECORDINGS)

    def test_file_monitor_gated_on_has_video(self):
        """File monitor skip is gated on `has_video`."""
        self.assertIn('and has_video', FILE_MONITOR)


# ===========================================================================
# 8. Documentation
# ===========================================================================

class TestDocumentation(unittest.TestCase):
    """VIDEO_PASSTHROUGH_ASR is documented in all relevant places."""

    DOC_FILES = [
        'config/env.transcription.example',
        'docs/admin-guide/system-settings.md',
        'docs/features.md',
        'docs/getting-started/installation.md',
    ]

    def test_documented_in_all_relevant_files(self):
        for rel_path in self.DOC_FILES:
            content = read_file(rel_path)
            with self.subTest(file=rel_path):
                self.assertIn('VIDEO_PASSTHROUGH_ASR', content,
                              f"VIDEO_PASSTHROUGH_ASR missing from {rel_path}")

    def test_env_example_commented_out_by_default(self):
        """The env example has the option commented out (opt-in)."""
        self.assertIn('# VIDEO_PASSTHROUGH_ASR=false', ENV_EXAMPLE)

    def test_docs_warn_about_asr_compatibility(self):
        """Docs warn that standard APIs will reject video input."""
        system_settings = read_file('docs/admin-guide/system-settings.md')
        installation = read_file('docs/getting-started/installation.md')
        self.assertIn('reject', system_settings.lower())
        self.assertIn('reject', installation.lower())


# ===========================================================================
# 9. Interaction matrix — structural verification
# ===========================================================================

class TestInteractionMatrix(unittest.TestCase):
    """
    Verify the 3-way branch structure in processing.py:
      if VIDEO_PASSTHROUGH_ASR → passthrough
      elif VIDEO_RETENTION      → retention
      else                      → default extraction
    """

    def test_three_way_branch_in_main_path(self):
        """Main path has if/elif/else for passthrough/retention/default."""
        video_block = PROCESSING_MAIN[PROCESSING_MAIN.find('if is_video:'):]
        # All three branches present in order
        pt_pos = video_block.find('if VIDEO_PASSTHROUGH_ASR:')
        ret_pos = video_block.find('elif VIDEO_RETENTION:')
        else_pos = video_block.find('\n                else:', ret_pos)
        self.assertNotEqual(pt_pos, -1)
        self.assertNotEqual(ret_pos, -1)
        self.assertNotEqual(else_pos, -1)
        self.assertLess(pt_pos, ret_pos)
        self.assertLess(ret_pos, else_pos)

    def test_incognito_two_way_branch(self):
        """Incognito has if/else for passthrough/extract (no retention)."""
        video_block = PROCESSING_INCOGNITO[PROCESSING_INCOGNITO.find('if is_video:'):]
        pt_pos = video_block.find('if VIDEO_PASSTHROUGH_ASR:')
        else_pos = video_block.find('\n            else:', pt_pos)
        self.assertNotEqual(pt_pos, -1)
        self.assertNotEqual(else_pos, -1)
        # No VIDEO_RETENTION in incognito
        incognito_video_block = video_block[:500]
        self.assertNotIn('VIDEO_RETENTION', incognito_video_block)


if __name__ == '__main__':
    unittest.main(verbosity=2)
