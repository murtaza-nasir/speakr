#!/bin/bash
set -e

# Create necessary directories
mkdir -p /data/uploads /data/instance
chmod 755 /data/uploads /data/instance

# Initialize the database if it doesn't exist
if [ ! -f /data/instance/transcriptions.db ]; then
    echo "Database doesn't exist. Creating new database..."
    python -c "from app import app, db; app.app_context().push(); db.create_all()"
    echo "Database created successfully."
    
    # Check if we need to create an admin user
    if [ -n "$ADMIN_USERNAME" ] && [ -n "$ADMIN_EMAIL" ] && [ -n "$ADMIN_PASSWORD" ]; then
        echo "Creating admin user using environment variables..."
        python docker_create_admin.py
    fi
else
    echo "Database exists. Checking for schema updates..."
    python -c "from app import app; app.app_context().push()"
fi

# Start the application
exec "$@"
