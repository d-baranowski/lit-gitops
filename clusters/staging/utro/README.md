# utro — staging (EKS / lit)

Ported from `k8s-setup/clusters/may-chang/utr-staging`, adapted for this platform.
Flux syncs it via the `utro` Kustomization (`../flux-system/utro-kustomization.yaml`),
gated behind `external-secrets-resources` so the ClusterSecretStore exists first.

## What changed vs may-chang

| Concern | may-chang (self-managed) | here (lit / EKS) |
| --- | --- | --- |
| Registry | `ghcr.io/inspiration-particle/utro-*` + `ghcr-pull-secret` | `277265293752.dkr.ecr.eu-central-1.amazonaws.com/utro-*`; pods pull via node IAM (no imagePullSecret), Flux reflector via `provider: aws` (Pod Identity) |
| Database | in-cluster Zalando `postgresql` CRD (`db-cluster.yaml`) | **external Aurora** — `db-cluster.yaml` dropped |
| DB creds | operator secrets `<role>.pg-staging-cluster.credentials…zalan.do` | ESO `ExternalSecret`s from `lit/staging/postgres/<role>` |
| DB host | `pg-staging-cluster.default.svc…` | `host` key from the DB ExternalSecret (endpoint-agnostic) |
| DB name | `utr_staging` | `utro` (`aurora_database_name`) |
| App secrets | flat k8s Secrets (key `value`) | ESO from `lit/staging/{stripe,resend,twilio}` |

### DB role mapping (Aurora `aurora_roles`: admin/bootstrap/migrations/app/core_event)

| Workload | Aurora role | k8s Secret |
| --- | --- | --- |
| core, gateway, payment, notification (runtime) | `app` (non-owner) | `db-app` |
| all `*-migrate` Jobs | `migrations` (owner) | `db-migrations` |
| bootstrap service (seeds users/permissions/data) | `migrations` (owner) | `db-migrations` |
| core-event (runtime CDC) | `core_event` (rds_replication) | `db-core-event` |

`admin` + `bootstrap` roles are still provisioned (rds_superuser DBA identities)
but nothing runs as them in-cluster, so they have no ExternalSecret.

### RLS model on Aurora (no BYPASSRLS)

may-chang ran in-cluster Zalando Postgres with a real `SUPERUSER`, so it stamped
`BYPASSRLS` on the migrator + core_event roles. **Aurora/RDS can grant BYPASSRLS
to no one** (the master is `NOSUPERUSER` and there's no `rds_bypassrls` role), so
we adopt the [AWS-recommended RLS posture](https://aws.amazon.com/blogs/database/multi-tenant-data-isolation-with-postgresql-row-level-security/):

- **No `FORCE ROW LEVEL SECURITY`.** The two tables that were FORCE'd
  (`core.user`, `core.user_role`) have it removed in
  `app/core/db/20240612052730_permissions.sql`. Every other core table was
  already net NO-FORCE. So the **table owner (`migrations`) bypasses RLS** and can
  run migrations + seed data — the substitute for the old migrator's BYPASSRLS.
- **App traffic connects as a non-owner** (`app`), so it stays fully subject to
  the policies (which read identity from the `core.user` GUC). Unchanged.
- **core-event** is a non-owner that must read all rows for enrichment, so it gets
  permissive `core_event_read` policies (see below) instead of BYPASSRLS.
- **Service "run without RLS" paths** (the repository's `SkipRLS` calls — e.g. the
  pre-auth **login** lookup that reads `core.user` by email as the non-owner `app`
  role) also can't use BYPASSRLS. Instead `pkg/repository`'s `BypassRLS` sets a
  transaction-local `core.bypass='on'` flag, and `core.can()` short-circuits to
  `true` when it's set — so every policy passes for that one transaction. **This is
  what makes password login work** (this deployment does not use Cloudflare Access).
  Security note: `core.bypass` is a session GUC any connection can set, so it moves
  the bypass gate from a role attribute to "the app sets it on purpose" — acceptable
  because the app is the trust boundary and uses parameterized (bun) queries.
  Requires the edited `core.can()` migrations (`permissions.sql` +
  `rls_performance.sql`) to be applied.

## Prerequisites (do these BEFORE / alongside the first Flux sync)

1. **Aurora up + roles bootstrapped — BEFORE the migrate Jobs run.** From `infrastructure/`:
   ```sh
   terraform -chdir=environments/staging apply      # Aurora + lit/staging/postgres/* secrets + params
   ./ansible/bootstrap-db.sh staging                # CREATE ROLE admin/bootstrap/migrations/app/core_event
                                                    #   + rds_replication + pg_cron extension + grants
   ```
   The migrate Jobs connect as `migrations` and the cron migration needs the
   `cron` schema, so run `bootstrap-db.sh` first. If Flux runs the Jobs before it,
   they fail and retry (backoffLimit) until the roles + pg_cron exist — eventually
   consistent, just noisy.

   **Reboot note:** `rds.logical_replication`, `shared_preload_libraries=pg_cron`,
   and `cron.database_name` are *static* params. A fresh cluster gets them at
   creation; enabling them on an *existing* cluster needs a one-time **writer
   reboot** before `bootstrap-db.sh` can `CREATE EXTENSION pg_cron`.

2. **Create the third-party app secrets** in Secrets Manager under `lit/staging/*`
   (ESO's IAM role can read the whole prefix). One JSON secret per vendor:
   ```sh
   aws secretsmanager create-secret --region eu-central-1 --name lit/staging/stripe \
     --secret-string '{"publishable_key":"pk_...","secret_key":"sk_...","webhook_secret":"whsec_..."}'
   aws secretsmanager create-secret --region eu-central-1 --name lit/staging/resend \
     --secret-string '{"api_key":"re_...","webhook_secret":"whsec_..."}'
   aws secretsmanager create-secret --region eu-central-1 --name lit/staging/twilio \
     --secret-string '{"account_sid":"AC...","auth_token":"...","from_phone":"+1..."}'
   ```

3. **Semver images in ECR.** The `ImagePolicy`s deploy the highest `vX.Y.Z` tag,
   which `utro-release` pushes. The `utro-build` pipeline's `:<sha>`/`:<branch>`
   tags are intentionally ignored. The pinned `:0.1.64` refs are placeholders —
   they must exist in ECR, or Flux's automation must find a semver tag to bump to,
   before pods can pull.

## core-event / logical replication (works out of the box)

The infra now provisions everything core-event's CDC needs — no manual steps:
- `modules/aurora-postgres` sets `rds.logical_replication=1` (wal_level=logical)
  on the cluster parameter group (toggle: `enable_logical_replication`, default true);
- `aurora_roles` includes a dedicated **`core_event`** role → secret
  `lit/staging/postgres/core_event` → ESO `db-core-event`;
- the Ansible db bootstrap runs `GRANT rds_replication TO core_event`.

Static-parameter caveat: enabling `rds.logical_replication` on an *existing*
cluster needs a one-time **writer reboot** (fresh clusters get wal_level=logical
at creation).

RLS caveat (handled): may-chang's core_event role used the `BYPASSRLS` attribute
to read the RLS-protected core tables for enrichment. Aurora's rds_superuser
can't set that attribute, and the app's `core_event_grants` migration only grants
SELECT — which RLS filters to **zero rows** on its own. Migration
`app/core/db/20260724000000_core_event_rls_read_policies.sql` closes the gap with
permissive `core_event_read` SELECT policies (equivalent read scope to BYPASSRLS,
no broader exposure). So once the core migrations run, enrichment works — no
manual step.

## pg_cron

The core schema schedules a job (`cron.schedule('*/1 * * * *', …)` in
`20240612052729_user.sql`) to clear expired user locks. Zalando preloaded
pg_cron; Aurora needs it wired up (all handled by infra, toggle
`aurora_enable_pg_cron`, default on):

- `modules/aurora-postgres` sets `shared_preload_libraries=pg_cron` and
  `cron.database_name=utro` on the cluster parameter group (static — see the
  reboot note above);
- the Ansible db bootstrap runs `CREATE EXTENSION pg_cron` (as the master, in the
  `utro` DB) and `GRANT USAGE ON SCHEMA cron TO migrations`, so the migration's
  `cron.schedule` call — which runs as `migrations` — succeeds.

To skip pg_cron entirely, set `aurora_enable_pg_cron=false` **and**
`db_enable_pg_cron=false` (Ansible), then remove the `cron.schedule` line.

## Known caveats

- **No Ingress.** Neither may-chang's manifests nor these expose the UI/gateway
  externally (that lived in the cluster's ingress layer). Add an Ingress/Gateway
  for the new cluster's entrypoint separately.
- **Observability.** `TR_ENDPOINT` / `OTEL_COLLECTOR_URL` point at
  `otel-collector.observability.svc.cluster.local`. If this cluster has no
  `observability` namespace, trace export just fails silently (non-fatal); set
  `TR_ENABLED=false` to quieten it.
- **Hostnames/CF Access.** `PUBLIC_WEBHOOK_URL` and the `CF_*` values are carried
  over from inspiration-particle — update them for this cluster's domain/CF app.

## Validate locally

```sh
kubectl kustomize clusters/staging/utro      # renders 44 objects, no errors
```
