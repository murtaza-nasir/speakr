#!/usr/bin/env python3

# Add this near the top if you run this standalone often outside app context
import os
import sys
import shutil
# Add project root to path if necessary for 'app' import
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Load environment variables in case DB path relies on them (optional here)
# from dotenv import load_dotenv
# load_dotenv()

# Check if running within app context already (e.g., via Flask command)
try:
    from flask import current_app
    # Ensure app context is pushed if needed for config access
    app = current_app._get_current_object()
    # Make sure db is initialized within the app context if needed
    # (SQLAlchemy initialization in app.py handles this mostly)
    with app.app_context():
      db = app.extensions['sqlalchemy'].db # Access db via extensions
except (RuntimeError, AttributeError, KeyError):
     # If not in app context, import directly
    try:
        # Ensure this import reflects the updated app.py with the new model
        from src.app import app, db
    except ImportError as e:
        print(f"Error: Could not import 'app' and 'db': {e}")
        print("Make sure reset_db.py is runnable and PYTHONPATH is set.")
        sys.exit(1)

def reset_database(delete_uploads=True):
    # Determine the database path relative to the instance folder
    # Use app config if available
    instance_path = app.instance_path if hasattr(app, 'instance_path') else os.path.join(os.getcwd(), 'instance')
    try:
        # Ensure app context for config access if not already present
        with app.app_context():
             # Use absolute path from config
             db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', 'sqlite:///instance/transcriptions.db')
             # Handle relative vs absolute paths specified in URI
             if db_uri.startswith('sqlite:///'):
                 # Assume absolute path from URI root if starts with '///'
                 db_path = db_uri.replace('sqlite:///', '/', 1) # Replace only first
                 # Ensure instance path reflects the directory containing the DB
                 instance_path = os.path.dirname(db_path)
             elif db_uri.startswith('sqlite://'):
                 # Assume relative path from instance folder
                 db_filename = db_uri.split('/')[-1]
                 db_path = os.path.join(instance_path, db_filename)
             else: # Handle other DB types or formats if needed
                 print(f"Warning: Non-SQLite URI detected: {db_uri}. Deletion logic might need adjustment.")
                 # Attempt to parse or fallback
                 db_filename = db_uri.split('/')[-1] # Best guess
                 db_path = os.path.join(instance_path, db_filename)

    except Exception as config_e:
        print(f"Error accessing app config for DB path: {config_e}. Using default.")
        # Fallback if config access fails
        instance_path = os.path.join(os.getcwd(), 'instance')
        db_filename = 'transcriptions.db'
        db_path = os.path.join(instance_path, db_filename)

    # Ensure instance directory exists
    print(f"Ensuring instance directory exists: {instance_path}")
    os.makedirs(instance_path, exist_ok=True)
    print(f"Database path identified as: {db_path}")

    # Remove existing database if it exists
    if os.path.exists(db_path):
        print(f"Removing existing database at {db_path}")
        try:
            os.remove(db_path)
            # Also remove journal file if it exists
            journal_path = db_path + "-journal"
            if os.path.exists(journal_path):
                os.remove(journal_path)
                print(f"Removing existing journal file at {journal_path}")
        except OSError as e:
            print(f"Error removing database file: {e}. Check permissions or if it's in use.")
            # Decide whether to exit or continue
            # sys.exit(1)

    # Create application context to work with the database
    try:
        with app.app_context():
            print("Creating new database schema (including 'summary' column)...")
            # Create all tables defined in models (app.py)
            db.create_all()
            print("Database schema created successfully!")
    except Exception as e:
        print(f"Error creating database schema: {e}")
        # Attempt rollback if possible (though less relevant for create_all)
        try:
             db.session.rollback()
        except Exception as rb_e:
             print(f"Rollback attempt failed: {rb_e}")
        sys.exit(1)
        
    # Delete all files in the uploads directory if requested
    if delete_uploads:
        try:
            uploads_dir = os.path.join(os.getcwd(), 'uploads')
            if os.path.exists(uploads_dir):
                print(f"Deleting all files in uploads directory: {uploads_dir}")
                for filename in os.listdir(uploads_dir):
                    file_path = os.path.join(uploads_dir, filename)
                    try:
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                            print(f"Deleted file: {file_path}")
                        elif os.path.isdir(file_path):
                            shutil.rmtree(file_path)
                            print(f"Deleted directory: {file_path}")
                    except Exception as e:
                        print(f"Error deleting {file_path}: {e}")
                print("All files in uploads directory have been deleted.")
            else:
                print(f"Uploads directory not found: {uploads_dir}")
                # Create the directory if it doesn't exist
                os.makedirs(uploads_dir, exist_ok=True)
                print(f"Created uploads directory: {uploads_dir}")
        except Exception as e:
            print(f"Error cleaning uploads directory: {e}")

if __name__ == "__main__":
    print("Attempting to reset the database and clean up all data...")
    reset_database(delete_uploads=True)
    print("Database reset process finished.")
