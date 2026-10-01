# Deploy Demo App with Argo CD

## Release flow

```text
Push to main → tests → image in ACR → Helm chart in ACR staging channel
                                         ↓
                              Argo CD → staging namespace
                                         ↓
                              Test the staging release
                                         ↓
                 Run “Promote to production” with its chart version
                                         ↓
                         Same chart copied to production channel
                                         ↓
                            Argo CD → production namespace
```

CI publishes `oci://REGISTRY.azurecr.io/charts/staging/demoapp:0.RUN.ATTEMPT`.
Each chart embeds `REGISTRY.azurecr.io/apps/demoapp@sha256:...`, plus both
environment values files. `RUN` and `ATTEMPT` are GitHub's workflow run number
and attempt, so reruns get a new chart version. Do not overwrite published charts
or reset this workflow's version sequence; promotion preserves archive contents.
Production tracks `oci://REGISTRY.azurecr.io/charts/production/demoapp`.

Argo CD selects the highest matching chart version in each channel and automatically
syncs, prunes removed chart resources, and corrects drift. Polling is sufficient;
no GitHub webhook or public Argo CD endpoint is required. Keep Argo CD's
`timeout.reconciliation` nonzero (its default is periodic reconciliation).
A GitHub source webhook is not the deployment trigger in this OCI-based design.
See [Argo CD reconciliation and webhooks](https://argo-cd.readthedocs.io/en/stable/operator-manual/webhook/).

## One-time platform setup

The companion `infra` repo owns Argo CD installation on AKS. This repo provides
application manifests; it does not reinstall Argo CD or provision PostgreSQL/Storage.
The following bootstrap must be completed before automatic deployment can work:

1. Configure the existing `acr-publish` GitHub environment as described in the root
   README. Its identity requires push access to the image and both chart repositories.
   The existing classic ACR `AcrPush` role covers this. Both publishing workflows
   use the same environment-bound OIDC federation and run only from `main`.
2. Ensure AKS's kubelet identity can pull the application image from ACR, or provide
   a namespace-local image pull Secret through the chart's `imagePullSecrets`.
3. Configure **Argo CD's repository server** separately to pull private OCI Helm
   charts. `argocd/repository-secret.yaml.example` shows a repository credential
   template with `enableOCI: 'true'` and the registry URL prefix. Supply a pull-only
   identity and credential through your platform's secret provisioning, outside Git.
   Kubelet image pull permission does not authenticate Argo CD's Helm client.
   Do not save a short-lived `az acr login --expose-token` token as a permanent
   Argo CD credential. A platform-managed Azure workload identity integration is
   another option, but it must be configured on Argo CD first.
4. Provision separate databases/users and Blob containers for staging and production.
   Separate namespaces alone do not isolate external data. The chart uses
   `demoapp-staging` and `demoapp-production` containers; preferably use separate
   storage accounts/credentials as well. The DB role needs schema-creation privileges
   because the app initializes its tables. Azure PostgreSQL URLs need TLS, e.g.
   `sslmode=require`, and URL-encoded credentials.
5. Create the runtime Secret in **each namespace**. Make `staging.runtime.env` and
   `production.runtime.env` from `runtime.env.example`; these filenames are ignored
   by Git. Populate the real credentials, then run from the repository root:

   ```sh
   kubectl create namespace staging --dry-run=client -o yaml | kubectl apply -f -
   kubectl create namespace production --dry-run=client -o yaml | kubectl apply -f -
   kubectl -n staging create secret generic demoapp-runtime \
     --from-env-file=staging.runtime.env --dry-run=client -o yaml | kubectl apply -f -
   kubectl -n production create secret generic demoapp-runtime \
     --from-env-file=production.runtime.env --dry-run=client -o yaml | kubectl apply -f -
   ```

6. Replace `REGISTRY.azurecr.io` in `argocd/applications.yaml` with the actual ACR
   hostname, then bootstrap it once:

   ```sh
   kubectl apply -f deploy/argocd/applications.yaml
   ```

   This creates a scoped AppProject and two Applications in `argocd`. Argo CD
   creates/syncs app resources in `staging` and `production`. Before the first
   publish/promotion, the corresponding Application will report no matching chart;
   it reconciles after a chart is available. Keep these bootstrap resources under
   the platform's GitOps/Pulumi ownership when integrating with the infra repo.

No cluster access or Argo CD API token is needed in GitHub Actions. AKS and Argo CD
must have network access to ACR; app pods must reach their DB and Storage endpoints.

## Promote a tested release

1. Confirm `demoapp-staging` is **Synced** and **Healthy** in Argo CD. Note the
   resolved chart version from Argo CD or the publishing workflow's summary.
2. Test upload, image display, guessing, and answer reveal in staging.
3. In GitHub Actions, open **Promote to production → Run workflow**, select `main`,
   and enter the exact tested version, for example `0.42.1`.
4. The workflow pulls that version from the staging channel and pushes the same
   archive to production. It does not build a new image or query the AKS cluster.
5. Confirm `demoapp-production` becomes **Synced** and **Healthy** at that version.

The workflow trusts the operator's selection of a tested version; it does not
claim to verify staging health automatically. A green promotion job means the
chart was published, not that the production rollout is already healthy.
The workflows share `acr-publish`; this is not a separate production approval gate.
Promotion rejects older versions because a version range would otherwise silently
keep deploying the newer chart. Repeating promotion of the same immutable version
is harmless. Avoid publishing new versions during a rollback until it is resolved.

## Rollback

Set production's `spec.source.targetRevision` in `argocd/applications.yaml` to an
exact previously promoted version, then apply that manifest (or sync its platform
owner). Automatic sync deploys the pinned chart. Keep the pin in the source of truth;
a one-off Argo rollback will be undone by auto-sync if desired state still tracks
the newest version. Restore `'>=0.0.0 <1.0.0'` when ready to resume promotions.
Database changes/data are not rolled back by Helm; current schema creation is
additive and serialized across replicas with a PostgreSQL advisory lock.

## Environment behavior

| Setting | Staging | Production |
| --- | --- | --- |
| Namespace | `staging` | `production` |
| Replicas | 1 | 2 |
| Runtime Secret | `staging/demoapp-runtime` | `production/demoapp-runtime` |
| Blob container | `demoapp-staging` | `demoapp-production` |
| Voluntary disruption budget | Disabled | At least 1 available |

The chart fails rendering if an environment values file is used in the wrong
namespace. Readiness checks DB and Blob connectivity; liveness only checks the
HTTP server. Pods use a read-only root filesystem with writable `/tmp` for uploads.
No persistent pod disk is needed. Secret rotations require a rollout to reload
environment variables; update `podAnnotations` in desired values or restart pods.

Ingress is off until a real controller, host, TLS certificate, and appropriate
access controls are available. For a temporary local view:

```sh
kubectl -n staging port-forward service/demoapp-staging-demoapp 8000:80
```

Then visit http://localhost:8000. To enable ingress, configure `ingress.enabled`,
`className`, `host`, `tls`, and controller-specific annotations in the environment
values file before publishing. Allow at least 9 MB request bodies for 8 MB photos
plus multipart overhead. Changes to packaged values require a new chart release.
The app has no authentication; a production namespace does not add it.

## Local validation

```sh
.venv/bin/python -m pytest -q tests/test_helm.py
```

Tests lint and render both environments, check namespace mismatch rejection,
verify ingress/TLS and secret references, and package a chart to verify its
embedded image digest. CI runs the same checks before publishing.

References: [Argo CD OCI Helm sources](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/),
[repository credentials](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/).
