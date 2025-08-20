#!/bin/bash

# Create directory for the application
sudo systemctl stop transcription

sudo mkdir -p /opt/transcription-app
sudo chown $USER:$USER /opt/transcription-app

# Copy application files
cp app.py /opt/transcription-app/
cp -r templates /opt/transcription-app/
cp requirements.txt /opt/transcription-app/
cp scripts/reset_db.py /opt/transcription-app/
cp scripts/create_admin.py /opt/transcription-app/
cp .env /opt/transcription-app/  # Copy the .env file with API keys

# Add SECRET_KEY to .env file if it doesn't exist
if ! grep -q "SECRET_KEY" /opt/transcription-app/.env; then
    echo "SECRET_KEY=$(openssl rand -hex 32)" >> /opt/transcription-app/.env
    echo "Added SECRET_KEY to .env file"
fi

# Create and activate virtual environment
python3 -m venv /opt/transcription-app/venv
source /opt/transcription-app/venv/bin/activate

# Install requirements
cd /opt/transcription-app
pip install -r requirements.txt

# Create directories for uploads and database with proper permissions
mkdir -p /opt/transcription-app/uploads
mkdir -p /opt/transcription-app/instance
chmod 755 /opt/transcription-app/uploads
chmod 755 /opt/transcription-app/instance

# Initialize or migrate the database
if [ ! -f /opt/transcription-app/instance/transcriptions.db ]; then
    # If database doesn't exist, create it from scratch
    echo "Database doesn't exist. Creating new database..."
    python reset_db.py
else
    # If database exists, migrate it to preserve data
    echo "Database exists. Migrating schema to preserve data..."
    python migrate_db.py
fi

# Set proper ownership for all files
sudo chown -R $USER:$USER /opt/transcription-app

# Create systemd service file
sudo tee /etc/systemd/system/transcription.service << EOF
[Unit]
Description=Transcription Web Application
After=network.target

[Service]
User=$USER
EnvironmentFile=/opt/transcription-app/.env
WorkingDirectory=/opt/transcription-app
Environment="PATH=/opt/transcription-app/venv/bin"
Environment="PYTHONPATH=/opt/transcription-app"
ExecStart=/opt/transcription-app/venv/bin/gunicorn --workers 3 --bind 0.0.0.0:8899 --timeout 600 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd and start service
sudo systemctl daemon-reload
sudo systemctl restart transcription
sudo systemctl disable transcription
sudo systemctl enable transcription

# Check service status
echo "Checking service status..."
sleep 3
sudo systemctl status transcription

echo "Installation complete! The application should be running on port 8899."

# Ask if user wants to create an admin user
read -p "Do you want to create an admin user now? (y/n): " create_admin
if [[ $create_admin == "y" || $create_admin == "Y" ]]; then
    echo "Creating admin user..."
    python create_admin.py
else
    echo "You can create an admin user later by running: python create_admin.py"
fi
