##############################################################################
# main.tf — Root Terraform configuration
# Project : Legacy-to-Salesforce Migration Platform
# Provider: Azure (azurerm)
# Managed : All core cloud infrastructure
##############################################################################

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.95"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 2.47"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.27"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  backend "azurerm" {
    resource_group_name  = "rg-sfmigration-tfstate"
    storage_account_name = "stsfmigrationtfstate"
    container_name       = "tfstate"
    key                  = "sfmigration.terraform.tfstate"
    use_oidc             = true
  }
}

##############################################################################
# Provider configuration
##############################################################################

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy               = false
      recover_soft_deleted_key_vaults            = true
      purge_soft_deleted_secrets_on_destroy      = false
      recover_soft_deleted_secrets               = true
    }
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
    virtual_machine {
      delete_os_disk_on_deletion = true
    }
  }
  subscription_id = var.subscription_id
  tenant_id       = var.tenant_id
  use_oidc        = true
}

provider "azuread" {
  tenant_id = var.tenant_id
}

##############################################################################
# Data sources
##############################################################################

data "azurerm_client_config" "current" {}

data "azuread_client_config" "current" {}

##############################################################################
# Random suffix for globally unique names
##############################################################################

resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}

##############################################################################
# Resource Group
##############################################################################

resource "azurerm_resource_group" "main" {
  name     = "rg-${var.project}-${var.environment}-${var.location_short}"
  location = var.location

  tags = local.common_tags
}

##############################################################################
# Log Analytics Workspace (central observability)
##############################################################################

resource "azurerm_log_analytics_workspace" "main" {
  name                = "law-${var.project}-${var.environment}-${random_string.suffix.result}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days

  tags = local.common_tags
}

##############################################################################
# Networking module
##############################################################################

module "networking" {
  source = "./modules/networking"

  project             = var.project
  environment         = var.environment
  location            = var.location
  location_short      = var.location_short
  resource_group_name = azurerm_resource_group.main.name
  address_space       = var.vnet_address_space
  aks_subnet_cidr     = var.aks_subnet_cidr
  app_subnet_cidr     = var.app_subnet_cidr
  data_subnet_cidr    = var.data_subnet_cidr
  mgmt_subnet_cidr    = var.mgmt_subnet_cidr
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  common_tags         = local.common_tags
}

##############################################################################
# AKS module
##############################################################################

module "aks" {
  source = "./modules/aks"

  project                    = var.project
  environment                = var.environment
  location                   = var.location
  location_short             = var.location_short
  resource_group_name        = azurerm_resource_group.main.name
  suffix                     = random_string.suffix.result
  kubernetes_version         = var.kubernetes_version
  aks_subnet_id              = module.networking.aks_subnet_id
  system_node_pool_vm_size   = var.system_node_pool_vm_size
  system_node_pool_min_count = var.system_node_pool_min_count
  system_node_pool_max_count = var.system_node_pool_max_count
  user_node_pool_vm_size     = var.user_node_pool_vm_size
  user_node_pool_min_count   = var.user_node_pool_min_count
  user_node_pool_max_count   = var.user_node_pool_max_count
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  admin_group_object_ids     = var.admin_group_object_ids
  common_tags                = local.common_tags
}

##############################################################################
# Key Vault module
##############################################################################

module "keyvault" {
  source = "./modules/keyvault"

  project             = var.project
  environment         = var.environment
  location            = var.location
  location_short      = var.location_short
  resource_group_name = azurerm_resource_group.main.name
  suffix              = random_string.suffix.result
  tenant_id           = var.tenant_id
  aks_kubelet_identity_object_id = module.aks.kubelet_identity_object_id
  log_analytics_workspace_id     = azurerm_log_analytics_workspace.main.id
  private_endpoint_subnet_id     = module.networking.data_subnet_id
  common_tags         = local.common_tags
}

##############################################################################
# Azure Container Registry
##############################################################################

resource "azurerm_container_registry" "main" {
  name                = "acr${var.project}${var.environment}${random_string.suffix.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = var.environment == "prod" ? "Premium" : "Standard"
  admin_enabled       = false

  dynamic "georeplications" {
    for_each = var.environment == "prod" ? var.acr_geo_replication_locations : []
    content {
      location                = georeplications.value
      zone_redundancy_enabled = true
      tags                    = local.common_tags
    }
  }

  network_rule_set {
    default_action = "Deny"

    dynamic "ip_rule" {
      for_each = var.acr_allowed_ip_ranges
      content {
        action   = "Allow"
        ip_range = ip_rule.value
      }
    }
  }

  tags = local.common_tags
}

# Grant AKS kubelet identity pull access to ACR
resource "azurerm_role_assignment" "aks_acr_pull" {
  principal_id                     = module.aks.kubelet_identity_object_id
  role_definition_name             = "AcrPull"
  scope                            = azurerm_container_registry.main.id
  skip_service_principal_aad_check = true
}

##############################################################################
# Azure Service Bus (async messaging for migration pipeline)
##############################################################################

resource "azurerm_servicebus_namespace" "main" {
  name                = "sbns-${var.project}-${var.environment}-${random_string.suffix.result}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = var.environment == "prod" ? "Premium" : "Standard"
  capacity            = var.environment == "prod" ? 1 : 0

  tags = local.common_tags
}

resource "azurerm_servicebus_queue" "migration_jobs" {
  name         = "migration-jobs"
  namespace_id = azurerm_servicebus_namespace.main.id

  enable_dead_lettering_on_message_expiration = true
  max_delivery_count                          = 10
  lock_duration                               = "PT5M"
  default_message_ttl                         = "P7D"
  duplicate_detection_history_time_window     = "PT10M"
  requires_duplicate_detection               = true
}

resource "azurerm_servicebus_queue" "migration_dlq_reprocess" {
  name         = "migration-dlq-reprocess"
  namespace_id = azurerm_servicebus_namespace.main.id

  enable_dead_lettering_on_message_expiration = false
  max_delivery_count                          = 5
  lock_duration                               = "PT5M"
  default_message_ttl                         = "P14D"
}

##############################################################################
# Azure PostgreSQL Flexible Server (staging data / audit logs)
##############################################################################

resource "azurerm_postgresql_flexible_server" "main" {
  name                   = "psql-${var.project}-${var.environment}-${random_string.suffix.result}"
  resource_group_name    = azurerm_resource_group.main.name
  location               = azurerm_resource_group.main.location
  version                = "15"
  delegated_subnet_id    = module.networking.data_subnet_id
  private_dns_zone_id    = azurerm_private_dns_zone.postgres.id
  administrator_login    = var.postgres_admin_username
  administrator_password = random_password.postgres_admin.result
  zone                   = "1"

  storage_mb   = var.postgres_storage_mb
  storage_tier = var.postgres_storage_tier

  sku_name = var.postgres_sku_name

  backup_retention_days        = var.environment == "prod" ? 35 : 7
  geo_redundant_backup_enabled = var.environment == "prod" ? true : false

  high_availability {
    mode                      = var.environment == "prod" ? "ZoneRedundant" : "Disabled"
    standby_availability_zone = var.environment == "prod" ? "2" : null
  }

  maintenance_window {
    day_of_week  = 0
    start_hour   = 2
    start_minute = 0
  }

  tags = local.common_tags

  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres]
}

resource "random_password" "postgres_admin" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "azurerm_key_vault_secret" "postgres_password" {
  name         = "postgres-admin-password"
  value        = random_password.postgres_admin.result
  key_vault_id = module.keyvault.key_vault_id

  tags = local.common_tags

  depends_on = [module.keyvault]
}

resource "azurerm_private_dns_zone" "postgres" {
  name                = "privatelink.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                  = "pdnslink-postgres-${var.environment}"
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  virtual_network_id    = module.networking.vnet_id
  resource_group_name   = azurerm_resource_group.main.name
  registration_enabled  = false
  tags                  = local.common_tags
}

resource "azurerm_postgresql_flexible_server_database" "migration" {
  name      = "sfmigration"
  server_id = azurerm_postgresql_flexible_server.main.id
  collation = "en_US.utf8"
  charset   = "UTF8"
}

##############################################################################
# Azure Redis Cache (distributed caching / dedup)
##############################################################################

resource "azurerm_redis_cache" "main" {
  name                = "redis-${var.project}-${var.environment}-${random_string.suffix.result}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  capacity            = var.redis_capacity
  family              = var.redis_family
  sku_name            = var.redis_sku_name

  enable_non_ssl_port           = false
  minimum_tls_version           = "1.2"
  public_network_access_enabled = false

  redis_configuration {
    maxmemory_reserved              = 50
    maxmemory_delta                 = 50
    maxmemory_policy                = "allkeys-lru"
    rdb_backup_enabled              = var.environment == "prod" ? true : false
    rdb_backup_frequency            = var.environment == "prod" ? 60 : null
    rdb_storage_connection_string   = var.environment == "prod" ? azurerm_storage_account.backups[0].primary_blob_connection_string : null
  }

  tags = local.common_tags
}

##############################################################################
# Storage Account (migration artifacts, exports, backups)
##############################################################################

resource "azurerm_storage_account" "main" {
  name                     = "st${var.project}${var.environment}${random_string.suffix.result}"
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = var.environment == "prod" ? "GRS" : "LRS"
  account_kind             = "StorageV2"
  min_tls_version          = "TLS1_2"

  allow_nested_items_to_be_public  = false
  shared_access_key_enabled        = false
  public_network_access_enabled    = false
  enable_https_traffic_only        = true
  infrastructure_encryption_enabled = true

  blob_properties {
    versioning_enabled       = true
    change_feed_enabled      = true
    last_access_time_enabled = true

    container_delete_retention_policy {
      days = 7
    }
    delete_retention_policy {
      days = 30
    }
  }

  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"]
    virtual_network_subnet_ids = [module.networking.aks_subnet_id]
  }

  tags = local.common_tags
}

resource "azurerm_storage_account" "backups" {
  count = var.environment == "prod" ? 1 : 0

  name                     = "stbkp${var.project}${var.environment}${random_string.suffix.result}"
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "RAGRS"
  account_kind             = "StorageV2"
  min_tls_version          = "TLS1_2"

  allow_nested_items_to_be_public  = false
  shared_access_key_enabled        = false
  public_network_access_enabled    = false
  enable_https_traffic_only        = true

  tags = local.common_tags
}

resource "azurerm_storage_container" "migration_exports" {
  name                  = "migration-exports"
  storage_account_name  = azurerm_storage_account.main.name
  container_access_type = "private"
}

resource "azurerm_storage_container" "migration_staging" {
  name                  = "migration-staging"
  storage_account_name  = azurerm_storage_account.main.name
  container_access_type = "private"
}

resource "azurerm_storage_container" "migration_archive" {
  name                  = "migration-archive"
  storage_account_name  = azurerm_storage_account.main.name
  container_access_type = "private"
}

##############################################################################
# Application Insights
##############################################################################

resource "azurerm_application_insights" "main" {
  name                = "appi-${var.project}-${var.environment}-${random_string.suffix.result}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"

  tags = local.common_tags
}

##############################################################################
# Locals
##############################################################################

locals {
  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    CostCenter  = var.cost_center
    Owner       = var.owner
    CreatedDate = formatdate("YYYY-MM-DD", timestamp())
    Purpose     = "legacy-salesforce-migration"
  }
}
