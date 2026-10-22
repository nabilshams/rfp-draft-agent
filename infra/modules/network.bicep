// Private networking for the storage account and the project ledger.
//
// A governance policy on some subscriptions forces public network access on storage accounts to Disabled, and
// even where it doesn't, private access is the safer default for documents that may be confidential. So:
//   * a VNet with one subnet for the Container Apps environment (delegated to Microsoft.App/environments)
//     and one for the private endpoints
//   * a private endpoint for the storage account's blob service, with the privatelink DNS zone linked to the VNet
//     so the storage name resolves to the private address from inside it
//   * the same for the Cosmos DB account (the ledger), with its own privatelink zone
// Search and AI Services keep their public endpoints (protected by Entra ID and RBAC, key auth disabled).

param location string
param tags object
param namePrefix string
param storageAccountId string
@description('Resource ID of the Cosmos DB account for the project ledger. Empty skips its private endpoint.')
param cosmosAccountId string = ''

@description('Address space of the VNet. Change it if it overlaps a network you peer with later.')
param vnetAddressPrefix string = '10.20.0.0/16'
@description('Container Apps environment subnet. A workload profiles environment needs at least a /27.')
param infrastructureSubnetPrefix string = '10.20.0.0/23'
param privateEndpointSubnetPrefix string = '10.20.2.0/27'

var blobDnsZoneName = 'privatelink.blob.${environment().suffixes.storage}'

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: '${namePrefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [vnetAddressPrefix]
    }
    subnets: [
      {
        name: 'containerapps'
        properties: {
          addressPrefix: infrastructureSubnetPrefix
          delegations: [
            {
              name: 'containerapps-environment'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
        }
      }
    ]
  }
}

resource blobDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: blobDnsZoneName
  location: 'global'
  tags: tags
}

resource blobDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: blobDnsZone
  name: '${namePrefix}-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource storagePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: '${namePrefix}-pe-blob'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: '${vnet.id}/subnets/private-endpoints'
    }
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: {
          privateLinkServiceId: storageAccountId
          groupIds: ['blob']
        }
      }
    ]
  }
}

resource storageDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = {
  parent: storagePrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: blobDnsZone.id
        }
      }
    ]
  }
}

output infrastructureSubnetId string = '${vnet.id}/subnets/containerapps'

// ── Cosmos DB (the project ledger) ──────────────────────

var hasCosmos = !empty(cosmosAccountId)
var cosmosDnsZoneName = 'privatelink.documents.azure.com'

resource cosmosDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = if (hasCosmos) {
  name: cosmosDnsZoneName
  location: 'global'
  tags: tags
}

resource cosmosDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = if (hasCosmos) {
  parent: cosmosDnsZone
  name: '${namePrefix}-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource cosmosPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = if (hasCosmos) {
  name: '${namePrefix}-pe-cosmos'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: '${vnet.id}/subnets/private-endpoints'
    }
    privateLinkServiceConnections: [
      {
        name: 'cosmos'
        properties: {
          privateLinkServiceId: cosmosAccountId
          groupIds: ['Sql']
        }
      }
    ]
  }
}

resource cosmosDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = if (hasCosmos) {
  parent: cosmosPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'cosmos'
        properties: {
          privateDnsZoneId: cosmosDnsZone.id
        }
      }
    ]
  }
}
