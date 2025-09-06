FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create directory structure for vendor files
RUN mkdir -p /app/static/vendor

# Copy only the scripts needed for downloading dependencies
COPY scripts/download_offline_deps.py scripts/

# Install requests and download vendor dependencies BEFORE copying all code
# This allows better Docker layer caching - vendor deps won't re-download on code changes
RUN pip install --no-cache-dir requests && \
    python scripts/download_offline_deps.py && \
    echo "✓ Vendor dependencies downloaded successfully"

# Now copy the rest of the application code
COPY . .

# Create necessary directories
RUN mkdir -p /data/uploads /data/instance
RUN chmod 755 /data/uploads /data/instance

# Set environment variables
ENV FLASK_APP=app.py
ENV SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
ENV UPLOAD_FOLDER=/data/uploads

# Add entrypoint script
COPY scripts/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Expose the port
EXPOSE 8899

# Set entrypoint and default command
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--workers", "3", "--bind", "0.0.0.0:8899", "--timeout", "600", "app:app"]
