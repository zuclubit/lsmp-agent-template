##############################################################################
# variables.tf — Input variable definitions
# Project : Legacy-to-Salesforce Migration Platform
##############################################################################

##############################################################################
# Core identity & subscription
##############################################################################

variable "subscription_id" {
  description = "Azure Subscription ID where resources will be deployed"
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.subscription_id))
    error_message = "subscription_id must be a valid UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)."
  }
}

variable "tenant_id" {
  description = "Azure Active Directory Tenant ID"
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.tenant_id))
    error_message = "tenant_id must be a valid UUID."
  }
}

##############################################################################
# Project metadata
##############################################################################

variable "project" {
  description = "Short project identifier used in resource naming (lowercase alphanumeric, max 12 chars)"
  type        = string
  default     = "sfmigration"

  validation {
    condition     = can(regex("^[a-z0-9]{3,12}$", var.project))
    error_message = "project must be 3–12 lowercase alphanumeric characters."
  }
}

variable "environment" {
  description = "Deployment environment. Must be one of: dev, staging, prod"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "cost_center" {
  description = "Cost center code for billing allocation"
  type        = string
  default     = "CC-MIGRATION-001"
}

variable "owner" {
  description = "Team or individual responsible for these resources (email or alias)"
  type        = string
  default     = "platform-engineering@company.com"
}

##############################################################################
# Azure regions
##############################################################################

variable "location" {
  description = "Primary Azure region for resource deployment"
  type        = string
  default     = "eastus2"

  validation {
    condition = contains([
      "eastus", "eastus2", "westus", "westus2", "westus3",
      "centralus", "northcentralus", "southcentralus",
      "westeurope", "northeurope", "uksouth", "ukwest",
      "australiaeast", "australiasoutheast",
      "southeastasia", "eastasia"
    ], var.location)
    error_message = "location must be a valid Azure region identifier."
  }
}

variable "location_short" {
  description = "Short abbreviation of the primary region used in resource names (e.g. eus2, weu)"
  type        = string
  default     = "eus2"

  validation {
    condition     = can(regex("^[a-z0-9]{2,6}$", var.location_short))
    error_message = "location_short must be 2–6 lowercase alphanumeric characters."
  }
}

variable "acr_geo_replication_locations" {
  description = "List of Azure regions for ACR geo-replication (prod only)"
  type        = list(string)
  default     = ["westus2"]
}

##############################################################################
# Networking
##############################################################################

variable "vnet_address_space" {
  description = "CIDR block(s) for the Virtual Network"
  type        = list(string)
  default     = ["10.10.0.0/16"]

  validation {
    condition     = alltrue([for cidr in var.vnet_address_space : can(cidrnetmask(cidr))])
    error_message = "All entries in vnet_address_space must be valid CIDR blocks."
  }
}

variable "aks_subnet_cidr" {
  description = "CIDR for the AKS node subnet"
  type        = string
  default     = "10.10.1.0/23"

  validation {
    condition     = can(cidrnetmask(var.aks_subnet_cidr))
    error_message = "aks_subnet_cidr must be a valid CIDR block."
  }
}

variable "app_subnet_cidr" {
  description = "CIDR for the application integration subnet"
  type        = string
  default     = "10.10.4.0/24"

  validation {
    condition     = can(cidrnetmask(var.app_subnet_cidr))
    error_message = "app_subnet_cidr must be a valid CIDR block."
  }
}

variable "data_subnet_cidr" {
  description = "CIDR for the data tier subnet (PostgreSQL, Redis private endpoints)"
  type        = string
  default     = "10.10.5.0/24"

  validation {
    condition     = can(cidrnetmask(var.data_subnet_cidr))
    error_message = "data_subnet_cidr must be a valid CIDR block."
  }
}

variable "mgmt_subnet_cidr" {
  description = "CIDR for management / bastion subnet"
  type        = string
  default     = "10.10.6.0/28"

  validation {
    condition     = can(cidrnetmask(var.mgmt_subnet_cidr))
    error_message = "mgmt_subnet_cidr must be a valid CIDR block."
  }
}

variable "acr_allowed_ip_ranges" {
  description = "IP ranges allowed to access Azure Container Registry"
  type        = list(string)
  default     = []
}

##############################################################################
# AKS cluster
##############################################################################

variable "kubernetes_version" {
  description = "Kubernetes version for AKS (must be a supported AKS version)"
  type        = string
  default     = "1.29.2"

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.kubernetes_version))
    error_message = "kubernetes_version must follow semver format (e.g. 1.29.2)."
  }
}

variable "system_node_pool_vm_size" {
  description = "VM size for the AKS system node pool"
  type        = string
  default     = "Standard_D4ds_v5"
}

variable "system_node_pool_min_count" {
  description = "Minimum node count for the system node pool (autoscaler lower bound)"
  type        = number
  default     = 2

  validation {
    condition     = var.system_node_pool_min_count >= 1
    error_message = "system_node_pool_min_count must be at least 1."
  }
}

variable "system_node_pool_max_count" {
  description = "Maximum node count for the system node pool (autoscaler upper bound)"
  type        = number
  default     = 5

  validation {
    condition     = var.system_node_pool_max_count >= var.system_node_pool_min_count
    error_message = "system_node_pool_max_count must be >= system_node_pool_min_count."
  }
}

variable "user_node_pool_vm_size" {
  description = "VM size for the AKS user (workload) node pool"
  type        = string
  default     = "Standard_D8ds_v5"
}

variable "user_node_pool_min_count" {
  description = "Minimum node count for the user node pool"
  type        = number
  default     = 2

  validation {
    condition     = var.user_node_pool_min_count >= 0
    error_message = "user_node_pool_min_count must be >= 0."
  }
}

variable "user_node_pool_max_count" {
  description = "Maximum node count for the user node pool"
  type        = number
  default     = 10

  validation {
    condition     = var.user_node_pool_max_count >= var.user_node_pool_min_count
    error_message = "user_node_pool_max_count must be >= user_node_pool_min_count."
  }
}

variable "admin_group_object_ids" {
  description = "List of Azure AD group object IDs that will have AKS cluster admin access"
  type        = list(string)
  default     = []
}

##############################################################################
# PostgreSQL Flexible Server
##############################################################################

variable "postgres_admin_username" {
  description = "Administrator username for PostgreSQL Flexible Server"
  type        = string
  default     = "sfmigrationadmin"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{2,62}$", var.postgres_admin_username))
    error_message = "postgres_admin_username must start with a letter and be 3–63 alphanumeric/underscore characters."
  }
}

variable "postgres_sku_name" {
  description = "SKU name for PostgreSQL Flexible Server (e.g. GP_Standard_D4s_v3)"
  type        = string
  default     = "GP_Standard_D4s_v3"
}

variable "postgres_storage_mb" {
  description = "Storage size in MB for PostgreSQL Flexible Server"
  type        = number
  default     = 65536

  validation {
    condition     = contains([32768, 65536, 131072, 262144, 524288, 1048576, 2097152, 4193280, 4194304, 8388608, 16777216], var.postgres_storage_mb)
    error_message = "postgres_storage_mb must be one of the supported Azure PostgreSQL storage sizes."
  }
}

variable "postgres_storage_tier" {
  description = "Performance tier for PostgreSQL storage"
  type        = string
  default     = "P30"
}

##############################################################################
# Redis Cache
##############################################################################

variable "redis_capacity" {
  description = "Redis cache capacity (size within the family/SKU)"
  type        = number
  default     = 1

  validation {
    condition     = var.redis_capacity >= 0 && var.redis_capacity <= 6
    error_message = "redis_capacity must be between 0 and 6."
  }
}

variable "redis_family" {
  description = "Redis cache family. C = Basic/Standard, P = Premium"
  type        = string
  default     = "C"

  validation {
    condition     = contains(["C", "P"], var.redis_family)
    error_message = "redis_family must be 'C' (Basic/Standard) or 'P' (Premium)."
  }
}

variable "redis_sku_name" {
  description = "Redis cache SKU: Basic, Standard, or Premium"
  type        = string
  default     = "Standard"

  validation {
    condition     = contains(["Basic", "Standard", "Premium"], var.redis_sku_name)
    error_message = "redis_sku_name must be one of: Basic, Standard, Premium."
  }
}

##############################################################################
# Observability
##############################################################################

variable "log_retention_days" {
  description = "Number of days to retain logs in Log Analytics Workspace"
  type        = number
  default     = 90

  validation {
    condition     = var.log_retention_days >= 30 && var.log_retention_days <= 730
    error_message = "log_retention_days must be between 30 and 730."
  }
}
