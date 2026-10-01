"""Map the AKS managed identity into PostgreSQL and install passwordless runtime config."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import certifi
import psycopg
from azure.identity import AzureCliCredential

SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def grant_access(conn):
    conn.execute("REVOKE ALL ON DATABASE demoapp FROM PUBLIC")
    conn.execute("GRANT CONNECT ON DATABASE demoapp TO demoapp")
    conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    conn.execute("GRANT USAGE, CREATE ON SCHEMA public TO demoapp")


def provision_role(host, admin_name, principal_id, token):
    connection = dict(host=host, user=admin_name, password=token, sslmode="verify-full",
                      sslrootcert=certifi.where(), connect_timeout=15)
    # pgaadauth functions are installed in the postgres database, not the app DB.
    with psycopg.connect(dbname="postgres", **connection) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(724031582)")
        rows = conn.execute("SELECT * FROM pg_catalog.pgaadauth_list_principals(false)").fetchall()
        existing = next((row for row in rows if row[0] == "demoapp"), None)
        if existing:
            if existing[1].lower() != "service" or existing[2].lower() != principal_id.lower() or existing[5]:
                raise ValueError("Existing demoapp principal mapping does not match this environment")
        else:
            conn.execute("SELECT * FROM pg_catalog.pgaadauth_create_principal_with_oid("
                         "%s, %s, 'service', false, false)", ("demoapp", principal_id))
    with psycopg.connect(dbname="demoapp", **connection) as conn:
        grant_access(conn)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", required=True, choices=["staging", "production"])
    parser.add_argument("--context", required=True, help="Explicit kubectl context for the intended AKS cluster")
    args = parser.parse_args()
    result = subprocess.run(["pulumi", "stack", "output", "--json", "--stack", args.stack],
                            cwd=Path(__file__).parent, check=True, capture_output=True, text=True)
    outputs = json.loads(result.stdout)
    if outputs["environment"] != args.stack:
        raise ValueError("Stack and target namespace do not match")
    kubectl = ["kubectl", "--context", args.context]
    subprocess.run(kubectl + ["cluster-info"], check=True, capture_output=True)
    with AzureCliCredential(tenant_id=outputs["tenantId"]) as credential:
        token = credential.get_token(SCOPE).token
        provision_role(outputs["postgresHost"], outputs["entraAdminName"],
                       outputs["identityPrincipalId"], token)
    objects = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": args.stack}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {
            "name": "demoapp-data", "namespace": args.stack, "annotations": {
                "azure.workload.identity/client-id": outputs["identityClientId"],
                "azure.workload.identity/tenant-id": outputs["tenantId"]}},
            "automountServiceAccountToken": False},
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": "demoapp-runtime", "namespace": args.stack}, "data": outputs["runtime"]},
    ]
    for obj in objects:
        subprocess.run(kubectl + ["apply", "--server-side", "--field-manager=demoapp-data", "-f", "-"],
                       input=json.dumps(obj), text=True, check=True, capture_output=True)
    print(f"Entra role, service account and {args.stack}/demoapp-runtime ConfigMap are ready.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Avoid logging SQL connection parameters or access tokens.
        print(f"Bootstrap failed ({type(error).__name__}); check Entra login/admin mapping, "
              "DB firewall, and kubectl context permissions.", file=sys.stderr)
        sys.exit(1)
