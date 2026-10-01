"""Validate environment boundaries before registering cloud resources."""
from ipaddress import ip_address


def validate(stack, ips, storage_gb, backup_days, blob_days, sku, tier):
    if stack not in {"staging", "production"}:
        raise ValueError("Use a staging or production stack to match the Helm namespaces")
    if not isinstance(ips, list) or not ips:
        raise ValueError("postgresAllowedIps must contain AKS egress and bootstrap IPv4 addresses")
    for value in ips:
        address = ip_address(value)
        if address.version != 4 or not address.is_global:
            raise ValueError("postgresAllowedIps must contain individual public IPv4 addresses")
    if len(set(ips)) != len(ips):
        raise ValueError("postgresAllowedIps must not contain duplicates")
    if storage_gb not in {2 ** exponent for exponent in range(5, 16)}:
        raise ValueError("postgresStorageGb must be a power of two between 32 and 32768 (Premium SSD)")
    if not 7 <= backup_days <= 35 or not 1 <= blob_days <= 365:
        raise ValueError("Backup retention must be 7–35 days; blob retention 1–365 days")
    if tier not in {"Burstable", "GeneralPurpose", "MemoryOptimized"} or not sku.startswith("Standard_"):
        raise ValueError("Provide a supported Azure PostgreSQL SKU and tier")
