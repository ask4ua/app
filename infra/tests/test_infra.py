"""Verify data isolation, firewall scope, and secret outputs without cloud access."""
import asyncio
from pathlib import Path
import runpy
import sys
import unittest

import pulumi
from pulumi.runtime import Mocks, set_all_config, set_mocks

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from settings import validate


class DataMocks(Mocks):
    def __init__(self):
        self.resources = {}

    def new_resource(self, args):
        self.resources[args.name] = args
        outputs = dict(args.inputs)
        outputs.setdefault("name", args.inputs.get("resourceGroupName", args.name) if args.typ == "azure-native:resources:ResourceGroup" else args.name)
        if args.typ == "pulumi:pulumi:StackReference":
            outputs["outputs"] = {"subscriptionId": "subscription", "tenantId": "tenant",
                "clusterLocation": "westeurope", "clusterName": "aks-existing",
                "resourceGroupName": "rg-existing", "oidcIssuerUrl": "https://issuer.example/"}
        if args.typ == "azure-native:managedidentity:UserAssignedIdentity":
            outputs.update(principalId="test-principal", clientId="test-client")
        if args.typ == "azure-native:dbforpostgresql:Server":
            outputs["fullyQualifiedDomainName"] = "test.postgres.database.azure.com"
        return [f"/mock/{args.name}", outputs]

    def call(self, args):
        raise AssertionError(args.token)


asyncio.set_event_loop(asyncio.new_event_loop())
mocks = DataMocks()
set_mocks(mocks, project="demoapp-data", stack="staging", preview=False)
set_all_config({"demoapp-data:aksStack": "org/aks/dev",
                "demoapp-data:postgresAllowedIps": '["8.8.8.8"]',
                "demoapp-data:entraAdminObjectId": "admin-object-id",
                "demoapp-data:entraAdminName": "admin@example.com"})
program = runpy.run_path(str(Path(__file__).resolve().parents[1] / "__main__.py"))


class InfrastructureTests(unittest.TestCase):
    @pulumi.runtime.test
    def test_resource_boundaries_and_recovery(self):
        def check(_):
            resources = mocks.resources
            self.assertFalse(any(r.typ.startswith("azure-native:containerservice:") for r in resources.values()))
            self.assertFalse(any(r.typ.startswith("kubernetes:") for r in resources.values()))
            self.assertEqual(resources["platform-subscription"].inputs["subscriptionId"], "subscription")
            server = resources["postgres"].inputs
            self.assertEqual(server["version"], "16")
            self.assertEqual(server["backup"]["backupRetentionDays"], 7)
            self.assertEqual(server["resourceGroupName"], resources["data"].inputs["resourceGroupName"])
            firewall = resources["allow-8-8-8-8"].inputs
            self.assertEqual(firewall["startIpAddress"], firewall["endIpAddress"])
            self.assertNotEqual(firewall["startIpAddress"], "0.0.0.0")
            photos = resources["photos"].inputs
            self.assertFalse(photos["allowBlobPublicAccess"])
            self.assertTrue(photos["enableHttpsTrafficOnly"])
            self.assertEqual(photos["minimumTlsVersion"], "TLS1_2")
            self.assertEqual(resources["challenges"].inputs["containerName"], "demoapp-staging")
            recovery = resources["blob-recovery"].inputs
            self.assertTrue(recovery["isVersioningEnabled"])
            self.assertEqual(recovery["containerDeleteRetentionPolicy"]["days"], 14)
            self.assertTrue(program["opts"].protect)
            self.assertEqual(server["authConfig"]["passwordAuth"], "Disabled")
            self.assertEqual(server["authConfig"]["activeDirectoryAuth"], "Enabled")
            self.assertNotIn("administratorLoginPassword", server)
            self.assertFalse(photos["allowSharedKeyAccess"])
            self.assertEqual(resources["app-federation"].inputs["subject"],
                             "system:serviceaccount:staging:demoapp-data")
            grant = resources["app-blob-access"].inputs
            self.assertEqual(grant["scope"], "/mock/challenges")
            self.assertEqual(grant["principalId"], "test-principal")
        return pulumi.Output.all(program["database"].urn, program["container"].urn,
                                 program["federation"].urn, program["blob_role"].urn,
                                 *[r.urn for r in program["firewalls"]]).apply(check)

    @pulumi.runtime.test
    def test_runtime_has_no_stored_credentials(self):
        def check(runtime):
            self.assertEqual(runtime["POSTGRES_AUTH"], "entra")
            self.assertNotIn("AZURE_STORAGE_CONNECTION_STRING", runtime)
            self.assertTrue(runtime["DATABASE_URL"].startswith("postgresql://demoapp@"))
        return pulumi.Output.from_input(program["runtime"]).apply(check)

    def test_rejects_unsafe_config(self):
        defaults = (32, 7, 14, "Standard_B1ms", "Burstable")
        for ips in [[], ["0.0.0.0"], ["10.1.0.1"], ["203.0.113.10"], ["8.8.8.8/0"], ["8.8.8.8", "8.8.8.8"]]:
            with self.subTest(ips=ips), self.assertRaises(ValueError):
                validate("staging", ips, *defaults)
        with self.assertRaises(ValueError):
            validate("dev", ["8.8.8.8"], *defaults)
        validate("production", ["8.8.8.8"], *defaults)
        for size in [0, 33, 65536]:
            with self.subTest(size=size), self.assertRaises(ValueError):
                validate("staging", ["8.8.8.8"], size, *defaults[1:])
