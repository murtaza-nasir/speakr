# Adding GitHub Secrets for Docker Hub Authentication

This guide provides step-by-step instructions on how to add your Docker Hub credentials as secrets in your GitHub repository.

## Step 1: Generate a Docker Hub Access Token

1. Log in to your Docker Hub account at [https://hub.docker.com/](https://hub.docker.com/)
2. Click on your username in the top-right corner and select "Account Settings"
3. In the left sidebar, click on "Security"
4. Click the "New Access Token" button
5. Give your token a descriptive name (e.g., "GitHub Actions")
6. Select "Read & Write" permissions
7. Click "Generate"
8. **IMPORTANT**: Copy the generated token immediately and save it somewhere secure. You won't be able to see it again after you leave this page.

## Step 2: Add Secrets to Your GitHub Repository

1. Go to your GitHub repository at [https://github.com/murtaza-nasir/speakr](https://github.com/murtaza-nasir/speakr)
2. Click on the "Settings" tab near the top of the page
3. In the left sidebar, click on "Secrets and variables", then select "Actions"
4. Click the "New repository secret" button
5. Add your Docker Hub username:
   - Name: `DOCKERHUB_USERNAME`
   - Value: Your Docker Hub username
   - Click "Add secret"
6. Click the "New repository secret" button again
7. Add your Docker Hub access token:
   - Name: `DOCKERHUB_TOKEN`
   - Value: The access token you generated in Step 1
   - Click "Add secret"

## Step 3: Verify the Secrets

1. After adding both secrets, they should appear in the "Repository secrets" list
2. The actual values will be hidden for security reasons
3. These secrets are now available for use in your GitHub Actions workflows

## Step 4: Test the Workflow

1. Make a small change to your repository and push it to the `master` branch
2. Go to the "Actions" tab in your GitHub repository
3. You should see the "Docker Build and Publish" workflow running
4. Once completed successfully, check your Docker Hub repository to verify that the image was pushed

## Troubleshooting

If the workflow fails with authentication errors:
- Verify that the secrets are named exactly `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`
- Check that your Docker Hub access token has "Read & Write" permissions
- Ensure that your Docker Hub account has a repository with the same name as your GitHub repository (`speakr`)
- Check the workflow logs for specific error messages

## Screenshots

Here's a visual guide to help you navigate the GitHub interface:

### Finding the Settings Tab
![GitHub Settings Tab](https://docs.github.com/assets/cb-40742/mw-1440/images/help/repository/repo-actions-settings.webp)

### Navigating to Secrets and Variables
![Secrets and Variables](https://docs.github.com/assets/cb-39735/mw-1440/images/help/settings/actions-settings.webp)

### Adding a New Secret
![New Secret](https://docs.github.com/assets/cb-86795/mw-1440/images/help/security/add-secret-repository.webp)
