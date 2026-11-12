# DeepSift release workflow

A single source commit can be tested and **manually** released to the 21stReach VPS, the Apify Actor, or both. Workflow: `.github/workflows/release.yml`.

## How to release

1. Review, test, commit and push the intended DeepSift source changes.
2. Open GitHub > pavanmulka/deepsift > Actions > **Release DeepSift** > **Run workflow**.
3. Choose the intended branch/commit. Tick **deploy_vps** and/or **deploy_apify**. Both default off (test only).
4. Monitor the test job and both selected deploy jobs. Verify production APIs/MCP after deployment and capture the deployed Git SHA from the run.

This workflow is dispatch-only. Nothing automatically deploys on a normal push.

## One-time setup

Configure GitHub Actions secrets (Repository Settings > Secrets and variables > Actions):
- `DEEPSIFT_VPS_HOST`: the VPS hostname or address.
- `DEEPSIFT_VPS_USER`: deploy SSH user with access to `/docker/21streach-deepsift/src` and permission to run Docker Compose. Current directory is root-owned; don't assume a non-root account has write access.
- `DEEPSIFT_VPS_SSH_KEY`: private deploy SSH key, authorized for the specified user.
- `DEEPSIFT_VPS_KNOWN_HOSTS`: verified SSH host key line(s), obtained from a trusted source. Do not use `StrictHostKeyChecking=no`.
- `APIFY_TOKEN`: Apify token for the Aadstra publisher account with permission to deploy `aadstra/deepsift-search`.

Create/secure GitHub environment `deepsift-production` and, if desired, add required reviewers and restrict deployment branches. An environment name by itself **does not guarantee approval is required**.

Ensure deployment user can access Docker, rsync, and the existing Compose project. Never commit secret values.

## VPS

VPS project root: `/docker/21streach-deepsift`; its existing Compose build context is `./src`, Dockerfile `deploy/Dockerfile`, service name `deepsift`. Deployment copies only `deepsift/`, `deploy/` and `requirements.txt` into `src`; it does **not** replace `docker-compose.yml`, `searxng.yml`, env settings, persistent volumes, SearXNG, or 21stReach app code. Docker Compose rebuilds/restarts only the DeepSift service.

Before first production use, validate SSH and directory permissions with the chosen deploy identity. Docker image builds require enough disk space. After release, verify DeepSift container health and perform a real search from 21stReach; a container health signal does not guarantee relevance or end-to-end availability.

## Apify

CLI pushes the checked-out Actor source to `aadstra/deepsift-search` using `APIFY_TOKEN`. It waits for build outcome. Confirm which Actor build tag/version is serving the public API and MCP endpoint before treating it as live.

## Rollback

There is no automatic transactional rollback across VPS and Apify. Keep the previous commit SHA, then manually dispatch an approved previous revision (or use the platforms' previous image/build controls). Validate each destination independently. Do not overwrite server Compose configuration, secrets, or runtime data while rolling back.

## Deployment consistency

Both deployment jobs use the same `github.sha` from the triggering run, but they are independently gated, so operators may intentionally deploy only one. A successful job is not evidence the other environment was updated. Keep a small release log of SHA, VPS status, Apify build and smoke-test results.
