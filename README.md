# demoapp

A tiny OSINT-inspired browser game. Upload a photo and its country, then let another player choose from four countries. The answer and optional clue explanation appear after the guess.

## Start locally

Install and start Docker Desktop (or another Docker Engine with Compose v2), then run:

```sh
docker compose up --build -d
docker compose ps
```

Open **http://localhost:8000**. Open **Upload a challenge** and add the first photo; then switch to **Guess a country**. An empty installation intentionally has no photos. You can upload several challenges and share the browser with a teammate. Random selection can repeat photos, including your own.

API documentation: **http://localhost:8000/docs**.

```sh
docker compose logs -f app
docker compose down
```

Named volumes preserve both photos and database rows between restarts. `docker compose down -v` permanently deletes all local game data.

If port 8000 is busy, copy `.env.example` to `.env` and change `APP_PORT`. Only the web app is published to the host, bound to localhost. Database and storage are available on the internal Compose network.

## Components and data flow

- **API:** Python / FastAPI, serving the plain HTML/CSS/JavaScript frontend on the same origin.
- **Database:** PostgreSQL 16, accessed through parameterized psycopg queries.
- **Object storage:** Azurite, Microsoft's local Azure Blob Storage emulator, accessed through the Azure Blob Storage SDK and protocol. This is Azure-compatible storage, not an S3 adapter.

| Action | Blob Storage | PostgreSQL |
| --- | --- | --- |
| Upload challenge | Writes a normalized JPEG | Inserts country, explanation, object key |
| Start round | Reads photo through API image endpoint | Reads random challenge; inserts round and answer options |
| Submit guess | — | Reads answer; records guess, result, and timestamp |

Original filenames and EXIF/GPS metadata are discarded. Accepted uploads are JPEG, PNG, and WebP up to 8 MB and 20 million pixels; stored images are resized to fit 2400 × 2400. Correct answers and explanations are never included in the round response. A row lock prevents changing a submitted answer; retrying the same answer is safe. Failed database inserts trigger blob cleanup.

There are no accounts. Each round has an unguessable ID, but it is not tied to a user. The score shown in the browser lasts for that visit; individual guesses remain in PostgreSQL. Uploaders supply the answers; the app does not independently verify locations. This is a local demo, with no authentication or public-upload abuse controls.

## API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Check database and storage connectivity |
| GET | `/api/countries` | Country selector values |
| POST | `/api/challenges` | Multipart fields: `photo`, `country` (ISO alpha-2), optional `explanation` |
| POST | `/api/rounds` | Create a round with four country options |
| GET | `/api/rounds/{id}/image` | Read challenge image from Blob Storage |
| POST | `/api/rounds/{id}/guess` | JSON: `{"country":"UA"}`; save guess and reveal answer |

## Azure data infrastructure

The [`infra/`](infra/README.md) Pulumi project provisions separate PostgreSQL and
Blob Storage resources for staging and production, independently of application
releases. Follow its setup guide to provision data and install each namespace's
`demoapp-runtime` ConfigMap and workload ServiceAccount before deploying the Helm chart. The manual **Data
infrastructure** workflow supports preview and apply; CI checks the project with
Pulumi mocks.

### Connection settings

Set `AZURE_STORAGE_CONNECTION_STRING` in `.env` to an Azure Storage connection string and optionally set `AZURE_STORAGE_CONTAINER`. The same code and Blob protocol are used; the account must allow container creation (or the container must already exist). Keep secrets out of Git. For deployment, configure `DATABASE_URL` to point to Azure Database for PostgreSQL with its required TLS settings. The local Compose file intentionally supplies local database credentials; override that environment setting for a remote database.

Azurite is an emulator, so Azure deployment uses Entra Workload Identity for PostgreSQL and Blob Storage; the connection-string path remains available for local development. Schema creation currently happens at startup; use versioned migrations when evolving the database.

Reference: [Microsoft's Azurite documentation](https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azurite).

## Development and checks

Python 3.9+ is supported; the Docker image uses Python 3.12.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

To exercise actual PostgreSQL writes, Blob Storage uploads/downloads, answer hiding, and guess persistence against the running stack:

```sh
RUN_INTEGRATION=1 .venv/bin/python -m pytest -q
```

These integration tests create a small test photo and round in the local database. Set `TEST_BASE_URL` if using a different port. Without `RUN_INTEGRATION=1`, service tests are explicitly skipped.

```text
api/main.py          API, PostgreSQL queries, Azure Blob operations
web/                 Plain JavaScript game and upload form
tests/               Image validation and real-service API checks
compose.yaml         App + PostgreSQL + Azurite, health checks and volumes
```

## GitHub Actions and Azure publishing

`.github/workflows/ci.yml` runs unit tests, builds the Docker image, starts an
isolated Compose stack, and runs real PostgreSQL/Blob integration tests on pull
requests and pushes to `main`. After tests pass on `main`, it builds and pushes
`<registry-login-server>/apps/demoapp:sha-<full-commit-sha>` for `linux/amd64`.
Manual runs publish only on `main`. Pull requests never sign in to Azure.
The publish job summary records the image digest to use for deployment.
There is no mutable `latest` tag. A rerun can rebuild a SHA tag, so deploy by digest.

Create the GitHub environment **acr-publish**, restrict its deployment branches
to **main**, and add these environment variables (not client secrets):

| Variable | Value |
| --- | --- |
| `AZURE_CLIENT_ID` | Federated publishing identity's client ID |
| `AZURE_TENANT_ID` | Azure tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Subscription containing ACR |
| `ACR_NAME` | ACR resource name, without `.azurecr.io` |
| `ACR_LOGIN_SERVER` | Actual registry hostname from the infrastructure outputs |

The companion `app-tools` repo’s Pulumi project provisions the publishing identity and an
ACR-scoped `AcrPush` grant for the existing AKS platform's classic RBAC registry.
Its GitHub federation subject is `repo:OWNER/REPO:environment:acr-publish`, with
issuer `https://token.actions.githubusercontent.com` and audience
`api://AzureADTokenExchange`. `OWNER/REPO` must match this repository exactly.
No Azure password or AKS access is required by this workflow. The hosted runner
must be able to reach ACR; private registry networking needs a reachable runner.

Keep Argo CD and Prometheus infrastructure in the separate `app-tools` repo.
This pipeline also packages a Helm chart with the published image digest and
pushes it to ACR's `charts/staging` channel. Argo CD automatically deploys the
newest chart to the `staging` namespace. The **Promote to production** workflow
copies a tested chart version into `charts/production`, where Argo CD deploys it
to the `production` namespace without rebuilding the image.

See [deployment setup and promotion](deploy/README.md) for the Helm chart,
Argo CD Applications, per-environment secrets, registry access, and rollback.

References: [Azure Login OIDC](https://github.com/Azure/login#login-with-openid-connect-oidc-recommended),
[Docker build and push](https://github.com/docker/build-push-action).
