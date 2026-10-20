// ─────────────────────────────────────────────────────────
// RFP Draft Agent – Infrastructure-as-Code (Bicep)
//
// Provisions ALL required resources from scratch:
//   • Azure AI Services (with gpt-4o + embedding deployments)
//   • Azure AI Foundry Hub + Project
//   • Azure AI Search (Standard tier)
//   • Azure Storage Account (Blob, 3 containers)
//   • Azure Key Vault
//   • Application Insights + Log Analytics Workspace
//   • User-assigned Managed Identity + RBAC
// ─────────────────────────────────────────────────────────

targetScope = 'resourceGroup'

// ── Parameters ──────────────────────────────────────────

@description('Base name for all resources (lowercase, no hyphens)')
@minLength(3)
@maxLength(16)
param baseName string

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Entra ID tenant ID for JWT validation (set to "dev" for local dev)')
param entraTenantId string = 'dev'

@description('Entra ID client ID for JWT validation')
param entraClientId string = ''

// ── Variables ───────────────────────────────────────────

var uniqueSuffix = uniqueString(resourceGroup().id)
var aiServicesName = '${baseName}-ais-${uniqueSuffix}'
var searchName = '${baseName}-srch-${uniqueSuffix}'
var storageName = '${baseName}st${uniqueSuffix}'
var keyVaultName = '${baseName}-kv-${uniqueSuffix}'
var appInsightsName = '${baseName}-appi'
var logAnalyticsName = '${baseName}-law'
var managedIdName = '${baseName}-id'
var hubName = '${baseName}-hub'
var projectName = '${baseName}-project'

// ── Log Analytics + Application Insights ────────────────

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ── Managed Identity ────────────────────────────────────

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdName
  location: location
}

// ── Storage Account ─────────────────────────────────────

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take(storageName, 24)  // storage names max 24 chars
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource kbContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'knowledge-base'
}

resource rfpContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'rfp-uploads'
}

resource outputsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'outputs'
}

// ── Key Vault (required by AI Foundry Hub) ──────────────

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take(keyVaultName, 24)
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableSoftDelete: true
    enableRbacAuthorization: true
    publicNetworkAccess: 'Enabled'
  }
}

// ── Azure AI Services (models endpoint) ─────────────────

resource aiServices 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: aiServicesName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  properties: {
    publicNetworkAccess: 'Enabled'
    customSubDomainName: aiServicesName
  }
  identity: {
    type: 'SystemAssigned'
  }
}

// ── Model Deployments ───────────────────────────────────

resource gpt4oDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiServices
  name: 'gpt-4o'
  sku: {
    name: 'GlobalStandard'
    capacity: 30
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4o'
      version: '2024-11-20'
    }
  }
}

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiServices
  name: 'text-embedding-3-small'
  sku: {
    name: 'Standard'
    capacity: 50
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-small'
      version: '1'
    }
  }
  dependsOn: [gpt4oDeployment]  // serialize deployments
}

// ── Azure AI Search ─────────────────────────────────────

resource searchService 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: searchName
  location: location
  sku: {
    name: 'basic'
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'enabled'
  }
  identity: {
    type: 'SystemAssigned'
  }
}

// ── Azure AI Foundry Hub ────────────────────────────────

resource aiHub 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: hubName
  location: location
  kind: 'Hub'
  sku: {
    name: 'Basic'
    tier: 'Basic'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: 'RFP Agent Hub'
    storageAccount: storageAccount.id
    keyVault: keyVault.id
    applicationInsights: appInsights.id
    publicNetworkAccess: 'Enabled'
  }

  // Hub-level connection to AI Services
  resource aiServicesConnection 'connections@2024-10-01' = {
    name: 'aoai-connection'
    properties: {
      category: 'AzureOpenAI'
      authType: 'AAD'
      isSharedToAll: true
      target: aiServices.properties.endpoint
      metadata: {
        ApiType: 'Azure'
        ResourceId: aiServices.id
      }
    }
  }

  // Hub-level connection to AI Search
  resource searchConnection 'connections@2024-10-01' = {
    name: 'search-connection'
    properties: {
      category: 'CognitiveSearch'
      authType: 'AAD'
      isSharedToAll: true
      target: 'https://${searchService.name}.search.windows.net'
      metadata: {
        ResourceId: searchService.id
      }
    }
  }
}

// ── Azure AI Foundry Project ────────────────────────────

resource aiProject 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: projectName
  location: location
  kind: 'Project'
  sku: {
    name: 'Basic'
    tier: 'Basic'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: 'RFP Draft Agent Project'
    hubResourceId: aiHub.id
    publicNetworkAccess: 'Enabled'
  }
}

// ── RBAC Assignments ────────────────────────────────────

// Storage Blob Data Contributor for managed identity
var storageBlobDataContributorRole = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource storageBlobRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, managedIdentity.id, storageBlobDataContributorRole)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRole)
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Search Index Data Contributor for managed identity
var searchIndexDataContributorRole = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'

resource searchRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(searchService.id, managedIdentity.id, searchIndexDataContributorRole)
  scope: searchService
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataContributorRole)
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Cognitive Services OpenAI User for managed identity
var cognitiveServicesOpenAIUserRole = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource aiServicesRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiServices.id, managedIdentity.id, cognitiveServicesOpenAIUserRole)
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUserRole)
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Outputs ─────────────────────────────────────────────

output aiServicesName string = aiServices.name
output aiServicesEndpoint string = aiServices.properties.endpoint
output searchServiceName string = searchService.name
output searchEndpoint string = 'https://${searchService.name}.search.windows.net'
output storageAccountName string = storageAccount.name
output storageAccountUrl string = storageAccount.properties.primaryEndpoints.blob
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output managedIdentityClientId string = managedIdentity.properties.clientId
output hubName string = aiHub.name
output projectName string = aiProject.name
output foundryProjectEndpoint string = aiProject.properties.discoveryUrl
output searchConnectionName string = aiHub::searchConnection.name
output aoaiConnectionName string = aiHub::aiServicesConnection.name
