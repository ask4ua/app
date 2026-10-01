"""Check Entra principal mapping and passwordless Kubernetes configuration."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("data_bootstrap", Path(__file__).resolve().parents[1] / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def outputs(self, environment="staging"):
        return {"environment": environment, "postgresHost": "test.postgres.database.azure.com",
                "entraAdminName": "admin@example.com", "identityPrincipalId": "principal-id",
                "identityClientId": "client-id", "tenantId": "tenant-id",
                "runtime": {"DATABASE_URL": "postgresql://demoapp@example/demoapp", "POSTGRES_AUTH": "entra"}}

    def test_no_credentials_in_kubernetes(self):
        outputs = self.outputs()
        with patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(outputs))) as run, \
                patch.object(bootstrap, "provision_role") as provision, \
                patch.object(bootstrap, "AzureCliCredential") as credential, \
                patch("sys.argv", ["bootstrap.py", "--stack", "staging", "--context", "aks-test"]), \
                redirect_stdout(io.StringIO()) as stdout:
            credential.return_value.__enter__.return_value.get_token.return_value.token = "private-token"
            bootstrap.main()
        provision.assert_called_once_with(outputs["postgresHost"], outputs["entraAdminName"],
                                           "principal-id", "private-token")
        objects = [json.loads(call.kwargs["input"]) for call in run.call_args_list if "input" in call.kwargs]
        self.assertEqual([o["kind"] for o in objects], ["Namespace", "ServiceAccount", "ConfigMap"])
        self.assertEqual(objects[-1]["data"], outputs["runtime"])
        self.assertNotIn("private-token", str(run.call_args_list))
        self.assertNotIn("private-token", stdout.getvalue())

    def test_mismatched_environment_stops_before_changes(self):
        with patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(
                stdout=json.dumps(self.outputs("production")))), \
                patch.object(bootstrap, "provision_role") as provision, \
                patch("sys.argv", ["bootstrap.py", "--stack", "staging", "--context", "aks-test"]):
            with self.assertRaises(ValueError):
                bootstrap.main()
            provision.assert_not_called()

    def test_mapping_uses_object_id_and_postgres_database(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        with patch.object(bootstrap.psycopg, "connect") as connect:
            connect.return_value.__enter__.return_value = conn
            bootstrap.provision_role("host", "admin", "principal-id", "token")
        self.assertEqual(connect.call_args_list[0].kwargs["dbname"], "postgres")
        self.assertEqual(connect.call_args_list[1].kwargs["dbname"], "demoapp")
        sql = [c.args[0] for c in conn.execute.call_args_list]
        self.assertTrue(any("pgaadauth_create_principal_with_oid" in q for q in sql))
        self.assertIn("GRANT USAGE, CREATE ON SCHEMA public TO demoapp", sql)

    def test_rejects_wrong_existing_identity(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [("demoapp", "service", "wrong-id", "tenant", 0, 0)]
        with patch.object(bootstrap.psycopg, "connect") as connect:
            connect.return_value.__enter__.return_value = conn
            with self.assertRaises(ValueError):
                bootstrap.provision_role("host", "admin", "principal-id", "token")
        self.assertEqual(connect.call_count, 1)
