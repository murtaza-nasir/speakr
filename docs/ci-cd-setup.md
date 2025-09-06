# CI/CD Setup for Speakr

This document explains how the Continuous Integration and Continuous Deployment (CI/CD) pipeline is set up for the Speakr project.

## Automatic Docker Image Building and Publishing

The Speakr project uses GitHub Actions to automatically build and publish Docker images to Docker Hub whenever changes are pushed to the repository.

### How It Works

1. When code is pushed to the `master` branch or a new tag is created (in the format `v*.*.*`), the GitHub Actions workflow is triggered.
2. The workflow builds a Docker image using the project's Dockerfile.
3. If the trigger was a push to the `master` branch, the image is tagged with:
   - `latest`
   - The short SHA of the commit
   - The branch name (`master`)
4. If the trigger was a new tag (e.g., `v1.2.3`), the image is tagged with:
   - The full version (`1.2.3`)
   - The major.minor version (`1.2`)
   - The tag name (`v1.2.3`)
5. The image is then pushed to Docker Hub under your account.

### Setup Instructions

To set up automatic Docker image publishing, follow these steps:

1. **Create a Docker Hub Account** (if you don't already have one):
   - Go to [Docker Hub](https://hub.docker.com/) and sign up for an account.
   - Create a repository named `speakr`.

2. **Create a Docker Hub Access Token**:
   - Log in to Docker Hub.
   - Go to your account settings by clicking on your username in the top-right corner and selecting "Account Settings".
   - Navigate to the "Security" tab.
   - Click "New Access Token".
   - Give your token a name (e.g., "GitHub Actions") and set the appropriate permissions (at least "Read & Write").
   - Copy the generated token (you won't be able to see it again).

3. **Add Docker Hub Credentials to GitHub Secrets**:
   - Go to your GitHub repository.
   - Click on "Settings" > "Secrets and variables" > "Actions".
   - Click "New repository secret".
   - Add the following secrets:
     - Name: `DOCKERHUB_USERNAME`, Value: Your Docker Hub username
     - Name: `DOCKERHUB_TOKEN`, Value: The access token you generated in the previous step

4. **Verify the Workflow**:
   - The workflow file is located at `.github/workflows/docker-publish.yml`.
   - It's already configured to build and push Docker images to Docker Hub.
   - You can customize it further if needed.

5. **Trigger the Workflow**:
   - Push a change to the `master` branch or create a new tag to trigger the workflow.
   - Go to the "Actions" tab in your GitHub repository to monitor the workflow's progress.

### Using the Published Docker Images

Once the images are published to Docker Hub, you can use them in your docker-compose.yml file:

```yaml
version: '3.8'

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
    env_file:
      - .env
```

### Versioning Strategy

To create a new version of your application:

1. Make your changes and commit them to the repository.
2. Create a new tag following semantic versioning:
   ```bash
   git tag v1.0.0  # Replace with the appropriate version number
   git push origin v1.0.0
   ```
3. The GitHub Actions workflow will automatically build and publish a new Docker image with the appropriate version tags.

This allows users to choose between using the latest version (`learnedmachine/speakr:latest`) or a specific version (`learnedmachine/speakr:1.0.0`).
