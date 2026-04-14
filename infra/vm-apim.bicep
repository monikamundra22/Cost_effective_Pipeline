// ============================================================================
// vm-apim.bicep – Azure VM and API Management
// ============================================================================

targetScope = 'resourceGroup'

// ─── Parameters ─────────────────────────────────────────────────────────────

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('VM size (e.g. Standard_B2s)')
param vmSize string = 'Standard_B2s'

@description('VM operating system')
@allowed(['linux', 'windows'])
param vmOs string = 'linux'

@description('VM admin username')
param adminUsername string = 'azureuser'

@description('VM admin password or SSH public key')
@secure()
param adminPasswordOrKey string

@description('API Management SKU')
@allowed(['Consumption', 'Developer', 'Basic', 'Standard', 'Premium'])
param apimSku string = 'Developer'

@description('Number of APIM scale units')
@minValue(1)
param apimUnits int = 1

@description('APIM publisher email')
param apimPublisherEmail string = 'admin@contoso.com'

@description('APIM publisher name')
param apimPublisherName string = 'Contoso'

@description('Tags applied to every resource')
param tags object = {}

// ─── Variables ──────────────────────────────────────────────────────────────

var uniqueSuffix = uniqueString(resourceGroup().id)
var vmName = 'vm-${uniqueSuffix}'
var nicName = 'nic-${vmName}'
var vnetName = 'vnet-${uniqueSuffix}'
var subnetName = 'default'
var nsgName = 'nsg-${vmName}'
var publicIpName = 'pip-${vmName}'
var apimName = 'apim-${uniqueSuffix}'

var isLinux = vmOs == 'linux'

// ─── Network Security Group ────────────────────────────────────────────────

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: nsgName
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: isLinux ? 'Allow-SSH' : 'Allow-RDP'
        properties: {
          priority: 1000
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: isLinux ? '22' : '3389'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: '*'
        }
      }
    ]
  }
}

// ─── Virtual Network ────────────────────────────────────────────────────────

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.0.0.0/16']
    }
    subnets: [
      {
        name: subnetName
        properties: {
          addressPrefix: '10.0.1.0/24'
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
}

// ─── Public IP ──────────────────────────────────────────────────────────────

resource publicIp 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: publicIpName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

// ─── Network Interface ─────────────────────────────────────────────────────

resource nic 'Microsoft.Network/networkInterfaces@2023-11-01' = {
  name: nicName
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: {
            id: publicIp.id
          }
          subnet: {
            id: vnet.properties.subnets[0].id
          }
        }
      }
    ]
  }
}

// ─── Virtual Machine ────────────────────────────────────────────────────────

resource vm 'Microsoft.Compute/virtualMachines@2024-03-01' = {
  name: vmName
  location: location
  tags: tags
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      adminPassword: isLinux ? null : adminPasswordOrKey
      linuxConfiguration: isLinux ? {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: adminPasswordOrKey
            }
          ]
        }
      } : null
    }
    storageProfile: {
      imageReference: isLinux ? {
        publisher: 'Canonical'
        offer: '0001-com-ubuntu-server-jammy'
        sku: '22_04-lts-gen2'
        version: 'latest'
      } : {
        publisher: 'MicrosoftWindowsServer'
        offer: 'WindowsServer'
        sku: '2022-datacenter-azure-edition'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        managedDisk: {
          storageAccountType: 'StandardSSD_LRS'
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
  }
}

// ─── API Management ─────────────────────────────────────────────────────────

resource apim 'Microsoft.ApiManagement/service@2023-09-01-preview' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: apimSku
    capacity: apimSku == 'Consumption' ? 0 : apimUnits
  }
  properties: {
    publisherEmail: apimPublisherEmail
    publisherName: apimPublisherName
  }
}

// ─── Outputs ────────────────────────────────────────────────────────────────

output vmName string = vm.name
output vmId string = vm.id
output publicIpAddress string = publicIp.properties.ipAddress
output apimName string = apim.name
output apimGatewayUrl string = apim.properties.gatewayUrl
