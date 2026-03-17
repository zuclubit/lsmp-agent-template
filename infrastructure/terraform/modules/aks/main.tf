##############################################################################
# modules/aks/main.tf
# Project : Legacy-to-Salesforce Migration Platform
# Purpose : Azure Kubernetes Service cluster with system + user node pools,
#           AAD RBAC, workload identity, OIDC, monitoring, and autoscaler
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

variable "project"                    { type = string }
variable "environment"                { type = string }
variable "location"                   { type = string }
variable "location_short"             { type = string }
variable "resource_group_name"        { type = string }
variable "suffix"                     { type = string }
variable "kubernetes_version"         { type = string }
variable "aks_subnet_id"              { type = string }
variable "system_node_pool_vm_size"   { type = string }
variable "system_node_pool_min_count" { type = number }
variable "system_node_pool_max_count" { type = number }
variable "user_node_pool_vm_size"     { type = string }
variable "user_node_pool_min_count"   { type = number }
variable "user_node_pool_max_count"   { type = number }
variable "log_analytics_workspace_id" { type = string }
variable "admin_group_object_ids"     { type = list(string) }
variable "common_tags"                { type = map(string) }

##############################################################################
# User-Assigned Managed Identity for AKS control plane
##############################################################################

resource "azurerm_user_assigned_identity" "aks_control_plane" {
  name                = "id-aks-cp-${var.project}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.common_tags
}

# Allow AKS control plane to manage network resources in its subnet
resource "azurerm_role_assignment" "aks_network_contributor" {
  principal_id         = azurerm_user_assigned_identity.aks_control_plane.principal_id
  role_definition_name = "Network Contributor"
  scope                = var.aks_subnet_id
}

##############################################################################
# AKS Cluster
##############################################################################

resource "azurerm_kubernetes_cluster" "main" {
  name                             = "aks-${var.project}-${var.environment}-${var.location_short}"
  location                         = var.location
  resource_group_name              = var.resource_group_name
  dns_prefix_private_cluster       = "${var.project}-${var.environment}"
  kubernetes_version               = var.kubernetes_version
  node_resource_group              = "rg-${var.project}-${var.environment}-aks-nodes"
  private_cluster_enabled          = true
  private_cluster_public_fqdn_enabled = false
  sku_tier                         = var.environment == "prod" ? "Standard" : "Free"
  workload_identity_enabled        = true
  oidc_issuer_enabled              = true
  image_cleaner_enabled            = true
  image_cleaner_interval_hours     = 48
  run_command_enabled              = false   # Security: disable run command

  # Control plane identity
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.aks_control_plane.id]
  }

  # System node pool (critical system pods only)
  default_node_pool {
    name                        = "system"
    vm_size                     = var.system_node_pool_vm_size
    vnet_subnet_id              = var.aks_subnet_id
    os_disk_size_gb             = 128
    os_disk_type                = "Ephemeral"
    os_sku                      = "AzureLinux"
    enable_auto_scaling         = true
    min_count                   = var.system_node_pool_min_count
    max_count                   = var.system_node_pool_max_count
    max_pods                    = 110
    only_critical_addons_enabled = true
    zones                       = ["1", "2", "3"]
    node_labels = {
      "role"              = "system"
      "workload-type"     = "system"
    }
    node_taints = ["CriticalAddonsOnly=true:NoSchedule"]

    upgrade_settings {
      max_surge = "33%"
    }

    tags = var.common_tags
  }

  # AAD RBAC
  azure_active_directory_role_based_access_control {
    managed                = true
    azure_rbac_enabled     = true
    admin_group_object_ids = var.admin_group_object_ids
  }

  # Networking — Azure CNI for predictable pod IP ranges
  network_profile {
    network_plugin      = "azure"
    network_policy      = "calico"
    load_balancer_sku   = "standard"
    outbound_type       = "loadBalancer"
    service_cidr        = "172.16.0.0/16"
    dns_service_ip      = "172.16.0.10"
  }

  # OMS agent for Azure Monitor
  oms_agent {
    log_analytics_workspace_id      = var.log_analytics_workspace_id
    msi_auth_for_monitoring_enabled = true
  }

  # Key Vault Secrets Provider (CSI driver)
  key_vault_secrets_provider {
    secret_rotation_enabled  = true
    secret_rotation_interval = "2m"
  }

  # Azure Policy add-on
  azure_policy_enabled = true

  # HTTP application routing — disabled in prod, use nginx ingress
  http_application_routing_enabled = false

  # Maintenance window — weekends 02:00–06:00 UTC
  maintenance_window {
    allowed {
      day   = "Saturday"
      hours = [2, 3, 4, 5]
    }
    allowed {
      day   = "Sunday"
      hours = [2, 3, 4, 5]
    }
  }

  auto_scaler_profile {
    balance_similar_node_groups      = true
    expander                         = "random"
    max_graceful_termination_sec     = 600
    max_node_provisioning_time       = "15m"
    max_unready_nodes                = 3
    max_unready_percentage           = 45
    new_pod_scale_up_delay           = "10s"
    scale_down_delay_after_add       = "10m"
    scale_down_delay_after_delete    = "10s"
    scale_down_delay_after_failure   = "3m"
    scale_down_unneeded              = "10m"
    scale_down_unready               = "20m"
    scale_down_utilization_threshold = "0.5"
    scan_interval                    = "10s"
    skip_nodes_with_local_storage    = true
    skip_nodes_with_system_pods      = true
  }

  tags = var.common_tags

  lifecycle {
    ignore_changes = [
      default_node_pool[0].node_count,   # managed by autoscaler
      kubernetes_version,                 # managed via upgrades
    ]
  }

  depends_on = [
    azurerm_role_assignment.aks_network_contributor,
  ]
}

##############################################################################
# User (workload) node pool — migration services
##############################################################################

resource "azurerm_kubernetes_cluster_node_pool" "migration" {
  name                  = "migration"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.main.id
  vm_size               = var.user_node_pool_vm_size
  vnet_subnet_id        = var.aks_subnet_id
  os_disk_size_gb       = 256
  os_disk_type          = "Managed"
  os_sku                = "AzureLinux"
  enable_auto_scaling   = true
  min_count             = var.user_node_pool_min_count
  max_count             = var.user_node_pool_max_count
  max_pods              = 60
  zones                 = ["1", "2", "3"]
  mode                  = "User"

  node_labels = {
    "role"          = "migration-workload"
    "workload-type" = "migration"
  }

  node_taints = ["workload=migration:NoSchedule"]

  upgrade_settings {
    max_surge = "33%"
  }

  tags = var.common_tags

  lifecycle {
    ignore_changes = [node_count]
  }
}

##############################################################################
# Diagnostic settings
##############################################################################

resource "azurerm_monitor_diagnostic_setting" "aks" {
  name                       = "diag-aks-${var.project}-${var.environment}"
  target_resource_id         = azurerm_kubernetes_cluster.main.id
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log { category = "kube-apiserver" }
  enabled_log { category = "kube-audit" }
  enabled_log { category = "kube-audit-admin" }
  enabled_log { category = "kube-controller-manager" }
  enabled_log { category = "kube-scheduler" }
  enabled_log { category = "cluster-autoscaler" }
  enabled_log { category = "cloud-controller-manager" }
  enabled_log { category = "guard" }
  enabled_log { category = "csi-azuredisk-controller" }
  enabled_log { category = "csi-azurefile-controller" }
  enabled_log { category = "csi-snapshot-controller" }

  metric {
    category = "AllMetrics"
    enabled  = true
  }
}

##############################################################################
# Outputs
##############################################################################

output "cluster_name"               { value = azurerm_kubernetes_cluster.main.name }
output "cluster_id"                 { value = azurerm_kubernetes_cluster.main.id }
output "cluster_fqdn"               { value = azurerm_kubernetes_cluster.main.fqdn }
output "cluster_private_fqdn"       { value = azurerm_kubernetes_cluster.main.private_fqdn }
output "oidc_issuer_url"            { value = azurerm_kubernetes_cluster.main.oidc_issuer_url }
output "node_resource_group"        { value = azurerm_kubernetes_cluster.main.node_resource_group }
output "kubelet_identity_client_id" { value = azurerm_kubernetes_cluster.main.kubelet_identity[0].client_id }
output "kubelet_identity_object_id" { value = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id }
output "kube_config_raw"            {
  value     = azurerm_kubernetes_cluster.main.kube_config_raw
  sensitive = true
}
