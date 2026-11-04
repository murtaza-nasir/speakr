#!/usr/bin/env python3
"""
File Monitor for Automated Audio Processing
Monitors directories for new audio files and automatically processes them.
Supports multiple user modes:
1. Admin-only: Files go to admin user only
2. User-specific directories: Each user has their own folder (e.g., /auto-process/user123/)
3. Single default user: All files go to one specified user
"""

import os
import time
import threading
import logging
from pathlib import Path

# Flask app components will be imported inside functions to avoid circular imports

# A file that cannot be processed is renamed to <name>.failed, which later
# scans skip; the owner renames it back to try again. A file ingestion refuses
# (unreadable, empty, a video without audio) is set aside at once. Any other
# failure is retried, after 1, 2, 4 and 8 minutes, and the file is set aside
# after the fifth.
FAILED_SUFFIX = '.failed'
MAX_ATTEMPTS = 5
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600


class IngestionRefused(Exception):
    """Ingestion refused the file; trying it again would not change that."""

class FileMonitor:
    def __init__(self, base_watch_directory, check_interval=30, mode='admin_only'):
        """
        Initialize the file monitor.
        
        Args:
            base_watch_directory (str): Base directory to monitor for new files
            check_interval (int): How often to check for new files (seconds)
            mode (str): Processing mode - 'admin_only', 'user_directories', or 'single_user'
        """
        self.base_watch_directory = Path(base_watch_directory)
        self.check_interval = check_interval
        self.mode = mode
        self.running = False
        self.thread = None
        
        # Ensure base directory exists
        self.base_watch_directory.mkdir(parents=True, exist_ok=True)
        
        # Setup logging
        self.logger = logging.getLogger('file_monitor')
        self.logger.setLevel(logging.INFO)
        
        # Supported audio file extensions
        # We'll use ffprobe to detect audio files instead of extensions
        # Keep a basic list for initial filtering to avoid probing every file
        self.potential_audio_extensions = {
            '.wav', '.mp3', '.flac', '.amr', '.3gp', '.3gpp', 
            '.m4a', '.aac', '.ogg', '.oga', '.wma', '.webm', '.mp4', '.mov',
            '.opus', '.caf', '.aiff', '.ts', '.mts', '.mkv', '.avi',
            '.m4v', '.wmv', '.flv', '.mpeg', '.mpg', '.ogv', '.vob', '.asf'
        }
        
        # Cache for admin user and valid users
        self._admin_user_id = None
        self._valid_users = {}  # Maps user_id to username
        self._username_to_id = {}  # Maps username to user_id
        self._last_user_cache_update = 0

        # Files left in the folder for the storage quota, logged once (#413)
        self._quota_skipped = set()

        # Failed attempts per watched file: path -> (attempts, retry after)
        self._failures = {}

    def start(self):
        """Start the file monitoring in a background thread."""
        if self.running:
            self.logger.warning("File monitor is already running")
            return
            
        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        self.logger.info(f"File monitor started in '{self.mode}' mode, watching: {self.base_watch_directory}")
        
    def stop(self):
        """Stop the file monitoring."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        self.logger.info("File monitor stopped")
        
    def _update_user_cache(self):
        """Update the cache of valid users and admin user."""
        current_time = time.time()
        # Update cache every 5 minutes
        if current_time - self._last_user_cache_update < 300:
            return
        
        # Import Flask components inside function to avoid circular imports
        from src.app import app, db, User
            
        with app.app_context():
            try:
                # Find admin user
                admin_user = User.query.filter_by(is_admin=True).first()
                self._admin_user_id = admin_user.id if admin_user else None
                
                # Cache all valid users
                users = User.query.all()
                self._valid_users = {user.id: user.username for user in users}
                self._username_to_id = {user.username: user.id for user in users}
                # Username-named watch folders (#365) also accept the
                # filesystem-safe variant, matching the per-user export
                # directories which are created as secure_filename(username).
                from werkzeug.utils import secure_filename
                for user in users:
                    safe = secure_filename(user.username)
                    if safe and safe not in self._username_to_id:
                        self._username_to_id[safe] = user.id
                
                self._last_user_cache_update = current_time
                self.logger.debug(f"Updated user cache: {len(self._valid_users)} users, admin: {self._admin_user_id}")
                
            except Exception as e:
                self.logger.error(f"Error updating user cache: {e}")
        
    def _monitor_loop(self):
        """Main monitoring loop."""
        while self.running:
            try:
                self._update_user_cache()
                
                if self.mode == 'admin_only':
                    self._scan_admin_directory()
                elif self.mode == 'user_directories':
                    self._scan_user_directories()
                elif self.mode == 'single_user':
                    self._scan_single_user_directory()
                    
            except Exception as e:
                self.logger.error(f"Error during directory scan: {e}", exc_info=True)
            
            # Wait for next check
            time.sleep(self.check_interval)
            
    def _scan_admin_directory(self):
        """Scan the main directory for files to process as admin user."""
        if not self._admin_user_id:
            self.logger.warning("No admin user found, skipping admin directory scan")
            return

        self._scan_directory_for_user(self.base_watch_directory, self._admin_user_id)
        self._scan_tag_subdirectories(self.base_watch_directory, self._admin_user_id)

    def _scan_user_directories(self):
        """Scan user-specific subdirectories."""
        if not self.base_watch_directory.exists():
            return

        # Look for user directories, either id-based (user123, 123) or named
        # after the username itself (#365, consistent with export folders).
        for item in self.base_watch_directory.iterdir():
            if not item.is_dir():
                continue

            # Extract user ID from directory name
            user_id = self._extract_user_id_from_dirname(item.name)
            if user_id and user_id in self._valid_users:
                self._scan_directory_for_user(item, user_id)
                self._scan_tag_subdirectories(item, user_id)
                continue

            # Fall back to matching the directory name against usernames
            # (raw or secure_filename form).
            username_user_id = self._username_to_id.get(item.name)
            if username_user_id:
                self._scan_directory_for_user(item, username_user_id)
                self._scan_tag_subdirectories(item, username_user_id)
            elif item.name.startswith('user'):
                self.logger.warning(f"Found user directory '{item.name}' but user ID {user_id} is not valid")

    def _scan_single_user_directory(self):
        """Scan directory for a single configured user."""
        default_username = os.environ.get('AUTO_PROCESS_DEFAULT_USERNAME')
        if not default_username:
            self.logger.warning("AUTO_PROCESS_DEFAULT_USERNAME not configured for single_user mode")
            return

        user_id = self._username_to_id.get(default_username)
        if user_id:
            self._scan_directory_for_user(self.base_watch_directory, user_id)
            self._scan_tag_subdirectories(self.base_watch_directory, user_id)
        else:
            self.logger.warning(f"Configured default username '{default_username}' is not valid")

    def _scan_tag_subdirectories(self, directory, user_id):
        """Scan subdirectories that match auto-process tag folders."""
        if not directory.exists():
            return

        from src.app import app
        from src.models import Tag

        with app.app_context():
            for item in directory.iterdir():
                if not item.is_dir():
                    continue

                # Skip hidden dirs and user directories (e.g., user123)
                if item.name.startswith('.') or self._extract_user_id_from_dirname(item.name) is not None:
                    continue

                # Look up matching auto-process tag
                tag = Tag.query.filter_by(
                    user_id=user_id,
                    is_auto_process=True,
                    auto_process_folder_name=item.name
                ).first()

                if tag:
                    self._scan_directory_for_user(item, user_id, tag_id=tag.id)
            
    def _scan_directory_for_user(self, directory, user_id, tag_id=None):
        """Scan a specific directory for files to process for a specific user."""
        if not directory.exists():
            return

        for file_path in directory.iterdir():
            if not file_path.is_file():
                continue

            # Skip hidden files, processing files, or non-supported files
            if file_path.name.startswith('.') or file_path.suffix == '.processing':
                continue

            if file_path.suffix.lower() not in self.potential_audio_extensions:
                continue

            failure = self._failures.get(str(file_path))
            if failure and time.time() < failure[1]:
                continue

            # Check if file is still being written (size stability check)
            stability_time = int(os.environ.get('AUTO_PROCESS_STABILITY_TIME', '5'))
            try:
                if not self._is_file_stable(file_path, stability_time):
                    continue
            except FileNotFoundError:
                # File might have been picked up by another worker after iterdir()
                continue

            self.logger.info(f"Found potential audio file for user {user_id}: {file_path}")

            # --- Atomic Lock via Rename ---
            processing_path = file_path.with_suffix(file_path.suffix + '.processing')

            try:
                file_path.rename(processing_path)
                self.logger.info(f"Acquired lock for {file_path}, renamed to {processing_path}")
            except FileNotFoundError:
                self.logger.debug(f"Could not acquire lock for {file_path}, already processed by another worker.")
                continue
            except Exception as e:
                self.logger.error(f"Error acquiring lock for {file_path}: {e}")
                continue

            # --- Process the locked file ---
            try:
                self._process_file(processing_path, user_id, tag_id=tag_id)
                self._failures.pop(str(file_path), None)
            except IngestionRefused as e:
                self._failures.pop(str(file_path), None)
                self._set_aside(processing_path, file_path, user_id, 'notifications.autoProcessRefused',
                                f"refused: {e}", reason=str(e))
            except Exception as e:
                self.logger.error(f"Error processing file {processing_path}: {e}", exc_info=True)
                attempts = self._failures.get(str(file_path), (0, 0))[0] + 1
                if attempts >= MAX_ATTEMPTS:
                    self._failures.pop(str(file_path), None)
                    self._set_aside(processing_path, file_path, user_id, 'notifications.autoProcessFailed',
                                    f"failed {attempts} times, last: {e}")
                    continue
                delay = min(RETRY_BASE_SECONDS * 2 ** (attempts - 1), RETRY_MAX_SECONDS)
                self._failures[str(file_path)] = (attempts, time.time() + delay)
                # Unlock the file by renaming it back; it is tried again after the delay
                try:
                    processing_path.rename(file_path)
                    self.logger.info(f"Unlocked {processing_path} after processing error "
                                     f"(attempt {attempts} of {MAX_ATTEMPTS}, next in {delay}s).")
                except Exception as rename_err:
                    self.logger.error(f"CRITICAL: Failed to unlock file {processing_path} after error: {rename_err}")

    def _set_aside(self, processing_path, file_path, user_id, message_key, log_reason, reason=None):
        """Rename a file that cannot be processed to <name>.failed and tell its owner once."""
        target = file_path.with_name(file_path.name + FAILED_SUFFIX)
        if target.exists():
            target = file_path.with_name(f"{file_path.stem}-{int(time.time())}{file_path.suffix}{FAILED_SUFFIX}")
        try:
            processing_path.rename(target)
        except Exception as e:
            self.logger.error(f"CRITICAL: Failed to set aside {processing_path} as {target.name}: {e}")
            return
        self.logger.warning(f"Set aside {file_path} as {target.name}, {log_reason}")
        try:
            from src.app import app
            from src.services.notifications import notify
            params = {'filename': file_path.name, 'failed_name': target.name}
            if reason:
                params['reason'] = reason
            with app.app_context():
                notify('auto_process.file_set_aside', message_key, user_ids=[user_id], level='warning',
                       params=params, dedupe_key=f'auto_process.file_set_aside:{user_id}:{target.name}'[:200])
        except Exception as e:
            self.logger.warning(f"Could not notify user {user_id} about {target.name}: {e}")
                
    def _extract_user_id_from_dirname(self, dirname):
        """
        Extract user ID from directory name.
        
        Expected formats: user123, 123
        
        Args:
            dirname (str): Directory name
            
        Returns:
            int or None: User ID if found, None otherwise
        """
        import re
        
        # Pattern: user123 or just 123
        patterns = [
            r'^user(\d+)$',  # user123
            r'^(\d+)$'       # 123
        ]
        
        for pattern in patterns:
            match = re.match(pattern, dirname, re.IGNORECASE)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    continue
                    
        return None
        
    def _is_file_stable(self, file_path, stability_time=5):
        """
        Check if a file is stable (not being written to).
        
        Args:
            file_path (Path): Path to the file
            stability_time (int): Time in seconds to wait for size stability
            
        Returns:
            bool: True if file appears stable
        """
        try:
            initial_size = file_path.stat().st_size
            initial_mtime = file_path.stat().st_mtime
            
            # Wait a bit and check again
            time.sleep(stability_time)
            
            current_size = file_path.stat().st_size
            current_mtime = file_path.stat().st_mtime
            
            # File is stable if size and modification time haven't changed
            return initial_size == current_size and initial_mtime == current_mtime
            
        except (OSError, FileNotFoundError):
            return False
            
    def _process_file(self, processing_path, user_id, tag_id=None):
        """
        Process a single locked audio file for a specific user.

        Args:
            processing_path (Path): Path to the locked audio file (e.g., file.mp3.processing)
            user_id (int): ID of the user to assign the recording to
            tag_id (int, optional): Tag ID to apply to the recording
        """
        # Import Flask components inside function to avoid circular imports
        from src.app import app, db, User
        
        with app.app_context():
            try:
                # Verify user exists
                user = db.session.get(User, user_id)
                if not user:
                    self.logger.error(f"User ID {user_id} not found in database for file {processing_path}")
                    # We must raise an exception to trigger the unlock mechanism
                    raise ValueError(f"User ID {user_id} not found")

                # Derive original filename by removing .processing suffix
                original_filename = processing_path.name.replace('.processing', '')

                # Storage quota (#413): a file that does not fit stays in the
                # folder untouched and is tried again on later scans.
                from src.services.storage_quota import check_room, StorageQuotaExceeded, raise_notice
                try:
                    check_room(user, processing_path.stat().st_size)
                except StorageQuotaExceeded as quota_error:
                    original_path = processing_path.with_name(original_filename)
                    try:
                        processing_path.rename(original_path)
                    except FileNotFoundError:
                        pass
                    if str(original_path) not in self._quota_skipped:
                        self._quota_skipped.add(str(original_path))
                        self.logger.info(f"Left {original_path} in the watch folder: {quota_error}")
                        raise_notice(user, quota_error, original_filename)
                    return
                self._quota_skipped.discard(str(processing_path.with_name(original_filename)))

                # Delegate the rest of the pipeline (probe, conversion, storage,
                # Recording creation, enqueue) to the shared ingestion path so
                # the monitor and the upload entry point never drift apart.
                # The monitor's only responsibilities left are: quota gating
                # (above), handing the file over as a path source, applying a
                # tag via the form, and cleaning up the locked watch-dir file.
                from src.services.recording_source import PathSource
                from src.api.recordings import ingest_uploaded_recording

                # Preserve the monitor's filename-date parsing by forwarding it
                # as a meeting_date when the owner opted in.
                form = {}
                if tag_id is not None:
                    form['tag_ids[0]'] = str(tag_id)
                try:
                    if getattr(user, 'parse_filename_dates', False):
                        from src.utils.filename_dates import parse_filename_date
                        md = parse_filename_date(
                            original_filename,
                            pattern_key=user.filename_date_pattern or 'auto',
                            custom_regex=user.filename_date_regex,
                        )
                        if md:
                            form['meeting_date'] = md.isoformat()
                except Exception as e:
                    self.logger.warning(f"Could not parse filename date for {original_filename}: {e}")

                from src.utils.titles import placeholder_titles
                def _prepare(recording):
                    recording.title = placeholder_titles(original_filename)[1]
                    recording.title_source = None

                source = PathSource(processing_path)
                response, status = ingest_uploaded_recording(
                    owner=user,
                    source=source,
                    form=form,
                    original_filename=original_filename,
                    processing_source='auto_process',
                    success_status=202,
                    prepare=_prepare,
                    enforce_upload_limits=False,
                )

                # ingest returns a Flask response tuple.
                try:
                    payload = response.get_json(silent=True) or {}
                except Exception:
                    payload = {}
                source.close()
                if 400 <= int(status) < 500:
                    raise IngestionRefused(payload.get('error') or f"status {status}")
                if not (200 <= int(status) < 300):
                    raise RuntimeError(
                        f"ingestion failed with status {status}: {payload.get('error') or payload}"
                    )

                recording_id = payload.get('id')
                self.logger.info(f"Created recording record with ID: {recording_id} for user: {user.username}")

                # Remove the locked file from the watch directory now that ingestion
                # has copied it into staging.  ingest never deletes its source.
                try:
                    processing_path.unlink()
                    self.logger.info(f"Deleted locked file: {processing_path}")
                except FileNotFoundError:
                    self.logger.warning(f"Locked file {processing_path} was already deleted.")

                self.logger.info(f"Queued background processing for recording ID: {recording_id}")
                self.logger.info(f"Successfully processed and moved file from: {processing_path}")

                    
            except IngestionRefused:
                raise
            except Exception as e:
                self.logger.error(f"Error processing file {processing_path} for user {user_id}: {e}", exc_info=True)
                # Re-raise the exception to be caught by the calling method, which will handle unlocking.
                raise
                



# Global file monitor instance
file_monitor = None

def start_file_monitor():
    """Start the file monitor with configuration from environment variables."""
    global file_monitor
    
    if file_monitor and file_monitor.running:
        return
    
    # Import Flask app inside function to avoid circular imports
    from src.app import app
        
    # Get configuration from environment
    watch_dir = os.environ.get('AUTO_PROCESS_WATCH_DIR', '/data/auto-process')
    check_interval = int(os.environ.get('AUTO_PROCESS_CHECK_INTERVAL', '30'))
    mode = os.environ.get('AUTO_PROCESS_MODE', 'admin_only')  # admin_only, user_directories, single_user
    
    # Validate mode
    valid_modes = ['admin_only', 'user_directories', 'single_user']
    if mode not in valid_modes:
        app.logger.error(f"Invalid AUTO_PROCESS_MODE: {mode}. Must be one of: {valid_modes}")
        return
            
    # Only start if auto-processing is enabled
    if os.environ.get('ENABLE_AUTO_PROCESSING', 'false').lower() == 'true':
        file_monitor = FileMonitor(
            base_watch_directory=watch_dir,
            check_interval=check_interval,
            mode=mode
        )
        file_monitor.start()
        app.logger.info(f"Automated file processing started in '{mode}' mode")

        # Ensure all auto-process tag folders exist
        _ensure_tag_folders_on_startup(app, watch_dir, mode)
    else:
        app.logger.info("Automated file processing is disabled")


def _ensure_tag_folders_on_startup(app, watch_dir, mode):
    """Ensure all auto-process tag folders exist on startup."""
    with app.app_context():
        try:
            from src.database import db
            from src.models import Tag
            from src.models.user import User
            tags = Tag.query.filter_by(is_auto_process=True).all()
            if not tags:
                return

            for tag in tags:
                user = db.session.get(User, tag.user_id)
                if not user:
                    continue

                if mode == 'user_directories':
                    base_dir = Path(watch_dir) / f'user{user.id}'
                else:
                    base_dir = Path(watch_dir)

                if tag.auto_process_folder_name:
                    folder_path = base_dir / tag.auto_process_folder_name
                    try:
                        folder_path.mkdir(parents=True, exist_ok=True)
                    except OSError as e:
                        app.logger.error(f"Could not create auto-process folder {folder_path}: {e}")

            app.logger.info(f"Verified {len(tags)} auto-process tag folder(s)")
        except Exception as e:
            app.logger.warning(f"Error ensuring tag folders on startup: {e}")

def stop_file_monitor():
    """Stop the file monitor."""
    global file_monitor
    if file_monitor:
        file_monitor.stop()
        file_monitor = None

def get_file_monitor_status():
    """Get the current status of the file monitor."""
    global file_monitor
    if file_monitor and file_monitor.running:
        return {
            'running': True,
            'mode': file_monitor.mode,
            'watch_directory': str(file_monitor.base_watch_directory),
            'check_interval': file_monitor.check_interval
        }
    else:
        return {'running': False}
