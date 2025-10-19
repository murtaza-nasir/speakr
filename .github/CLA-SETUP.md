# CLA Assistant Setup Instructions

This document explains how to set up automated CLA checking for Speakr.

## What We're Using

**CLA Assistant** - A free, open-source GitHub Action that automatically:
- Comments on PRs asking contributors to sign the CLA
- Tracks who has signed
- Blocks PR merging until CLA is signed
- Stores signatures in a JSON file

## Setup Steps

### 1. Create a Personal Access Token (PAT)

The CLA Assistant needs a GitHub Personal Access Token to create commits for storing signatures.

1. Go to GitHub Settings → Developer Settings → Personal Access Tokens → Tokens (classic)
   - Or visit: https://github.com/settings/tokens

2. Click "Generate new token" → "Generate new token (classic)"

3. Give it a descriptive name: `Speakr CLA Assistant`

4. Set expiration: `No expiration` (or your preferred duration)

5. Select scopes:
   - ✅ `repo` (Full control of private repositories)
   - ✅ `workflow` (Update GitHub Action workflows)

6. Click "Generate token"

7. **IMPORTANT**: Copy the token immediately (you won't see it again!)

### 2. Add Token to Repository Secrets

1. Go to your repository: https://github.com/murtaza-nasir/speakr

2. Navigate to: Settings → Secrets and variables → Actions

3. Click "New repository secret"

4. Name: `PERSONAL_ACCESS_TOKEN`

5. Value: Paste the token you copied in step 1

6. Click "Add secret"

### 3. Commit the CLA Files

The files have been created locally. Commit them:

```bash
git add CLA.md CONTRIBUTING.md .github/workflows/cla.yml .github/CLA-SETUP.md
git commit -m "Add Contributor License Agreement and automated CLA checking"
git push
```

### 4. Create the Signatures Branch

The CLA Assistant will store signatures in a separate branch:

```bash
# Create and push the signatures branch
git checkout -b cla-signatures
git push -u origin cla-signatures
git checkout master  # or main
```

### 5. Update README.md (Optional but Recommended)

Add a badge to show CLA status. Add this near the top of README.md:

```markdown
[![CLA assistant](https://cla-assistant.io/readme/badge/murtaza-nasir/speakr)](https://cla-assistant.io/murtaza-nasir/speakr)
```

Add a link in the Contributing section:

```markdown
## Contributing

We welcome contributions! Please read our [Contributing Guide](CONTRIBUTING.md) to learn about our CLA process and development workflow.
```

### 6. Test the Setup

1. Create a test PR from a different account or ask someone to create one

2. The CLA bot should automatically comment asking for signature

3. They sign by commenting: `I have read the CLA Document and I hereby sign the CLA`

4. The bot updates the PR with a success message

## How It Works

### For Contributors

1. They open a PR
2. Bot comments with CLA instructions
3. They read [CLA.md](../CLA.md)
4. They comment: `I have read the CLA Document and I hereby sign the CLA`
5. Bot records signature in `.github/signatures/cla.json` on `cla-signatures` branch
6. Bot marks PR as CLA-signed ✅
7. Future PRs from same user are auto-approved

### For Maintainers

- You'll see CLA status checks on PRs
- Signatures are stored in `.github/signatures/cla.json`
- You can manually check signatures anytime
- Merging is only possible after CLA is signed

## Viewing Signatures

All signatures are stored in:
```
https://github.com/murtaza-nasir/speakr/blob/cla-signatures/.github/signatures/cla.json
```

Format:
```json
{
  "signedContributors": [
    {
      "name": "username",
      "id": 12345,
      "comment_id": 67890,
      "created_at": "2025-01-18T12:34:56Z",
      "repoId": 123456789,
      "pullRequestNo": 42
    }
  ]
}
```

## Troubleshooting

### Bot Not Commenting on PRs

- Check that PERSONAL_ACCESS_TOKEN secret is set
- Verify token has correct permissions
- Check GitHub Actions are enabled for repo
- Look at Actions tab for error logs

### "Branch 'cla-signatures' not found"

```bash
git checkout -b cla-signatures
git push -u origin cla-signatures
git checkout master
```

### Need to Reset Signatures

To remove all signatures (use carefully!):
```bash
git checkout cla-signatures
rm .github/signatures/cla.json
git commit -m "Reset CLA signatures"
git push
```

### Want Someone to Re-sign

Delete their entry from `.github/signatures/cla.json` and commit:
```bash
git checkout cla-signatures
# Edit .github/signatures/cla.json to remove the user
git add .github/signatures/cla.json
git commit -m "Remove CLA signature for username"
git push
git checkout master
```

## Customization

You can customize the CLA bot messages by editing `.github/workflows/cla.yml`:

- `custom-notsigned-prcomment` - Message shown to unsigned contributors
- `custom-pr-sign-comment` - Message after signing
- `custom-allsigned-prcomment` - Message when all have signed
- `allowlist` - Users who don't need to sign (bots, etc.)

## Alternative: Lighter Weight DCO

If you want something simpler, consider using **Developer Certificate of Origin (DCO)** instead:
- Contributors add `Signed-off-by: Name <email>` to commits
- No separate signature required
- Less formal but still legally binding
- Used by Linux kernel and many projects

Let me know if you'd prefer DCO instead!

## Support

- CLA Assistant Docs: https://github.com/contributor-assistant/github-action
- Issues with setup? Open an issue in this repo
