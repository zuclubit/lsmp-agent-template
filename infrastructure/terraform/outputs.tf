##############################################################################
# outputs.tf — Terraform output values
# Project : Legacy-to-Salesforce Migration Platform
# NOTE    : No secrets or passwords are exposed. Connection strings reference
#           Key Vault secret URIs only.
##############################################################################

##############################################################################
# Resource Group
##############################################################################

output "resource_group_name" {
  description = "Name of the primary resource group"
  value       = azurerm_resource_group.main.name
}

output "resource_group_id" {
  description = "Resource ID of the primary resource group"
  value       = azurerm_resource_group.main.id
}

output "resource_group_location" {
  description = "Azure region where the resource group is deployed"
  value       = azurerm_resource_group.main.location
}

##############################################################################
# Networking
##############################################################################

output "vnet_id" {
  description = "Resource ID of the Virtual Network"
  value       = module.networking.vnet_id
}

output "vnet_name" {
  description = "Name of the Virtual Network"
  value       = module.networking.vnet_name
}

output "aks_subnet_id" {
  description = "Resource ID of the AKS node subnet"
  value       = module.networking.aks_subnet_id
}

output "app_subnet_id" {
  description = "Resource ID of the application subnet"
  value       = module.networking.app_subnet_id
}

output "data_subnet_id" {
  description = "Resource ID of the data tier subnet"
  value       = module.networking.data_subnet_id
}

##############################################################################
# AKS Cluster
##############################################################################

output "aks_cluster_name" {
  description = "Name of the AKS cluster"
  value       = module.aks.cluster_name
}

output "aks_cluster_id" {
  description = "Resource ID of the AKS cluster"
  value       = module.aks.cluster_id
}

output "aks_cluster_fqdn" {
  description = "Fully qualified domain name of the AKS API server"
  value       = module.aks.cluster_fqdn
}

output "aks_cluster_private_fqdn" {
  description = "Private FQDN of the AKS API server (private cluster)"
  value       = module.aks.cluster_private_fqdn
}

output "aks_oidc_issuer_url" {
  description = "OIDC issuer URL for workload identity federation"
  value       = module.aks.oidc_issuer_url
}

output "aks_kubelet_identity_client_id" {
  description = "Client ID of the AKS kubelet managed identity"
  value       = module.aks.kubelet_identity_client_id
}

output "aks_kubelet_identity_object_id" {
  description = "Object ID of the AKS kubelet managed identity"
  value       = module.aks.kubelet_identity_object_id
}

output "aks_node_resource_group" {
  description = "Name of the auto-generated AKS node resource group"
  value       = module.aks.node_resource_group
}

##############################################################################
# Container Registry
##############################################################################

output "acr_name" {
  description = "Name of the Azure Container Registry"
  value       = azurerm_container_registry.main.name
}

output "acr_login_server" {
  description = "Login server (FQDN) of the Azure Container Registry"
  value       = azurerm_container_registry.main.login_server
}

output "acr_id" {
  description = "Resource ID of the Azure Container Registry"
  value       = azurerm_container_registry.main.id
}

##############################################################################
# Key Vault (URIs only — no secrets)
##############################################################################

output "key_vault_name" {
  description = "Name of the Azure Key Vault"
  value       = module.keyvault.key_vault_name
}

output "key_vault_id" {
  description = "Resource ID of the Azure Key Vault"
  value       = module.keyvault.key_vault_id
}

output "key_vault_uri" {
  description = "URI of the Azure Key Vault (for SDK/SDK references)"
  value       = module.keyvault.key_vault_uri
}

output "postgres_password_secret_uri" {
  description = "Key Vault secret URI for the PostgreSQL administrator password (no value exposed)"
  value       = azurerm_key_vault_secret.postgres_password.versionless_id
  sensitive   = false
}

##############################################################################
# PostgreSQL
##############################################################################

output "postgres_server_name" {
  description = "Name of the PostgreSQL Flexible Server"
  value       = azurerm_postgresql_flexible_server.main.name
}

output "postgres_fqdn" {
  description = "Fully qualified domain name of the PostgreSQL server"
  value       = azurerm_postgresql_flexible_server.main.fqdn
}

output "postgres_database_name" {
  description = "Name of the migration database"
  value       = azurerm_postgresql_flexible_server_database.migration.name
}

##############################################################################
# Redis Cache
##############################################################################

output "redis_cache_name" {
  description = "Name of the Azure Redis Cache instance"
  value       = azurerm_redis_cache.main.name
}

output "redis_cache_hostname" {
  description = "Hostname of the Redis Cache (private endpoint)"
  value       = azurerm_redis_cache.main.hostname
}

output "redis_cache_ssl_port" {
  description = "SSL port for Redis Cache connections"
  value       = azurerm_redis_cache.main.ssl_port
}

##############################################################################
# Service Bus
##############################################################################

output "servicebus_namespace_name" {
  description = "Name of the Azure Service Bus namespace"
  value       = azurerm_servicebus_namespace.main.name
}

output "servicebus_namespace_fqdn" {
  description = "FQDN of the Service Bus namespace endpoint"
  value       = "${azurerm_servicebus_namespace.main.name}.servicebus.windows.net"
}

output "servicebus_migration_jobs_queue_name" {
  description = "Name of the migration jobs Service Bus queue"
  value       = azurerm_servicebus_queue.migration_jobs.name
}

##############################################################################
# Storage
##############################################################################

output "storage_account_name" {
  description = "Name of the primary storage account"
  value       = azurerm_storage_account.main.name
}

output "storage_account_id" {
  description = "Resource ID of the primary storage account"
  value       = azurerm_storage_account.main.id
}

output "storage_migration_exports_container" {
  description = "Name of the migration exports blob container"
  value       = azurerm_storage_container.migration_exports.name
}

output "storage_migration_staging_container" {
  description = "Name of the migration staging blob container"
  value       = azurerm_storage_container.migration_staging.name
}

##############################################################################
# Observability
##############################################################################

output "log_analytics_workspace_id" {
  description = "Resource ID of the Log Analytics Workspace"
  value       = azurerm_log_analytics_workspace.main.id
}

output "log_analytics_workspace_name" {
  description = "Name of the Log Analytics Workspace"
  value       = azurerm_log_analytics_workspace.main.name
}

output "application_insights_name" {
  description = "Name of the Application Insights instance"
  value       = azurerm_application_insights.main.name
}

output "application_insights_instrumentation_key_secret_name" {
  description = "Name of the Key Vault secret storing the App Insights instrumentation key"
  value       = "appinsights-instrumentation-key"
}

output "application_insights_connection_string_secret_name" {
  description = "Name of the Key Vault secret storing the App Insights connection string"
  value       = "appinsights-connection-string"
}
