"""
Application startup functions.
"""

import os
import time
import threading
from datetime import datetime, timedelta
from flask import current_app

ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
GLOBAL_RETENTION_DAYS = int(os.environ.get('GLOBAL_RETENTION_DAYS', '0'))


def initialize_file_monitor(app):
    """Initialize file monitor after app is fully loaded to avoid circular imports."""
    try:
        # Import here to avoid circular imports
        import src.file_monitor as file_monitor
        file_monitor.start_file_monitor()
        app.logger.info("File monitor initialization completed")
    except Exception as e:
        app.logger.warning(f"File monitor initialization failed: {e}")

def get_file_monitor_functions(app):
    """Get file monitor functions, handling import errors gracefully."""
    try:
        import src.file_monitor as file_monitor
        return file_monitor.start_file_monitor, file_monitor.stop_file_monitor, file_monitor.get_file_monitor_status
    except ImportError as e:
        app.logger.warning(f"File monitor not available: {e}")

        # Create stub functions if file_monitor is not available
        def start_file_monitor():
            pass
        def stop_file_monitor():
            pass
        def get_file_monitor_status():
            return {'running': False, 'error': 'File monitor module not available'}

        return start_file_monitor, stop_file_monitor, get_file_monitor_status

# --- Auto-Processing API Endpoints ---
def initialize_auto_deletion_scheduler(app):
    """Initialize the daily auto-deletion scheduler if enabled."""
    from src.services.retention import process_auto_deletion

    if not ENABLE_AUTO_DELETION:
        app.logger.info("Auto-deletion scheduler not started (ENABLE_AUTO_DELETION=false)")
        return

    if GLOBAL_RETENTION_DAYS <= 0:
        app.logger.info("Auto-deletion scheduler not started (GLOBAL_RETENTION_DAYS not set)")
        return

    def run_daily_deletion():
        """Background thread that runs auto-deletion daily at 2 AM."""
        import time
        from datetime import datetime, timedelta

        app.logger.info("Auto-deletion scheduler started - will run daily at 2:00 AM")

        while True:
            try:
                # Calculate time until next 2 AM
                now = datetime.now()
                next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)

                # If it's past 2 AM today, schedule for tomorrow
                if now.hour >= 2:
                    next_run += timedelta(days=1)

                sleep_seconds = (next_run - now).total_seconds()

                app.logger.info(f"Next auto-deletion scheduled for: {next_run.strftime('%Y-%m-%d %H:%M:%S')} (in {sleep_seconds/3600:.1f} hours)")

                # Sleep until next run time
                time.sleep(sleep_seconds)

                # Run auto-deletion
                app.logger.info("Running scheduled auto-deletion...")
                with app.app_context():
                    stats = process_auto_deletion()
                    app.logger.info(f"Scheduled auto-deletion completed: {stats}")

            except Exception as e:
                app.logger.error(f"Error in auto-deletion scheduler: {e}", exc_info=True)
                # Sleep for 1 hour before retrying on error
                time.sleep(3600)

    # Start the scheduler thread
    import threading
    scheduler_thread = threading.Thread(target=run_daily_deletion, daemon=True, name="AutoDeletionScheduler")
    scheduler_thread.start()
    app.logger.info("✅ Auto-deletion scheduler initialized - running daily at 2:00 AM")


def initialize_file_exporter(app):
    """Initialize file exporter after app is fully loaded."""
    try:
        from src.file_exporter import initialize_export_directory, ENABLE_AUTO_EXPORT
        if ENABLE_AUTO_EXPORT:
            initialize_export_directory()
            app.logger.info("✅ Auto-export initialized")
        else:
            app.logger.info("ℹ️  Auto-export: Disabled (set ENABLE_AUTO_EXPORT=true to enable)")
    except Exception as e:
        app.logger.warning(f"File exporter initialization failed: {e}")


def initialize_job_queue(app):
    """Initialize and start the background job queue with orphan recovery."""
    try:
        from src.services.job_queue import job_queue

        # Initialize job queue with app context
        job_queue.init_app(app)

        # Recover any jobs that were processing when the app crashed
        job_queue.recover_orphaned_jobs()

        # Start worker threads
        job_queue.start()

        # Get queue status
        status = job_queue.get_queue_status()
        t_queue = status['transcription_queue']
        s_queue = status['summary_queue']
        app.logger.info(
            f"Job queues started: "
            f"transcription ({t_queue['workers']} workers, {t_queue['queued']} queued), "
            f"summary ({s_queue['workers']} workers, {s_queue['queued']} queued)"
        )
    except Exception as e:
        app.logger.error(f"Failed to start job queue: {e}", exc_info=True)


# Module-level guard so reloads (Flask --reload, gunicorn --reload,
# container restart-on-change) don't spawn a second cleanup thread on
# every reinit. The webhook dispatcher in src/services/webhook_dispatch.py
# uses the same pattern.
_cleanup_thread_started = False


def initialize_recording_session_cleanup(app):
    """Sweep expired recording sessions on a background thread.

    Issue #287 (c)(d): in-progress recording sessions accumulate chunk
    files in ``UPLOAD_FOLDER/_sessions/<id>/``. The session row's
    ``last_seen_at`` is updated on every chunk POST; sessions that go
    silent for longer than ``RECORDING_SESSION_TTL_HOURS`` (default 24)
    are reaped here and their on-disk dirs removed.

    Runs on the same pattern as the other schedulers in this module
    (daemon thread with a sleep loop), with a configurable interval via
    ``RECORDING_SESSION_CLEANUP_INTERVAL_SECONDS`` (default 3600 = hourly).
    """
    import os
    import threading
    import time

    global _cleanup_thread_started
    if _cleanup_thread_started:
        # Reload or duplicate init; the existing daemon is fine.
        app.logger.info("Recording-session cleanup thread already running; skipping duplicate init")
        return

    interval = int(os.environ.get('RECORDING_SESSION_CLEANUP_INTERVAL_SECONDS', '3600'))
    if interval <= 0:
        app.logger.info("Recording-session cleanup disabled (interval <= 0)")
        return

    def _loop():
        from src.api.recording_sessions import cleanup_expired_sessions
        app.logger.info(f"Recording-session cleanup scheduler started (interval={interval}s)")
        # First sweep happens after the interval, not at boot, to avoid
        # contention with other startup tasks.
        while True:
            try:
                time.sleep(interval)
                reaped = cleanup_expired_sessions(app=app)
                if reaped:
                    app.logger.info(f"Recording-session cleanup reaped {reaped} session(s)")
            except Exception as e:
                app.logger.error(f"Recording-session cleanup error: {e}", exc_info=True)
                # Don't tight-loop on failure
                time.sleep(60)

    thread = threading.Thread(target=_loop, daemon=True, name="RecordingSessionCleanup")
    thread.start()
    _cleanup_thread_started = True
    app.logger.info("✅ Recording-session cleanup scheduler initialized")


def run_startup_tasks(app):
    """Run all startup tasks that need to happen after app creation."""
    from src.models import SystemSetting

    with app.app_context():
        # Set dynamic MAX_CONTENT_LENGTH from DB settings. The Werkzeug
        # ceiling is the higher of the two limits because audio-only video
        # uploads can exceed max_file_size_mb (only the extracted audio
        # has to fit that). The view-level handler re-checks each request
        # against the right effective limit.
        max_file_size_mb = int(SystemSetting.get_setting('max_file_size_mb', 250))
        max_audio_only_video_mb = int(
            SystemSetting.get_setting('max_audio_only_video_size_mb', max_file_size_mb * 4)
        )
        wsgi_ceiling_mb = max(max_file_size_mb, max_audio_only_video_mb)
        app.config['MAX_CONTENT_LENGTH'] = wsgi_ceiling_mb * 1024 * 1024
        app.logger.info(
            f"Set MAX_CONTENT_LENGTH to {wsgi_ceiling_mb}MB "
            f"(max_file_size_mb={max_file_size_mb}, "
            f"max_audio_only_video_size_mb={max_audio_only_video_mb})"
        )

        # Initialize job queue for background processing
        initialize_job_queue(app)

        # Initialize file monitor after app setup
        initialize_file_monitor(app)

        # Initialize file exporter
        initialize_file_exporter(app)

        # Initialize auto-deletion scheduler
        initialize_auto_deletion_scheduler(app)

        # Initialize recording-session cleanup scheduler (#287 c/d)
        initialize_recording_session_cleanup(app)

        # Initialize webhook dispatcher (#275)
        from src.services.webhook_dispatch import start_dispatcher_thread
        start_dispatcher_thread(app)
