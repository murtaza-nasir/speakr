# CI/CD Setup for Speakr

This page describes how the project builds and publishes its Docker images. The pipeline is owned by the GitHub Actions workflow in `.github/workflows/docker-publish.yml`; it builds and pushes multi-architecture images to Docker Hub with auditable build logs and provenance attestation. Forgejo runs the test suite only, so the same tag is never published twice.

## When the Workflow Runs

The workflow has two triggers:

- `release: types: [published]` - the normal path. Publishing a GitHub Release starts the build.
- `workflow_dispatch` - a manual run from the Actions tab, used for rebuilds and for a publish without a new release.

It does **not** run on pushes to `master` and it does **not** run when a tag is created on its own. Creating a tag without publishing a release builds nothing. There is no `pull_request` trigger, so a pull request never builds or pushes an image.

## Pipeline Stages

1. **Version check** (`version-check`; the comparison runs only for a release event). The workflow compares the contents of the `VERSION` file, with whitespace removed, against `github.event.release.tag_name`. They must match exactly, including the leading `v` (`VERSION` currently reads `v0.11.7`, so the release tag must be `v0.11.7`). A mismatch fails the run before anything is built. A manual run has no release tag to compare, so this step is skipped.
2. **Lite image smoke test** (`smoke-lite`). The lite image is built for `linux/amd64` into the runner's local Docker daemon and started with the environment that `tests/e2e/smoke_env.py` generates. `tests/e2e/test_image_smoke.py` opens its pages in a real browser. Both build jobs depend on this job, so an image that fails this check is never pushed.
3. **Full image** (`build`). Multi-arch (`linux/amd64`, `linux/arm64`) build and push.
4. **Lite image** (`build-lite`). Multi-arch build and push of the image without PyTorch.

The workflow pushes to the `learnedmachine/speakr` repository on Docker Hub.

## Published Tags

A release with tag `v0.11.7` publishes:

| Image | Tags |
| --- | --- |
| Full | `0.11.7`, `sha-<short SHA>`, `latest` |
| Lite | `lite`, `0.11.7-lite`, `sha-<short SHA>-lite` |

- The version tag is the release tag with the leading `v` removed (`type=match,pattern=v(.*),group=1`). There is no `major.minor` tag and no tag with the `v` prefix.
- The SHA tag identifies the exact commit the image was built from.
- `latest` always points at the full image. The lite job sets `latest=false`, because otherwise it would overwrite the full image's `latest` tag.
- A manual run publishes only tags that can be derived without a release: `sha-<short SHA>` and `latest` (full), `sha-<short SHA>-lite` and `lite` (lite). Version tags appear only when the run uses a matching `v*` tag ref.

## Repository Secrets

The build jobs log in to Docker Hub with repository secrets:

- `DOCKERHUB_USERNAME` - the Docker Hub account that owns or can push to `learnedmachine/speakr`.
- `DOCKERHUB_TOKEN` - an access token for that account with read and write permission.

Set them under **Settings > Secrets and variables > Actions**. If you run your own fork, change `IMAGE_NAME` in the workflow to your repository and add secrets for an account that can push to it.

## Release Process

1. Update `VERSION` to the new version, including the leading `v` (for example `v0.11.8`), and commit it.
2. Publish a GitHub Release whose tag is exactly the contents of `VERSION`, targeting that commit.
3. Publishing the release starts the workflow. If the version check fails, no image is built; correct `VERSION` or the tag and publish a new release, because re-running the failed job on the same commit repeats the same check.
4. Watch the run under the **Actions** tab. A green run ends with both images pushed to Docker Hub.

Pushing the tag alone does not build anything. The tag has to be attached to a published release.

## Using the Published Images

Pin a version in `docker-compose.yml` instead of following `latest`:

```yaml
services:
  app:
    image: learnedmachine/speakr:0.11.7
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

Use `learnedmachine/speakr:lite` or a `-lite` version tag for the image without PyTorch. The full image ships the local embedding stack for Inquire Mode, which you enable with `ENABLE_INQUIRE_MODE=true`; the lite image has no local model and uses text search unless `EMBEDDING_BASE_URL` points at an OpenAI-compatible embedding provider. Both images contain every other feature.

To roll back, set the image to a version tag that was actually published, without the leading `v`; see the [installation guide](getting-started/installation.md#updating-speakr).
