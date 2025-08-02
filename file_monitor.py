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
from datetime import datetime
from pathlib import Path
import mimetypes
import subprocess
from werkzeug.utils import secure_filename

# Flask app components will be imported inside functions to avoid circular imports

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
        self.supported_extensions = {
            '.wav', '.mp3', '.flac', '.amr', '.3gp', '.3gpp', 
            '.m4a', '.aac', '.ogg', '.wma', '.webm', '.mp4', '.mov'
        }
        
        # Cache for admin user and valid users
        self._admin_user_id = None
        self._valid_users = {}  # Maps user_id to username
        self._username_to_id = {}  # Maps username to user_id
        self._last_user_cache_update = 0
        
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
        from app import app, db, User
            
        with app.app_context():
            try:
                # Find admin user
                admin_user = User.query.filter_by(is_admin=True).first()
                self._admin_user_id = admin_user.id if admin_user else None
                
                # Cache all valid users
                users = User.query.all()
                self._valid_users = {user.id: user.username for user in users}
                self._username_to_id = {user.username: user.id for user in users}
                
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
        
    def _scan_user_directories(self):
        """Scan user-specific subdirectories."""
        if not self.base_watch_directory.exists():
            return
            
        # Look for user directories (e.g., user123, user456)
        for item in self.base_watch_directory.iterdir():
            if not item.is_dir():
                continue
                
            # Extract user ID from directory name
            user_id = self._extract_user_id_from_dirname(item.name)
            if user_id and user_id in self._valid_users:
                self._scan_directory_for_user(item, user_id)
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
        else:
            self.logger.warning(f"Configured default username '{default_username}' is not valid")
            
    def _scan_directory_for_user(self, directory, user_id):
        """Scan a specific directory for files to process for a specific user."""
        if not directory.exists():
            return
            
        for file_path in directory.iterdir():
            if not file_path.is_file():
                continue

            # Skip hidden files, processing files, or non-supported files
            if file_path.name.startswith('.') or file_path.suffix == '.processing':
                continue
            
            if file_path.suffix.lower() not in self.supported_extensions:
                continue

            # Check if file is still being written (size stability check)
            try:
                if not self._is_file_stable(file_path):
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
                self._process_file(processing_path, user_id)
            except Exception as e:
                self.logger.error(f"Error processing file {processing_path}: {e}", exc_info=True)
                # If processing fails, unlock the file by renaming it back
                try:
                    original_path = processing_path.with_suffix(processing_path.suffix.replace('.processing', ''))
                    processing_path.rename(original_path)
                    self.logger.info(f"Unlocked file {processing_path} back to {original_path} after processing error.")
                except Exception as rename_err:
                    self.logger.error(f"CRITICAL: Failed to unlock file {processing_path} after error: {rename_err}")
                
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
            time.sleep(min(stability_time, 2))
            
            current_size = file_path.stat().st_size
            current_mtime = file_path.stat().st_mtime
            
            # File is stable if size and modification time haven't changed
            return initial_size == current_size and initial_mtime == current_mtime
            
        except (OSError, FileNotFoundError):
            return False
            
    def _process_file(self, processing_path, user_id):
        """
        Process a single locked audio file for a specific user.
        
        Args:
            processing_path (Path): Path to the locked audio file (e.g., file.mp3.processing)
            user_id (int): ID of the user to assign the recording to
        """
        # Import Flask components inside function to avoid circular imports
        from app import app, db, Recording, User, transcribe_audio_task
        
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
                safe_filename = secure_filename(original_filename)
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                new_filename = f"auto_{timestamp}_{safe_filename}"
                
                uploads_dir = Path(app.config['UPLOAD_FOLDER'])
                uploads_dir.mkdir(parents=True, exist_ok=True)
                destination_path = uploads_dir / new_filename
                
                # Copy locked file to uploads directory
                import shutil
                shutil.copy(str(processing_path), str(destination_path))
                self.logger.info(f"Copied {processing_path} to {destination_path}")
                
                # Delete the locked file from watch directory after successful copy
                try:
                    processing_path.unlink()
                    self.logger.info(f"Deleted locked file: {processing_path}")
                except FileNotFoundError:
                    # This should not happen if the lock is held, but good to log
                    self.logger.warning(f"Locked file {processing_path} was already deleted.")
                
                # Convert file if necessary (same logic as upload_file)
                final_path = self._convert_file_if_needed(destination_path, original_filename)
                
                # Get file size and MIME type
                file_size = final_path.stat().st_size
                mime_type, _ = mimetypes.guess_type(str(final_path))
                
                # Create database record
                recording = Recording(
                    audio_path=str(final_path),
                    original_filename=original_filename,
                    title=f"Auto-processed - {original_filename}",
                    file_size=file_size,
                    status='PENDING',
                    meeting_date=datetime.utcnow().date(),
                    user_id=user_id,
                    mime_type=mime_type,
                    is_inbox=True,  # Auto-processed files go to inbox
                    processing_source='auto_process'  # Track that this was auto-processed
                )
                
                db.session.add(recording)
                db.session.commit()
                
                self.logger.info(f"Created recording record with ID: {recording.id} for user: {user.username}")
                
                # Start background processing
                start_time = datetime.utcnow()
                thread = threading.Thread(
                    target=transcribe_audio_task,
                    args=(app.app_context(), recording.id, str(final_path), final_path.name, start_time)
                )
                thread.start()
                
                self.logger.info(f"Started background processing for recording ID: {recording.id}")
                self.logger.info(f"Successfully processed and moved file from: {processing_path}")
                    
            except Exception as e:
                self.logger.error(f"Error processing file {processing_path} for user {user_id}: {e}", exc_info=True)
                # Re-raise the exception to be caught by the calling method, which will handle unlocking.
                raise
                
    def _convert_file_if_needed(self, file_path, original_filename):
        """
        Convert audio file to supported format if needed.
        Uses 32kbps MP3 for optimal size/quality balance.
        
        Args:
            file_path (Path): Current file path
            original_filename (str): Original filename for format detection
            
        Returns:
            Path: Path to the final (possibly converted) file
        """
        filename_lower = original_filename.lower()
        
        # Support WebM and other formats directly when possible
        supported_formats = ('.wav', '.mp3', '.flac', '.webm', '.m4a', '.aac', '.ogg')
        convertible_formats = ('.amr', '.3gp', '.3gpp', '.wma', '.mp4', '.mov')
        
        if filename_lower.endswith(supported_formats):
            self.logger.info(f"File format {filename_lower} is supported, no conversion needed")
            return file_path
            
        if not filename_lower.endswith(convertible_formats):
            self.logger.warning(f"Unknown file format {filename_lower}, attempting conversion anyway")
            
        # Need to convert to 32kbps MP3 for optimal size
        self.logger.info(f"Converting {filename_lower} format to 32kbps MP3")
        
        base_path = file_path.with_suffix('')
        temp_mp3_path = base_path.with_suffix('.temp.mp3')
        final_mp3_path = base_path.with_suffix('.mp3')
        
        try:
            # Convert to 32kbps MP3 for optimal size/quality balance
            subprocess.run([
                'ffmpeg', '-i', str(file_path), '-y', 
                '-acodec', 'mp3', '-ab', '32k', '-ar', '16000', '-ac', '1', 
                str(temp_mp3_path)
            ], check=True, capture_output=True, text=True)
            
            self.logger.info(f"Successfully converted {file_path} to {temp_mp3_path} (32kbps MP3)")
            
            # Remove original and rename temp file
            file_path.unlink()
            temp_mp3_path.rename(final_mp3_path)
            
            return final_mp3_path
            
        except FileNotFoundError:
            self.logger.error("ffmpeg not found. Please ensure ffmpeg is installed.")
            raise
        except subprocess.CalledProcessError as e:
            self.logger.error(f"ffmpeg conversion failed: {e.stderr}")
            raise


# Global file monitor instance
file_monitor = None

def start_file_monitor():
    """Start the file monitor with configuration from environment variables."""
    global file_monitor
    
    if file_monitor and file_monitor.running:
        return
    
    # Import Flask app inside function to avoid circular imports
    from app import app
        
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
    else:
        app.logger.info("Automated file processing is disabled")

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
