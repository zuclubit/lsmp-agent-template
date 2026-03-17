##############################################################################
# modules/keyvault/main.tf
# Project : Legacy-to-Salesforce Migration Platform
# Purpose : Azure Key Vault with RBAC, private endpoint, diagnostic logging,
#           and soft-delete/purge-protection enforcement
##############################################################################

terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.95"
    }
  }
}

##############################################################################
# Variables
##############################################################################

variable "project"                         { type = string }
variable "environment"                     { type = string }
variable "location"                        { type = string }
variable "location_short"                  { type = string }
variable "resource_group_name"             { type = string }
variable "suffix"                          { type = string }
variable "tenant_id"                       { type = string }
variable "aks_kubelet_identity_object_id"  { type = string }
variable "log_analytics_workspace_id"      { type = string }
variable "private_endpoint_subnet_id"      { type = string }
variable "common_tags"                     { type = map(string) }

##############################################################################
# Data sources
##############################################################################

data "azurerm_client_config" "current" {}

##############################################################################
# Azure Key Vault
##############################################################################

resource "azurerm_key_vault" "main" {
  name                            = "kv-${var.project}-${var.environment}-${var.suffix}"
  location                        = var.location
  resource_group_name             = var.resource_group_name
  tenant_id                       = var.tenant_id
  sku_name                        = "premium"   # HSM-backed keys in prod
  soft_delete_retention_days      = 90
  purge_protection_enabled        = true
  enable_rbac_authorization       = true        # Use RBAC, not access policies
  public_network_access_enabled   = false       # Private endpoint only

  network_acls {
    bypass                     = "AzureServices"
    default_action             = "Deny"
    ip_rules                   = []
    virtual_network_subnet_ids = []
  }

  tags = var.common_tags
}

##############################################################################
# RBAC role assignments
##############################################################################

# Terraform service principal / managed identity gets Key Vault Admin during provisioning
resource "azurerm_role_assignment" "terraform_kv_admin" {
  principal_id         = data.azurerm_client_config.current.object_id
  role_definition_name = "Key Vault Administrator"
  scope                = azurerm_key_vault.main.id
}

# AKS kubelet identity gets secrets read access (for CSI driver)
resource "azurerm_role_assignment" "aks_kv_secrets_user" {
  principal_id                     = var.aks_kubelet_identity_object_id
  role_definition_name             = "Key Vault Secrets User"
  scope                            = azurerm_key_vault.main.id
  skip_service_principal_aad_check = true
}

##############################################################################
# Private DNS Zone for Key Vault
##############################################################################

resource "azurerm_private_dns_zone" "keyvault" {
  name                = "privatelink.vaultcore.azure.net"
  resource_group_name = var.resource_group_name
  tags                = var.common_tags
}

##############################################################################
# Private Endpoint
##############################################################################

resource "azurerm_private_endpoint" "keyvault" {
  name                = "pep-kv-${var.project}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "psc-kv-${var.project}-${var.environment}"
    private_connection_resource_id = azurerm_key_vault.main.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "pdnszg-kv"
    private_dns_zone_ids = [azurerm_private_dns_zone.keyvault.id]
  }

  tags = var.common_tags
}

##############################################################################
# Encryption key for storage / disk encryption (RSA-HSM 4096 in prod)
##############################################################################

resource "azurerm_key_vault_key" "migration_data_key" {
  name         = "migration-data-key"
  key_vault_id = azurerm_key_vault.main.id
  key_type     = "RSA"
  key_size     = 4096
  key_opts     = ["encrypt", "decrypt", "sign", "verify", "wrapKey", "unwrapKey"]

  rotation_policy {
    automatic {
      time_before_expiry = "P30D"
    }
    expire_after         = "P365D"
    notify_before_expiry = "P29D"
  }

  tags = var.common_tags

  depends_on = [azurerm_role_assignment.terraform_kv_admin]
}

##############################################################################
# Diagnostic settings
##############################################################################

resource "azurerm_monitor_diagnostic_setting" "keyvault" {
  name                       = "diag-kv-${var.project}-${var.environment}"
  target_resource_id         = azurerm_key_vault.main.id
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log { category = "AuditEvent" }
  enabled_log { category = "AzurePolicyEvaluationDetails" }

  metric {
    category = "AllMetrics"
    enabled  = true
  }
}

##############################################################################
# Outputs
##############################################################################

output "key_vault_id"   { value = azurerm_key_vault.main.id }
output "key_vault_name" { value = azurerm_key_vault.main.name }
output "key_vault_uri"  { value = azurerm_key_vault.main.vault_uri }
output "data_key_id"    { value = azurerm_key_vault_key.migration_data_key.id }
output "private_endpoint_id" { value = azurerm_private_endpoint.keyvault.id }
