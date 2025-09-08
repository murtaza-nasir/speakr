# Installation Guide

This comprehensive guide covers deploying Speakr for production use, including detailed configuration options, performance tuning, and deployment best practices. While the Quick Start guide gets you running quickly, this guide provides everything you need for a robust production deployment.

## Understanding Speakr's Architecture

Before diving into installation, it's helpful to understand how Speakr works. The application integrates with external APIs for two main purposes: transcription services that convert your audio to text, and text generation services that power features like summaries, titles, and interactive chat. Speakr is designed to be flexible, supporting both cloud-based services like OpenAI and self-hosted solutions running on your own infrastructure.

Speakr uses specific API endpoint formats for these integrations. For transcription, it supports the standard OpenAI Whisper API format using the `/audio/transcriptions` endpoint, which is implemented by OpenAI, OpenRouter, and many self-hosted solutions. Alternatively, for advanced features like speaker diarization, Speakr can connect to ASR webservices that provide an `/asr` endpoint. **Note: Using the ASR endpoint option requires running an additional Docker container** (`onerahmet/openai-whisper-asr-webservice`) alongside Speakr - full setup instructions are provided in the [Running ASR Service for Speaker Diarization](#running-asr-service-for-speaker-diarization) section below. For text generation, Speakr uses the OpenAI Chat Completions API format with the `/chat/completions` endpoint, which is widely supported across different AI providers.

## Prerequisites

For a production deployment, ensure your system meets these requirements. You'll need Docker Engine version 20.10 or later and Docker Compose version 2.0 or later. The system should have at least 4GB of RAM, with 8GB recommended for optimal performance, especially if you're processing longer recordings. Plan for at least 20GB of free disk space to accommodate recordings and transcriptions, though actual requirements will depend on your usage patterns. The server should have a stable internet connection for API calls to transcription and AI services, unless you're running everything locally.

## Choosing Your Deployment Method

You have two main options for deploying Speakr. The first and recommended approach is using the pre-built Docker image from Docker Hub, which requires no source code and gets you running quickly. The second option is building from source, which is useful if you need to modify the code or prefer to build your own images. Both methods use Docker Compose for orchestration and management.

## Standard Installation Using Pre-Built Image

### Step 1: Create Installation Directory

Choose an appropriate location for your Speakr installation. This directory will contain your configuration files and data volumes. For production deployments, using a dedicated directory like `/opt/speakr` or `/srv/speakr` is recommended as it provides a clear separation from user home directories and follows Linux filesystem hierarchy standards.

```bash
mkdir -p /opt/speakr
cd /opt/speakr
```

If you're just testing or running Speakr for personal use, you can create the directory in your home folder instead. The location isn't critical, but keeping everything organized in one place makes management easier.

### Step 2: Create Docker Compose Configuration

Create a `docker-compose.yml` file with the following configuration:

```yaml
services:
  app:
    image: learnedmachine/speakr:latest
    container_name: speakr
    restart: unless-stopped
    ports:
      - "8899:8899"
    env_file:
      - .env
    volumes:
      - ./uploads:/data/uploads
      - ./instance:/data/instance
```

Or download the example configuration:

```bash
wget https://raw.githubusercontent.com/murtaza-nasir/speakr/master/config/docker-compose.example.yml -O docker-compose.yml
```

The restart policy `unless-stopped` ensures Speakr automatically starts after system reboots unless you've explicitly stopped it. The volumes mount local directories for persistent storage of uploads and database files.

### Step 3: Environment Configuration

The environment configuration is where you tell Speakr which AI services to use and how to connect to them. Download the appropriate environment template based on your transcription service choice. This template contains all the configuration variables with helpful comments explaining each setting.

#### For OpenAI Whisper API

If you're using OpenAI's Whisper API or any compatible service, download the Whisper environment template:

```bash
wget https://raw.githubusercontent.com/murtaza-nasir/speakr/master/config/env.whisper.example -O .env
```

Now edit the `.env` file to add your API keys and customize the settings. The configuration is organized into logical sections. First, configure the text generation model that powers summaries, titles, and chat features. OpenRouter is recommended here because it provides access to multiple AI models at competitive prices, but you can use any OpenAI-compatible service:

```bash
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL_API_KEY=sk-or-v1-your-key-here
TEXT_MODEL_NAME=openai/gpt-4o-mini
```

If you prefer to use OpenAI directly for text generation, simply change the base URL to `https://api.openai.com/v1` and use your OpenAI API key. You can also use local models through Ollama or LM Studio by pointing to `http://localhost:11434/v1` or similar.

Next, configure the transcription service. This is what converts your audio files into text:

```bash
TRANSCRIPTION_BASE_URL=https://api.openai.com/v1
TRANSCRIPTION_API_KEY=sk-your-openai-key-here
WHISPER_MODEL=whisper-1
```

#### For Custom ASR Endpoint with Speaker Diarization

If you want speaker diarization to identify who's speaking in your recordings, you'll need to use an ASR webservice endpoint. **This requires running an additional Docker container** (`onerahmet/openai-whisper-asr-webservice`) alongside Speakr, but provides powerful features for meeting transcriptions and multi-speaker recordings.

> **Important:** Before proceeding with this configuration, you'll need to set up the ASR service container. See [Running ASR Service for Speaker Diarization](#running-asr-service-for-speaker-diarization) for complete instructions on deploying both containers together or separately.

Download the ASR configuration template:

```bash
wget https://raw.githubusercontent.com/murtaza-nasir/speakr/master/config/env.asr.example -O .env
```

The ASR configuration enables the custom endpoint and tells Speakr where to find it:

```bash
USE_ASR_ENDPOINT=true
ASR_BASE_URL=http://your-asr-service:9000
```

The ASR_BASE_URL depends on your deployment architecture. If you're running the ASR service in the same Docker Compose stack as Speakr, use the service name from your docker-compose.yml file, like `http://whisper-asr:9000`. This uses Docker's internal networking for communication. If the ASR service is running elsewhere, use its full URL with the appropriate IP address or domain name.

Speaker diarization is automatically enabled when using ASR endpoints. The system will identify different speakers in your recordings and label them as Speaker 1, Speaker 2, and so on. You can optionally override the default speaker detection settings by uncommenting and adjusting ASR_MIN_SPEAKERS and ASR_MAX_SPEAKERS in your environment file.

### Step 4: Configure System Settings

One of Speakr's conveniences is automatic admin account creation. Instead of going through a registration process, you define the admin credentials in your environment file, and Speakr creates the account automatically on first startup. This ensures you can log in immediately without any additional setup steps:

```bash
ADMIN_USERNAME=admin
ADMIN_EMAIL=admin@your-domain.com
ADMIN_PASSWORD=your-secure-password-here
```

Choose a strong password for this account as it has full system access, including the ability to manage users and view all recordings. The admin account is special and cannot be created through the regular registration process, only through these environment variables.

Next, configure how the application behaves. These settings control user access and system operation:

```bash
ALLOW_REGISTRATION=false
TIMEZONE="America/New_York"
LOG_LEVEL="INFO"
```

Setting `ALLOW_REGISTRATION=false` means only the admin can create new user accounts, which is recommended for private installations where you want to control access. If you're running Speakr for a team or family, this prevents random people from creating accounts. The timezone setting affects how dates and times are displayed throughout the interface, so set it to your local timezone for convenience. The log level controls how much information Speakr writes to its logs. Use `INFO` during initial setup and testing to see what's happening, then switch to `ERROR` for production to reduce log volume and improve performance.

### Step 5: Configure Advanced Features

#### Large File Handling

One of Speakr's most useful features is automatic handling of large audio files. Many transcription APIs have file size limits, with OpenAI's 25MB limit being a common constraint. Rather than forcing you to manually split files, Speakr handles this automatically through intelligent chunking:

```bash
ENABLE_CHUNKING=true
CHUNK_LIMIT=20MB
CHUNK_OVERLAP_SECONDS=3
```

When chunking is enabled, Speakr automatically detects when a file exceeds the configured limit and splits it into smaller pieces. Each chunk is processed separately, and the transcriptions are seamlessly merged back together. The overlap setting ensures that no words are lost at chunk boundaries, which is especially important for continuous speech. The chunk limit can be specified as a file size like `20MB` or as a duration like `20m` for 20 minutes, depending on your API's limitations.

This feature only applies when using the standard Whisper API method. If you're using an ASR endpoint, chunking is not needed as these services typically handle large files natively.

#### Inquire Mode for Semantic Search

Inquire Mode transforms Speakr from a simple transcription tool into a knowledge base of all your recordings. When enabled, you can search across all your transcriptions using natural language questions:

```bash
ENABLE_INQUIRE_MODE=true
```

With Inquire Mode active, Speakr creates embeddings of your transcriptions that enable semantic search. This means you can ask questions like "When did we discuss the marketing budget?" and find relevant recordings even if those exact words weren't used. The feature requires additional processing during transcription but provides powerful search capabilities that become more valuable as your recording library grows.

#### Automated File Processing

The automated file processing feature, sometimes called the "black hole" directory, monitors a designated folder for new audio files and automatically processes them without manual intervention. This is perfect for integrating with recording devices, automated workflows, or batch processing scenarios:

```bash
ENABLE_AUTO_PROCESSING=true
AUTO_PROCESS_MODE=admin_only
AUTO_PROCESS_WATCH_DIR=/data/auto-process
AUTO_PROCESS_CHECK_INTERVAL=30
```

When enabled, Speakr checks the watch directory every 30 seconds for new audio files. Any files found are automatically moved to the uploads directory and processed using your configured transcription settings. The `admin_only` mode assigns all processed files to the admin user, but you can also configure it for multi-user scenarios with separate directories for each user.

To use this feature, you'll need to mount an additional volume in your Docker Compose configuration, which we'll cover in the next steps.

### Step 6: Set Up Data Directories

Speakr needs local directories to store your data persistently. These directories are mounted as Docker volumes, ensuring your recordings and database survive container updates and restarts:

```bash
mkdir -p uploads instance
chmod 755 uploads instance
```

The `uploads` directory stores all your audio files and their transcriptions, organized by user. The `instance` directory contains the SQLite database that tracks all your recordings, users, and settings. Setting the permissions to 755 ensures the Docker container can read and write to these directories while maintaining reasonable security.

If you're using the automated file processing feature, create that directory as well:

```bash
mkdir -p auto-process
chmod 755 auto-process
```

### Step 7: Launch Speakr

With everything configured, you're ready to start Speakr. The `-d` flag runs the container in detached mode, meaning it continues running in the background:

```bash
docker compose up -d
```

The first time you run this command, Docker will download the Speakr image from Docker Hub. This image is approximately 3GB and contains all the dependencies needed to run Speakr, including FFmpeg for audio processing and various Python libraries. The download time depends on your internet connection speed.

Monitor the startup process to ensure everything is working correctly:

```bash
docker compose logs -f app
```

Watch the logs for any error messages. You should see messages about database initialization, admin user creation, and finally a message indicating the Flask application is running on port 8899. Press Ctrl+C to stop following the logs (this won't stop the container, just the log viewing).

### Step 8: Verify Installation

Open your web browser and navigate to `http://your-server:8899`, replacing `your-server` with your actual server address or `localhost` if you're running locally. You should see the Speakr login page with its distinctive gradient design.

Log in using the admin credentials you configured in the environment file. If login fails, check your Docker logs to ensure the admin user was created successfully. Sometimes typos in the environment file can cause issues.

Once logged in, test the installation by creating a test recording or uploading a sample audio file. The recording interface should show options for microphone, system audio, or both. Try uploading a small audio file first to verify that your API keys are working correctly. The transcription process should complete within a few moments for short files, and you should see the transcribed text appear along with an AI-generated summary.

If transcription fails, check the Docker logs for API authentication errors or connection issues. Common problems include incorrect API keys, insufficient API credits, or network connectivity issues.

## Advanced Deployment Scenarios

### Running ASR Service for Speaker Diarization

If you need speaker diarization to identify different speakers in your recordings, you'll need to run an ASR service alongside Speakr. This involves deploying an additional Docker container (`onerahmet/openai-whisper-asr-webservice`) that provides the ASR endpoint. The recommended approach is running both containers in the same Docker Compose stack for simplified networking and management.

First, you'll need a Hugging Face token to access the speaker diarization models. Create an account at Hugging Face, generate an access token, and importantly, visit the model pages for pyannote/segmentation-3.0 and pyannote/speaker-diarization-3.1 to accept their terms. These are gated models that require explicit approval.

Here's a complete Docker Compose configuration that includes both services:

```yaml
services:
  whisper-asr:
    image: onerahmet/openai-whisper-asr-webservice:latest-gpu
    container_name: whisper-asr-webservice
    ports:
      - "9000:9000"
    environment:
      - ASR_MODEL=distil-large-v3
      - ASR_COMPUTE_TYPE=int8
      - ASR_ENGINE=whisperx
      - HF_TOKEN=your_huggingface_token_here
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: [gpu]
              device_ids: ["0"]
    restart: unless-stopped
    networks:
      - speakr-network

  speakr:
    image: learnedmachine/speakr:latest
    container_name: speakr
    restart: unless-stopped
    ports:
      - "8899:8899"
    env_file:
      - .env
    volumes:
      - ./uploads:/data/uploads
      - ./instance:/data/instance
    depends_on:
      - whisper-asr
    networks:
      - speakr-network

networks:
  speakr-network:
    driver: bridge
```

When running both services in the same Docker Compose file, containers communicate using service names. In your `.env` file, set `ASR_BASE_URL=http://whisper-asr:9000` using the service name, not localhost or an IP address. This is a common source of confusion but is how Docker networking works.

#### Running Services in Separate Docker Compose Files

If you prefer to manage the services independently or are adding the ASR service to an existing Speakr installation, you can run them in separate Docker Compose files. This approach gives you more flexibility and works whether the services are on the same machine or different machines.

##### Option 1: Same Machine with Shared Network

If both services run on the same machine, you can use Docker's internal networking for communication:

First, create a shared Docker network:

```bash
docker network create speakr-network
```

Create `docker-compose.asr.yml` for the ASR service:

```yaml
services:
  whisper-asr:
    image: onerahmet/openai-whisper-asr-webservice:latest-gpu
    container_name: whisper-asr-webservice
    ports:
      - "9000:9000"
    environment:
      - ASR_MODEL=distil-large-v3
      - ASR_COMPUTE_TYPE=int8
      - ASR_ENGINE=whisperx
      - HF_TOKEN=your_huggingface_token_here
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: [gpu]
              device_ids: ["0"]
    restart: unless-stopped
    networks:
      - speakr-network

networks:
  speakr-network:
    external: true
```

Update your Speakr `docker-compose.yml` to use the shared network:

```yaml
services:
  app:
    image: learnedmachine/speakr:latest
    container_name: speakr
    restart: unless-stopped
    ports:
      - "8899:8899"
    env_file:
      - .env
    volumes:
      - ./uploads:/data/uploads
      - ./instance:/data/instance
    networks:
      - speakr-network

networks:
  speakr-network:
    external: true
```

In your `.env` file, use the container name:
```bash
ASR_BASE_URL=http://whisper-asr-webservice:9000
```

##### Option 2: Separate Machines

When running on different machines, you don't need the shared network. Each service runs independently and communicates over the network using IP addresses or hostnames.

On the ASR server, create `docker-compose.asr.yml`:

```yaml
services:
  whisper-asr:
    image: onerahmet/openai-whisper-asr-webservice:latest-gpu
    container_name: whisper-asr-webservice
    ports:
      - "9000:9000"  # Exposed to the network
    environment:
      - ASR_MODEL=distil-large-v3
      - ASR_COMPUTE_TYPE=int8
      - ASR_ENGINE=whisperx
      - HF_TOKEN=your_huggingface_token_here
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: [gpu]
              device_ids: ["0"]
    restart: unless-stopped
```

On the Speakr server, use the standard `docker-compose.yml`:

```yaml
services:
  app:
    image: learnedmachine/speakr:latest
    container_name: speakr
    restart: unless-stopped
    ports:
      - "8899:8899"
    env_file:
      - .env
    volumes:
      - ./uploads:/data/uploads
      - ./instance:/data/instance
```

In your Speakr `.env` file, use the ASR server's IP address or hostname:
```bash
# Using IP address
ASR_BASE_URL=http://192.168.1.100:9000

# Or using hostname
ASR_BASE_URL=http://asr-server.local:9000
```

Start both services on their respective machines:

```bash
# On ASR server
docker compose -f docker-compose.asr.yml up -d

# On Speakr server
docker compose up -d
```

Make sure port 9000 is accessible between the machines (check firewall rules if needed).

## Production Considerations

### Using a Reverse Proxy for SSL

For production deployments, running Speakr behind a reverse proxy is essential for security and enabling all features. The browser recording feature, particularly system audio capture, requires HTTPS to work due to browser security restrictions. A reverse proxy handles SSL termination, meaning it manages the HTTPS certificates while communicating with Speakr over HTTP internally.

Here's a complete nginx configuration for Speakr:

```nginx
server {
    listen 443 ssl http2;
    server_name speakr.yourdomain.com;

    ssl_certificate /path/to/certificate.crt;
    ssl_certificate_key /path/to/private.key;

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;

    location / {
        proxy_pass http://localhost:8899;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket support for live features
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # Timeouts for large file uploads
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name speakr.yourdomain.com;
    return 301 https://$server_name$request_uri;
}
```

The WebSocket configuration is important for real-time features in Speakr. The timeout settings ensure large file uploads don't get interrupted. You can obtain free SSL certificates from Let's Encrypt using Certbot, making HTTPS accessible for everyone.

### Backup Strategy

Regular backups are essential for production deployments. Your Speakr data consists of three critical components that need backing up: the SQLite database in the `instance` directory, the audio files and transcriptions in the `uploads` directory, and your configuration in the `.env` file.

Create a backup script that captures all three components:

```bash
#!/bin/bash
BACKUP_DIR="/backup/speakr"
DATE=$(date +%Y%m%d_%H%M%S)

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# Create the backup
tar czf "$BACKUP_DIR/speakr_backup_$DATE.tar.gz" \
    /opt/speakr/instance \
    /opt/speakr/uploads \
    /opt/speakr/.env

# Optional: Keep only last 30 days of backups
find "$BACKUP_DIR" -name "speakr_backup_*.tar.gz" -mtime +30 -delete

echo "Backup completed: speakr_backup_$DATE.tar.gz"
```

Make the script executable and schedule it with cron for automated daily backups:

```bash
chmod +x /opt/speakr/backup.sh
crontab -e
# Add this line for daily backups at 2 AM:
0 2 * * * /opt/speakr/backup.sh
```

For critical deployments, consider copying backups to remote storage or cloud services for additional redundancy. The compressed backup size is typically much smaller than the original data, as audio files compress well.

### Monitoring and Maintenance

Proactive monitoring helps prevent issues before they impact users. Audio files can consume significant storage over time, especially if you're recording long meetings regularly. Set up disk space monitoring with alerts when usage exceeds 80%. A simple monitoring approach uses cron with df:

```bash
#!/bin/bash
USAGE=$(df /opt/speakr | tail -1 | awk '{print $5}' | sed 's/%//')
if [ $USAGE -gt 80 ]; then
    echo "Warning: Speakr disk usage is at ${USAGE}%" | mail -s "Speakr Disk Alert" admin@example.com
fi
```

Monitor the Docker container health and logs regularly. You can use Docker's built-in health check feature or external monitoring tools. Check for patterns like repeated API failures, authentication errors, or processing timeouts. Also track your API usage and costs with your transcription service provider, as costs can add up with heavy usage.

### Security Hardening

Production deployments require additional security measures beyond the default configuration. Start by ensuring strong passwords for all accounts, especially the admin account. Never use default or simple passwords in production.

Restrict network access using firewall rules. If Speakr is only used internally, limit access to your organization's IP ranges:

```bash
# Example using ufw
ufw allow from 192.168.1.0/24 to any port 8899
ufw deny 8899
```

Implement rate limiting at the reverse proxy level to prevent abuse and API exhaustion. In nginx, you can add:

```nginx
limit_req_zone $binary_remote_addr zone=speakr:10m rate=10r/s;
limit_req zone=speakr burst=20;
```

Keep the Docker image updated with the latest security patches. Check for updates regularly and plan maintenance windows for updates. Always backup before updating, and test updates in a staging environment first if possible.

## Updating Speakr

Keeping Speakr updated ensures you have the latest features and security patches. The update process is straightforward but should be done carefully to avoid data loss.

First, always create a backup before updating:

```bash
# Create a backup
tar czf speakr_backup_before_update.tar.gz uploads/ instance/ .env

# Pull the latest image
docker compose pull

# Stop the current container
docker compose down

# Start with the new image
docker compose up -d

# Check the logs to ensure successful startup
docker compose logs -f app
```

The update process preserves all your data since it's stored in mounted volumes outside the container. However, checking the release notes is important as some updates might require configuration changes or have breaking changes that need attention.

If an update causes issues, you can rollback by specifying the previous version in your docker-compose.yml file:

```yaml
image: learnedmachine/speakr:v1.2.3  # Replace with your previous version
```

## Troubleshooting Common Issues

### Container Won't Start

When the container fails to start, the logs usually tell you exactly what's wrong. Check them first:

```bash
docker compose logs app
```

Common startup issues include missing or malformed `.env` files. Ensure your `.env` file exists and has proper syntax. Each line should be `KEY=value` with no spaces around the equals sign. Comments start with `#`.

Port conflicts are another common issue. Check if port 8899 is already in use:

```bash
netstat -tulpn | grep 8899
# Or on macOS:
lsof -i :8899
```

If the port is in use, either stop the conflicting service or change Speakr's port in docker-compose.yml.

### Transcription Failures

Transcription failures usually stem from API configuration issues. Check the Docker logs for specific error messages:

```bash
docker compose logs app | grep -i error
```

Common transcription issues include incorrect API keys, which show as authentication errors in the logs. Double-check your keys in the `.env` file and ensure they're for the correct service. Insufficient API credits will show as quota or payment errors. Check your account balance with your API provider. Network connectivity issues appear as connection timeouts or DNS resolution failures.

For ASR endpoints, verify the service is running and accessible:

```bash
# Test ASR endpoint connectivity
curl http://your-asr-service:9000/docs
```

If using Docker networking with service names, remember that containers must be on the same network to communicate.

### Performance Issues

Slow performance can have multiple causes. Start by checking system resources:

```bash
# Check memory usage
free -h

# Check disk I/O
iotop

# Check Docker resource usage
docker stats speakr
```

If memory is constrained, consider adding swap space or upgrading your server. For disk I/O issues, ensure you're using SSD storage for the uploads and instance directories. Traditional hard drives can significantly slow down operations, especially with multiple concurrent users.

For large file processing, ensure chunking is properly configured. Without chunking, large files might timeout or fail completely. The chunk size should be slightly below your API's limit to account for encoding overhead.

If you're seeing slow transcription with many concurrent users, you might be hitting API rate limits. Check your API provider's documentation for rate limits and consider upgrading your plan if needed.

### Browser Recording Issues

If browser recording isn't working, especially system audio, the most common cause is using HTTP instead of HTTPS. Browsers require secure connections for audio capture due to privacy concerns. Either set up SSL with a reverse proxy or, for local development only, modify your browser's security settings to treat your local URL as secure.

In Chrome, navigate to `chrome://flags`, search for "insecure origins", and add your URL to the list. Remember this reduces security and should only be used for development.

## Building from Source

If you need to modify Speakr's code or prefer building your own images, you can build from source. This requires cloning the repository and using Docker's build capability.

First, clone the repository and navigate to it:

```bash
git clone https://github.com/murtaza-nasir/speakr.git
cd speakr
```

Modify the docker-compose.yml to build locally instead of using the pre-built image:

```yaml
services:
  app:
    build: .  # Build from current directory
    image: speakr:custom  # Tag for your custom build
    container_name: speakr
    restart: unless-stopped
    ports:
      - "8899:8899"
    env_file:
      - .env
    volumes:
      - ./uploads:/data/uploads
      - ./instance:/data/instance
```

Build and start your custom version:

```bash
docker compose up -d --build
```

The `--build` flag forces Docker to rebuild the image even if one exists. This is useful when you've made code changes and want to test them.

## Performance Optimization

For high-volume deployments or when processing many large files, optimization becomes important. Start with model selection if using ASR. The distil-large-v3 model offers an excellent balance of speed and accuracy. For English-only content, use the `.en` variants which are faster and more accurate for English.

Optimize Docker resource allocation for your workload:

```yaml
services:
  app:
    image: learnedmachine/speakr:latest
    deploy:
      resources:
        limits:
          memory: 8G
          cpus: '4'
        reservations:
          memory: 4G
          cpus: '2'
```

This ensures Speakr has enough resources while preventing it from consuming everything on shared servers.

For storage performance, use SSD drives for the Docker volumes. The database benefits significantly from fast random I/O, and large audio file processing is much faster with SSDs. If using network storage, ensure low latency connections.

---

Next: [User Guide](../user-guide/index.md) to learn how to use all of Speakr's features