# Speakr

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0) [![Docker Build and Publish](https://github.com/murtaza-nasir/speakr/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/murtaza-nasir/speakr/actions/workflows/docker-publish.yml) This project is dual-licensed. See the [License](#license) section for details.

Speakr is a personal, self-hosted web application designed for transcribing audio recordings (like meetings), generating concise summaries and titles, and interacting with the content through a chat interface. Keep all your meeting notes and insights securely on your own server.

## Screenshots
![image](https://github.com/user-attachments/assets/788aebc7-86bd-4024-be4d-c6273d61c765)
![image](https://github.com/user-attachments/assets/a1d471fa-8b52-4f9e-962d-19cbd0955a3b)
![image](https://github.com/user-attachments/assets/637bfb92-bbfc-4113-93d2-c7d051c970af)

## Features

**Core Functionality:**

* **Audio Upload:** Upload audio files (MP3, WAV, M4A, etc.) via drag-and-drop or file selection.
* **Background Processing:** Transcription and summarization happen in the background without blocking the UI.
* **Transcription:** Uses OpenAI-compatible Speech-to-Text (STT) APIs (configurable, e.g., self-hosted Whisper).
* **AI Summarization & Titling:** Generates concise titles and summaries using configurable LLMs via OpenAI-compatible APIs (like OpenRouter).
* **Interactive Chat:** Ask questions and interact with the transcription content using an AI model.
* **Search, Inbox & Highlight:** For highlighting and easy processing
* **Metadata Editing:** Edit titles, participants, meeting dates, summaries, and notes associated with recordings.

**User Features:**

* **Authentication:** Secure user registration and login system.
* **Account Management:** Users can change their passwords.
* **Recording Gallery:** View, manage, and access all personal recordings.
* **Dark Mode:** Switch between light and dark themes.

**Admin Features:**

* **Admin Dashboard:** Central place for administration tasks (`/admin`).
* **User Management:** Add, edit, delete users, and grant/revoke admin privileges.
* **System Statistics:** View overall usage statistics (total users, recordings, storage, etc.).

## Technology Stack

* **Backend:** Python 3, Flask
* **Database:** SQLAlchemy ORM with SQLite (default)
* **WSGI Server:** Gunicorn
* **AI Integration:** OpenAI Python library (compatible with various endpoints like OpenAI, OpenRouter, or local servers)
* **Authentication:** Flask-Login, Flask-Bcrypt, Flask-WTF
* **Frontend:** Jinja2 Templates, Tailwind CSS, Vue.js (for interactive components), Font Awesome
* **Deployment:** 
  * Bash script (`setup.sh`) for Linux/Systemd environments
  * Docker with automated builds via GitHub Actions

## Prerequisites

* Python 3.8+
* `pip` (Python package installer)
* `venv` (Python virtual environment tool - usually included with Python)
* Access to OpenAI-compatible API endpoints for:
    * Speech-to-Text (e.g., Whisper - can be self-hosted)
    * Language Model (e.g., via OpenRouter, OpenAI, or other compatible service)
* API Keys for the chosen endpoints.
* (For Deployment) A Linux server with `sudo` access and `systemd`.

## Setup Instructions

**IMPORTANT NOTE:** Currently, only the Docker installation method is working. The Local Development and Deployment methods are not functional at this time.

Choose either **Docker** (recommended and currently the only working method), **Local Development**, or **Deployment**.

### 1. Docker Installation

The easiest way to deploy Speakr is using Docker and docker compose.

#### Option A: Using Pre-built Docker Images

1. **Create a docker-compose.yml file:**
   ```yaml
    services:
     app:
       image: learnedmachine/speakr:latest
       container_name: speakr
       restart: unless-stopped
       ports:
         - "8899:8899"
       volumes:
         - ./uploads:/data/uploads
         - ./instance:/data/instance
       environment:
         # Database and upload folder configuration
         - SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
         - UPLOAD_FOLDER=/data/uploads
         
         # Text model configuration
         - TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
         - TEXT_MODEL_API_KEY=your_text_model_api_key_here
         - TEXT_MODEL_NAME=openai/gpt-4o-mini
         
         # Whisper model configuration
         - TRANSCRIPTION_BASE_URL=http://your_local_api_url:port/v1/
         - TRANSCRIPTION_API_KEY=your_transcription_api_key_here
         - WHISPER_MODEL=Systran/faster-distil-whisper-large-v3
         - TRANSCRIPTION_LANGUAGE= # Optional: Specify ISO-639-1 language code (e.g., en, es, fr, zh for Chinese). Leave empty for auto-detection.
         - OUTPUT_LANGUAGE= # Optional: Specify desired language for LLM outputs (e.g., English, Spanish, Chinese). Leave empty for English default.
         
         # Application settings
         - ALLOW_REGISTRATION=false
   ```

   Alternatively, you can copy the example file:
   ```bash
   cp docker-compose.example.yml docker-compose.yml
   ```
   Then edit the file to update the environment variables with your actual values. **Make sure to change the mount points to the correct paths**.

2. **Start the container:**
   ```bash
   docker compose up -d
   ```

#### Option A.1: Deploying with Portainer

If you're using Portainer to manage your Docker containers:

1. **In Portainer:**
   - Go to Stacks → Add stack
   - Name your stack (e.g., "speakr")
   - Upload the `docker-compose.example.yml` file or paste its contents
   - Edit the environment variables with your actual values
   - Click "Deploy the stack"

2. **Important Notes for Portainer:**
   - Make sure to update all environment variables with your actual values
   - For volume persistence, you can either:
     - Use named volumes as shown in the example file
     - Or modify the volumes to use bind mounts to specific directories on your host

#### Option B: Building Locally

1. **Clone the Repository:**
   ```bash
   git clone https://github.com/murtaza-nasir/speakr.git
   cd speakr
   ```

2. **Configure Environment Variables:**
   * Copy the example docker-compose file:
     ```bash
     cp docker-compose.example.yml docker-compose.yml
     ```
   * Edit the `docker-compose.yml` file to update the environment variables with your actual API keys and settings as described in the Configuration section.

3. **Build and Start the Container:**
   ```bash
   docker compose up -d
   ```

4. **Create an Admin User:**
   
   You can create an admin user in one of the following ways:
   
   * **Option A: Using environment variables:**
     
     Create a `docker-compose.override.yml` file:
     ```bash
     cp docker-compose.override.yml.example docker-compose.override.yml
     ```
     
     Edit the file to uncomment and set the admin credentials:
     ```yaml
     services:
       app:
         environment:
           - ADMIN_USERNAME=admin
           - ADMIN_EMAIL=admin@example.com
           - ADMIN_PASSWORD=securepassword
     ```
     
     Then restart the container:
     ```bash
     docker compose down
     docker compose up -d
     ```
   
   * **Option B: Using the create_admin.py script:**
     ```bash
     docker compose exec app python create_admin.py
     ```
     Follow the interactive prompts to create an admin user.

5. **Access Speakr:** Open your web browser and navigate to `http://localhost:8899`.

#### Customizing Data Storage

By default, all data (database and uploaded audio files) is stored in Docker volumes. To use custom directories on your host system:

1. Create a `docker-compose.override.yml` file (if you haven't already):
   ```bash
   cp docker-compose.override.yml.example docker-compose.override.yml
   ```

2. Edit `docker-compose.override.yml` and uncomment the volume mappings:
   ```yaml
   services:
     app:
       volumes:
         - /path/to/your/uploads:/data/uploads
         - /path/to/your/database:/data/instance
   ```

3. Replace `/path/to/your/uploads` and `/path/to/your/database` with actual paths on your system.

4. Restart the container:
   ```bash
   docker compose down
   docker compose up -d
   ```

#### Updating the Docker Installation

To update the application to a newer version:

1. Pull the latest code:
   ```bash
   git pull
   ```

2. Rebuild and restart the container:
   ```bash
   docker compose down
   docker compose build
   docker compose up -d
   ```

Your data will be preserved as it's stored in Docker volumes or your custom mapped directories.

### 2. Local Development

**NOTE: This method is currently not working. Please use the Docker installation method above.**

Follow these steps to run Speakr on your local machine for development or testing.

1.  **Clone the Repository:**
    ```bash
    git clone https://github.com/murtaza-nasir/speakr.git
    cd speakr
    ```

2.  **Create and Activate Virtual Environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    # On Windows use: venv\Scripts\activate
    ```

3.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure Environment Variables:**
    * Copy the example environment file `.env.example` or create a new file named `.env` in the project root.
    * Add the following variables, replacing placeholder values with your actual keys and endpoints:

        ```dotenv
        # --- Required for Summaries/Chat ---
        # (Use OpenRouter or another OpenAI-compatible Chat API)
        TEXT_MODEL_API_KEY=sk-or-v1-... # Your OpenRouter or compatible API key
        TRANSCRIPTION_BASE_URL="https://openrouter.ai/api/v1" # Or your chat model endpoint
        # Recommended Models: openai/gpt-4o-mini, google/gemini-flash-1.5, etc.
        TEXT_MODEL_MODEL_NAME="openai/gpt-4o-mini"

        # --- Required for Transcription ---
        # (Use OpenAI Whisper API or a compatible local/remote endpoint)
        TRANSCRIPTION_API_KEY="cant-be-empty" # Use your OpenAI key OR often "none", "NA", "cant-be-empty" for local endpoints
        TRANSCRIPTION_BASE_URL="http://YOUR_LOCAL_WHISPER_IP:PORT/v1/" # Your transcription endpoint URL
        # Set the specific model name your transcription endpoint uses (if needed by API)
        WHISPER_MODEL="Systran/faster-distil-whisper-large-v3" # Or the model your endpoint expects

        # --- Flask Specific ---
        # A strong, random secret key is crucial for session security.
        # Generate one using: python -c 'import secrets; print(secrets.token_hex(32))'
        SECRET_KEY="YOUR_VERY_STRONG_RANDOM_SECRET_KEY"

        # --- Optional ---
        # Set to 'false' to disable new user registrations
        ALLOW_REGISTRATION="true"
        ```

5.  **Database Setup & Migrations:**
    * The application uses SQLite by default, stored in `instance/transcriptions.db`.
    * If you ever need to start completely fresh, you can use `python reset_db.py`, but **be careful as this deletes all data**)

6.  **Create an Admin User:**
    * Run the interactive script to create your first user with admin privileges:
        ```bash
        python create_admin.py
        ```
    * Follow the prompts to enter a username, email, and password.

7.  **Run the Application:**
    * **Option A (Flask Development Server - recommended for local dev):**
        ```bash
        flask run --host=0.0.0.0 --port=8899
        # Or (if flask command not found, ensure venv is active):
        # python app.py
        ```
    * **Option B (Gunicorn - closer to production):**
        ```bash
        gunicorn --workers 3 --bind 0.0.0.0:8899 --timeout 600 app:app
        ```

8.  **Access Speakr:** Open your web browser and navigate to `http://localhost:8899` (or your server's IP address if running remotely).

### 3. Deployment (Linux with Systemd)

**NOTE: This method is currently not working. Please use the Docker installation method above.**

The `deployment/setup.sh` script automates the setup process on a Linux server using `systemd`.

**Warning:** Review the script carefully before running it, especially the paths and user (`$USER`) it assumes.

1.  **Copy Project:** Ensure all project files (including the `deployment` directory and your configured `.env` file) are on the target server.
2.  **Make Script Executable:**
    ```bash
    chmod +x deployment/setup.sh
    ```
3.  **Run Setup Script:**
    ```bash
    sudo bash deployment/setup.sh
    ```
    * This script will:
        * Create `/opt/transcription-app`.
        * Copy necessary files.
        * Set up Python virtual environment and install dependencies.
        * Ensure `.env` exists and has a `SECRET_KEY`.
        * Create `uploads` and `instance` directories.
        * Initialize or migrate the database (`reset_db.py` on first run, `migrate_db.py` otherwise).
        * Set file ownership.
        * Create and enable a `systemd` service (`transcription.service`) to run the app with Gunicorn.
        * Start the service.
        * Prompt you to create an admin user.

4.  **Service Management:**
    * **Check Status:** `sudo systemctl status transcription.service`
    * **Stop Service:** `sudo systemctl stop transcription.service`
    * **Start Service:** `sudo systemctl start transcription.service`
    * **Restart Service:** `sudo systemctl restart transcription.service`
    * **View Logs:** `sudo journalctl -u transcription.service -f` (follow logs)
    * **View Recent Logs:** `sudo journalctl -u transcription.service -n 100 --no-pager`

5.  **Access Speakr:** Open your web browser and navigate to `http://YOUR_SERVER_IP:8899`.

## Configuration

Configuration is primarily handled through environment variables in the `docker-compose.yml` file for Docker installations, or through the `.env` file for other installation methods (though note that the non-Docker installation methods are currently not working).

**Key Variables:**

* `TEXT_MODEL_BASE_URL`: **Required.** The base URL for the chat/summarization API. Defaults to OpenRouter's URL.
* `TEXT_MODEL_API_KEY`: **Required.** Your API key for the chat/summarization model endpoint (e.g., OpenRouter API Key).
* `TEXT_MODEL_NAME`: **Required.** The specific model to use for chat/summarization. **Important:** Choose a model that supports structured outputs for optimal performance. OpenAI models (GPT-4o and later) and Fireworks models are recommended. See [OpenRouter's structured outputs documentation](https://openrouter.ai/docs/features/structured-outputs) for more details.
* `TRANSCRIPTION_BASE_URL`: **Required.** The base URL for your transcription API endpoint (e.g., `http://localhost:8787/v1/`).
* `TRANSCRIPTION_API_KEY`: **Required.** Your API key for the transcription endpoint. For local endpoints, this might be a specific string like "none" or "NA". Check your endpoint's documentation.
* `WHISPER_MODEL`: *Optional.* The specific model name your transcription endpoint uses/expects (e.g., `Systran/faster-distil-whisper-large-v3`). Check your endpoint's requirements.
* `TRANSCRIPTION_LANGUAGE`: *Optional.* The language of the input audio. Supplying the input language in ISO-639-1 format (e.g., `en` for English, `es` for Spanish) will improve transcription accuracy and latency. If not set, the transcription service will attempt to auto-detect the language.
* `OUTPUT_LANGUAGE`: *Optional.* The desired language for the generated titles, summaries, and chat responses (e.g., "English", "Spanish", "Chinese"). If not set, outputs will typically be in the language of the transcription or English. Ensure your chosen `TEXT_MODEL_NAME` supports the desired output language.
* `SECRET_KEY`: **Required.** A long, random string used by Flask for session security. The `setup.sh` script generates one if it's missing.
* `ALLOW_REGISTRATION`: *Optional.* Set to `false` to prevent new users from registering via the web UI. Defaults to `true`.

## Usage

1.  **Register/Login:** Access the web application via your browser. Register a new account (if enabled) or log in.
2.  **Upload:** Go to "New Recording" or drag-and-drop audio files onto the page. Upload progress and subsequent processing status will appear in the bottom-left popup.
3.  **View Recordings:** The main "Gallery" view lists your recordings, grouped by date. Click on a recording to view its details.
4.  **Interact:**
    * Listen to the audio using the player.
    * Read the transcription.
    * Review the AI-generated summary and title.
    * Read or edit metadata (participants, notes, meeting date). Use the small edit icons or the "Edit Details" button.
    * Use the "Chat with Transcript" panel to ask questions about the recording content.
5.  **Manage:** Edit details or delete recordings using the buttons in the detail view or the icons in the recording list.

## Admin Panel

* Accessible at `/admin` for logged-in admin users.
* **User Management:** View, add, edit (username, email, password, admin status), and delete users.
* **System Statistics:** View application-wide usage data.

## Database Management

The following scripts are located in the application root (`/opt/transcription-app` if deployed):

* `reset_db.py`: **Use with caution!** This script deletes the existing database (`instance/transcriptions.db`) and the contents of the `uploads` directory, then creates a fresh, empty database schema.

## License

This project is **dual-licensed**:

1.  **GNU Affero General Public License v3.0 (AGPLv3)**
    [![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

    Speakr is offered under the AGPLv3 as its open-source license. You are free to use, modify, and distribute this software under the terms of the AGPLv3. A key condition of the AGPLv3 is that if you run a modified version on a network server and provide access to it for others, you must also make the source code of your modified version available to those users under the AGPLv3.

    * You **must** create a file named `LICENSE` (or `COPYING`) in the root of your repository and paste the full text of the [GNU AGPLv3 license](https://www.gnu.org/licenses/agpl-3.0.txt) into it.
    * Read the full license text carefully to understand your rights and obligations.

2.  **Commercial License**

    For users or organizations who cannot or do not wish to comply with the terms of the AGPLv3 (for example, if you want to integrate Speakr into a proprietary commercial product or service without being obligated to share your modifications under AGPLv3), a separate commercial license is available.

    Please contact **[Your Name/Company Name and Email Address or Website Link for Licensing Inquiries]** for details on obtaining a commercial license.

**You must choose one of these licenses** under which to use, modify, or distribute this software. If you are using or distributing the software without a commercial license agreement, you must adhere to the terms of the AGPLv3.

## Contributing

While direct code contributions are not the primary focus at this stage, feedback, bug reports, and feature suggestions are highly valuable! Please feel free to open an Issue on the GitHub repository.

**Note on Future Contributions and CLAs:**
Should this project begin accepting code contributions from external developers in the future, signing a **Contributor License Agreement (CLA)** will be **required** before any pull requests can be merged. This policy ensures that the project maintainer receives the necessary rights to distribute all contributions under both the AGPLv3 and the commercial license options offered. Details on the CLA process will be provided if and when the project formally opens up to external code contributions.
