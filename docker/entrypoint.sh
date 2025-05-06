#!/bin/bash
set -euo pipefail

# Check if admin user exists
if [ ! -f /opt/transcription-app/instance/admin_created ]; then
  echo "No admin user found. Creating..."
  python create_admin.py
  touch /opt/transcription-app/instance/admin_created
else
  echo "Admin user already exists."
fi

# Run the Flask application using Gunicorn
gunicorn --workers 3 --bind 0.0.0.0:8899 --timeout 600 app:app
