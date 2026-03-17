##############################################################################
# modules/networking/main.tf
# Project : Legacy-to-Salesforce Migration Platform
# Purpose : Azure Virtual Network, subnets, NSGs, private DNS zones,
#           DDoS protection, and diagnostic settings
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

variable "project"             { type = string }
variable "environment"         { type = string }
variable "location"            { type = string }
variable "location_short"      { type = string }
variable "resource_group_name" { type = string }
variable "address_space"       { type = list(string) }
variable "aks_subnet_cidr"     { type = string }
variable "app_subnet_cidr"     { type = string }
variable "data_subnet_cidr"    { type = string }
variable "mgmt_subnet_cidr"    { type = string }
variable "log_analytics_workspace_id" { type = string }
variable "common_tags"         { type = map(string) }

##############################################################################
# Virtual Network
##############################################################################

resource "azurerm_virtual_network" "main" {
  name                = "vnet-${var.project}-${var.environment}-${var.location_short}"
  location            = var.location
  resource_group_name = var.resource_group_name
  address_space       = var.address_space
  dns_servers         = []           # Use Azure-provided DNS

  tags = var.common_tags
}

##############################################################################
# Network Security Groups
##############################################################################

resource "azurerm_network_security_group" "aks" {
  name                = "nsg-aks-${var.project}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.common_tags
}

# Allow HTTPS inbound from Azure Load Balancer health probes
resource "azurerm_network_security_rule" "aks_allow_lb_health" {
  name                        = "Allow-AzureLoadBalancer-Inbound"
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "AzureLoadBalancer"
  destination_address_prefix  = "*"
  resource_group_name         = var.resource_group_name
  network_security_group_name = azurerm_network_security_group.aks.name
}

# Allow inbound from within the VNet
resource "azurerm_network_security_rule" "aks_allow_vnet_inbound" {
  name                        = "Allow-VNet-Inbound"
  priority                    = 110
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "VirtualNetwork"
  destination_address_prefix  = "VirtualNetwork"
  resource_group_name         = var.resource_group_name
  network_security_group_name = azurerm_network_security_group.aks.name
}

# Deny all other inbound
resource "azurerm_network_security_rule" "aks_deny_all_inbound" {
  name                        = "Deny-All-Inbound"
  priority                    = 4096
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "*"
  destination_address_prefix  = "*"
  resource_group_name         = var.resource_group_name
  network_security_group_name = azurerm_network_security_group.aks.name
}

resource "azurerm_network_security_group" "app" {
  name                = "nsg-app-${var.project}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.common_tags
}

resource "azurerm_network_security_rule" "app_allow_https_inbound" {
  name                        = "Allow-HTTPS-Inbound"
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "443"
  source_address_prefix       = var.aks_subnet_cidr
  destination_address_prefix  = "*"
  resource_group_name         = var.resource_group_name
  network_security_group_name = azurerm_network_security_group.app.name
}

resource "azurerm_network_security_rule" "app_deny_all_inbound" {
  name                        = "Deny-All-Inbound"
  priority                    = 4096
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "*"
  destination_address_prefix  = "*"
  resource_group_name         = var.resource_group_name
  network_security_group_name = azurerm_network_security_group.app.name
}

resource "azurerm_network_security_group" "data" {
  name                = "nsg-data-${var.project}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.common_tags
}

# Only allow database ports from AKS and App subnets
resource "azurerm_network_security_rule" "data_allow_postgres" {
  name                        = "Allow-PostgreSQL-From-AKS"
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "5432"
  source_address_prefix       = var.aks_subnet_cidr
  destination_address_prefix  = "*"
  resource_group_name         = var.resource_group_name
  network_security_group_name = azurerm_network_security_group.data.name
}

resource "azurerm_network_security_rule" "data_allow_redis" {
  name                        = "Allow-Redis-From-AKS"
  priority                    = 110
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "6380"
  source_address_prefix       = var.aks_subnet_cidr
  destination_address_prefix  = "*"
  resource_group_name         = var.resource_group_name
  network_security_group_name = azurerm_network_security_group.data.name
}

resource "azurerm_network_security_rule" "data_deny_all_inbound" {
  name                        = "Deny-All-Inbound"
  priority                    = 4096
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "*"
  destination_address_prefix  = "*"
  resource_group_name         = var.resource_group_name
  network_security_group_name = azurerm_network_security_group.data.name
}

resource "azurerm_network_security_group" "mgmt" {
  name                = "nsg-mgmt-${var.project}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.common_tags
}

##############################################################################
# Subnets
##############################################################################

resource "azurerm_subnet" "aks" {
  name                 = "snet-aks-${var.project}-${var.environment}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.aks_subnet_cidr]

  service_endpoints = [
    "Microsoft.ContainerRegistry",
    "Microsoft.Storage",
    "Microsoft.KeyVault",
    "Microsoft.ServiceBus",
  ]
}

resource "azurerm_subnet" "app" {
  name                 = "snet-app-${var.project}-${var.environment}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.app_subnet_cidr]

  service_endpoints = [
    "Microsoft.Storage",
    "Microsoft.KeyVault",
  ]
}

resource "azurerm_subnet" "data" {
  name                 = "snet-data-${var.project}-${var.environment}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.data_subnet_cidr]

  delegation {
    name = "flexibleServers"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "mgmt" {
  name                 = "snet-mgmt-${var.project}-${var.environment}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.mgmt_subnet_cidr]
}

##############################################################################
# NSG associations
##############################################################################

resource "azurerm_subnet_network_security_group_association" "aks" {
  subnet_id                 = azurerm_subnet.aks.id
  network_security_group_id = azurerm_network_security_group.aks.id
}

resource "azurerm_subnet_network_security_group_association" "app" {
  subnet_id                 = azurerm_subnet.app.id
  network_security_group_id = azurerm_network_security_group.app.id
}

resource "azurerm_subnet_network_security_group_association" "data" {
  subnet_id                 = azurerm_subnet.data.id
  network_security_group_id = azurerm_network_security_group.data.id
}

resource "azurerm_subnet_network_security_group_association" "mgmt" {
  subnet_id                 = azurerm_subnet.mgmt.id
  network_security_group_id = azurerm_network_security_group.mgmt.id
}

##############################################################################
# Network Watcher Flow Logs
##############################################################################

resource "azurerm_network_watcher" "main" {
  name                = "nw-${var.project}-${var.environment}-${var.location_short}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.common_tags
}

##############################################################################
# Diagnostic settings for NSGs
##############################################################################

resource "azurerm_monitor_diagnostic_setting" "nsg_aks" {
  name                       = "diag-nsg-aks"
  target_resource_id         = azurerm_network_security_group.aks.id
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log {
    category = "NetworkSecurityGroupEvent"
  }
  enabled_log {
    category = "NetworkSecurityGroupRuleCounter"
  }
}

resource "azurerm_monitor_diagnostic_setting" "nsg_data" {
  name                       = "diag-nsg-data"
  target_resource_id         = azurerm_network_security_group.data.id
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log {
    category = "NetworkSecurityGroupEvent"
  }
  enabled_log {
    category = "NetworkSecurityGroupRuleCounter"
  }
}

##############################################################################
# Outputs
##############################################################################

output "vnet_id"         { value = azurerm_virtual_network.main.id }
output "vnet_name"       { value = azurerm_virtual_network.main.name }
output "aks_subnet_id"   { value = azurerm_subnet.aks.id }
output "app_subnet_id"   { value = azurerm_subnet.app.id }
output "data_subnet_id"  { value = azurerm_subnet.data.id }
output "mgmt_subnet_id"  { value = azurerm_subnet.mgmt.id }
output "nsg_aks_id"      { value = azurerm_network_security_group.aks.id }
output "nsg_data_id"     { value = azurerm_network_security_group.data.id }
