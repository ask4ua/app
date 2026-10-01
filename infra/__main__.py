"""App data has independent state and survives Helm/Argo CD releases."""
import uuid

import pulumi
from pulumi_azure_native import Provider, authorization, dbforpostgresql as pg, managedidentity, resources, storage

from settings import validate

config = pulumi.Config()
environment = pulumi.get_stack()
ips = config.require_object("postgresAllowedIps")
size = config.get_int("postgresStorageGb")
size = 32 if size is None else size
backup_days = config.get_int("backupRetentionDays")
backup_days = 7 if backup_days is None else backup_days
blob_days = config.get_int("blobRetentionDays")
blob_days = 14 if blob_days is None else blob_days
sku = config.get("postgresSku") or "Standard_B1ms"
tier = config.get("postgresTier") or "Burstable"
validate(environment, ips, size, backup_days, blob_days, sku, tier)
admin_object_id = config.require("entraAdminObjectId")
admin_name = config.require("entraAdminName")
admin_type = config.get("entraAdminType") or "User"
platform = pulumi.StackReference(config.require("aksStack"))
subscription = platform.require_output("subscriptionId")
azure = Provider("platform-subscription", subscription_id=subscription,
                 tenant_id=platform.require_output("tenantId"))
protect = config.get_bool("protect")
opts = pulumi.ResourceOptions(provider=azure, protect=True if protect is None else protect)
location = platform.require_output("clusterLocation")
# Include subscription identity to avoid name collisions across deployments.
suffix = subscription.apply(lambda sub: uuid.uuid5(
    uuid.NAMESPACE_URL, f"{sub}/{pulumi.get_project()}/{environment}").hex[:12])
tags = {"managed-by": "pulumi", "app": "demoapp", "environment": environment}
rg = resources.ResourceGroup("data", resource_group_name=suffix.apply(
    lambda s: f"rg-demoapp-{environment}-{s}"), location=location, tags=tags, opts=opts)
server = pg.Server("postgres", resource_group_name=rg.name, location=location,
    server_name=suffix.apply(lambda s: f"pg-demoapp-{environment}-{s}"),
    version="16",
    auth_config={"password_auth": "Disabled", "active_directory_auth": "Enabled",
                 "tenant_id": platform.require_output("tenantId")},
    sku={"name": sku, "tier": tier},
    storage={"storage_size_gb": size, "type": "Premium_LRS", "auto_grow": "Enabled"},
    backup={"backup_retention_days": backup_days, "geo_redundant_backup": "Disabled"},
    high_availability={"mode": "Disabled"},
    network={"public_network_access": "Enabled"}, tags=tags, opts=opts)
identity = managedidentity.UserAssignedIdentity("app-identity", resource_group_name=rg.name,
    resource_name_=suffix.apply(lambda s: f"id-demoapp-{environment}-{s}"),
    location=location, tags=tags, opts=opts)
federation = managedidentity.FederatedIdentityCredential("app-federation",
    resource_group_name=rg.name, resource_name_=identity.name,
    federated_identity_credential_resource_name="aks-demoapp",
    issuer=platform.require_output("oidcIssuerUrl"),
    subject=f"system:serviceaccount:{environment}:demoapp-data",
    audiences=["api://AzureADTokenExchange"], opts=opts)
database = pg.Database("database", resource_group_name=rg.name,
    server_name=server.name, database_name="demoapp", charset="UTF8", opts=opts)
firewalls = [pg.FirewallRule(f"allow-{ip.replace('.', '-')}",
    resource_group_name=rg.name, server_name=server.name,
    firewall_rule_name=f"allow-{ip.replace('.', '-')}", start_ip_address=ip,
    end_ip_address=ip, opts=pulumi.ResourceOptions(provider=azure)) for ip in ips]
# Entra principal operations require an accessible server; wait for DB/firewall updates.
admin = pg.Administrator("entra-admin", resource_group_name=rg.name,
    server_name=server.name, object_id=admin_object_id, principal_name=admin_name,
    principal_type=admin_type, tenant_id=platform.require_output("tenantId"), opts=pulumi.ResourceOptions.merge(opts,
        pulumi.ResourceOptions(depends_on=[database, *firewalls])))
account = storage.StorageAccount("photos", resource_group_name=rg.name,
    account_name=suffix.apply(lambda s: f"demophotos{s}"), location=location,
    kind="StorageV2", sku={"name": "Standard_LRS"}, access_tier="Hot",
    allow_blob_public_access=False, allow_shared_key_access=False,
    enable_https_traffic_only=True, minimum_tls_version="TLS1_2",
    public_network_access="Enabled", tags=tags, opts=opts)
blob_service = storage.BlobServiceProperties("blob-recovery",
    resource_group_name=rg.name, account_name=account.name, blob_services_name="default",
    is_versioning_enabled=True,
    delete_retention_policy={"enabled": True, "days": blob_days},
    container_delete_retention_policy={"enabled": True, "days": blob_days}, opts=opts)
container = storage.BlobContainer("challenges", resource_group_name=rg.name,
    account_name=account.name, container_name=f"demoapp-{environment}",
    public_access="None", opts=pulumi.ResourceOptions.merge(opts,
        pulumi.ResourceOptions(depends_on=[blob_service])))
# Grant data-plane access only to this environment's container.
blob_role = authorization.RoleAssignment("app-blob-access", scope=container.id,
    principal_id=identity.principal_id, principal_type="ServicePrincipal",
    role_definition_id=pulumi.Output.concat("/subscriptions/", subscription,
        "/providers/Microsoft.Authorization/roleDefinitions/ba92f5b4-2d11-453d-a403-e96b0029c9fe"),
    role_assignment_name=pulumi.Output.all(container.id, identity.principal_id).apply(
        lambda args: str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join(args)))), opts=opts)
runtime = {
    "DATABASE_URL": server.fully_qualified_domain_name.apply(lambda host:
        f"postgresql://demoapp@{host}:5432/demoapp?sslmode=verify-full&connect_timeout=15"),
    "POSTGRES_AUTH": "entra",
    "AZURE_STORAGE_ACCOUNT_URL": account.name.apply(lambda name: f"https://{name}.blob.core.windows.net"),
    "AZURE_STORAGE_CONTAINER": container.name,
}
pulumi.export("environment", environment)
pulumi.export("resourceGroupName", rg.name)
pulumi.export("clusterName", platform.require_output("clusterName"))
pulumi.export("clusterResourceGroup", platform.require_output("resourceGroupName"))
pulumi.export("postgresHost", server.fully_qualified_domain_name)
pulumi.export("storageAccountName", account.name)
pulumi.export("storageContainer", container.name)
pulumi.export("identityClientId", identity.client_id)
pulumi.export("identityPrincipalId", identity.principal_id)
pulumi.export("tenantId", platform.require_output("tenantId"))
pulumi.export("entraAdminName", admin_name)
pulumi.export("runtime", runtime)
