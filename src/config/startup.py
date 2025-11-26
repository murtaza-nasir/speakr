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
    """Initialize and start the background job queue."""
    try:
        from src.services.job_queue import job_queue
        job_queue.start()
        app.logger.info("Job queue started with 2 workers")
    except Exception as e:
        app.logger.error(f"Failed to start job queue: {e}")


def run_startup_tasks(app):
    """Run all startup tasks that need to happen after app creation."""
    from src.models import SystemSetting

    with app.app_context():
        # Set dynamic MAX_CONTENT_LENGTH based on database setting
        max_file_size_mb = SystemSetting.get_setting('max_file_size_mb', 250)
        app.config['MAX_CONTENT_LENGTH'] = max_file_size_mb * 1024 * 1024
        app.logger.info(f"Set MAX_CONTENT_LENGTH to {max_file_size_mb}MB from database setting")

        # Initialize job queue for background processing
        initialize_job_queue(app)

        # Initialize file monitor after app setup
        initialize_file_monitor(app)

        # Initialize file exporter
        initialize_file_exporter(app)

        # Initialize auto-deletion scheduler
        initialize_auto_deletion_scheduler(app)
