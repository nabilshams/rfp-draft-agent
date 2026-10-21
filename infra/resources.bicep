// ─────────────────────────────────────────────────────────
// RFP Draft Agent – resource-group-scope resources (called from main.bicep)
// ─────────────────────────────────────────────────────────

targetScope = 'resourceGroup'

param location string
param tags object
param environmentName string
param principalId string
param principalType string
param apiAppExists bool
param entraTenantId string
param entraClientId string
param entraAudience string
param bingConnectionId string
param allowedIpRanges string
param chatModelName string
param chatModelVersion string
param chatModelSku string
param chatModelCapacity int
param embeddingModelName string
param embeddingModelVersion string
param embeddingModelSku string
param embeddingModelCapacity int
param searchSku string

// ── Names ───────────────────────────────────────────────

var token = toLower(uniqueString(subscription().id, environmentName, location))
var aiServicesName = 'ais-${token}'
var projectName = 'proj-${token}'
var searchName = 'srch-${token}'
var storageName = 'st${token}'
var registryName = 'cr${token}'
var logAnalyticsName = 'log-${token}'
var appInsightsName = 'appi-${token}'
var identityName = 'id-${token}'
var containerEnvName = 'cae-${token}'
var apiAppName = 'ca-api-${token}'

// Shown until `azd deploy` pushes the real image (azd replaces it on first deploy).
var placeholderImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
var apiPort = 8000

// ── Built-in role definition IDs ────────────────────────

var roleStorageBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var roleSearchIndexDataContributor = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
var roleSearchServiceContributor = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
var roleAzureAIUser = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var roleCognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var roleAcrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// ── Monitoring ──────────────────────────────────────────

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
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
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ── Managed identity used by the container app ─────────

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

// ── Storage ─────────────────────────────────────────────

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    // The app authenticates with Entra ID (DefaultAzureCredential); keys are not needed.
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
  }

  resource blobService 'blobServices' = {
    name: 'default'

    resource knowledgeBase 'containers' = {
      name: 'knowledge-base'
    }
    resource rfpUploads 'containers' = {
      name: 'rfp-uploads'
    }
    resource outputs 'containers' = {
      name: 'outputs'
    }
  }
}

// ── Azure AI Search ─────────────────────────────────────

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: searchName
  location: location
  tags: tags
  sku: {
    name: searchSku
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'enabled'
    // Entra ID only – the app uses DefaultAzureCredential.
    disableLocalAuth: true
  }
}

// ── Azure AI Foundry (AI Services account + project + models) ──

resource aiServices 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: aiServicesName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: aiServicesName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }

  resource project 'projects' = {
    name: projectName
    location: location
    tags: tags
    identity: {
      type: 'SystemAssigned'
    }
    properties: {
      displayName: 'RFP Draft Agent'
      description: 'Foundry project hosting the RFP draft and QA agents.'
    }
  }

  resource chatDeployment 'deployments' = {
    name: chatModelName
    sku: {
      name: chatModelSku
      capacity: chatModelCapacity
    }
    properties: {
      model: {
        format: 'OpenAI'
        name: chatModelName
        version: chatModelVersion
      }
    }
  }

  resource embeddingDeployment 'deployments' = {
    name: embeddingModelName
    // Model deployments on one account must be created one at a time.
    dependsOn: [chatDeployment]
    sku: {
      name: embeddingModelSku
      capacity: embeddingModelCapacity
    }
    properties: {
      model: {
        format: 'OpenAI'
        name: embeddingModelName
        version: embeddingModelVersion
      }
    }
  }
}

// ── Container registry + Container Apps environment ────

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, roleAcrPull)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerEnvName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// Keep the image that `azd deploy` last pushed when re-provisioning.
module fetchLatestImage 'modules/fetch-container-image.bicep' = {
  name: 'fetch-api-image'
  params: {
    exists: apiAppExists
    name: apiAppName
  }
}

var foundryProjectEndpoint = 'https://${aiServices.name}.services.ai.azure.com/api/projects/${projectName}'
var ipRanges = empty(allowedIpRanges) ? [] : split(allowedIpRanges, ',')

var baseEnv = [
  { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
  { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
  { name: 'FOUNDRY_MODEL_DEPLOYMENT_NAME', value: chatModelName }
  { name: 'EMBEDDING_MODEL_DEPLOYMENT_NAME', value: embeddingModelName }
  { name: 'AZURE_AI_SEARCH_CONNECTION_NAME', value: search.name }
  { name: 'AZURE_AI_SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
  { name: 'AI_SEARCH_INDEX_NAME', value: 'rfp-knowledge-base' }
  { name: 'AZURE_STORAGE_ACCOUNT_URL', value: storage.properties.primaryEndpoints.blob }
  { name: 'STORAGE_KB_CONTAINER', value: 'knowledge-base' }
  { name: 'STORAGE_RFP_CONTAINER', value: 'rfp-uploads' }
  { name: 'STORAGE_OUTPUT_CONTAINER', value: 'outputs' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-connection-string' }
  { name: 'ENTRA_TENANT_ID', value: entraTenantId }
  // Settings requires a client ID value even in dev mode.
  { name: 'ENTRA_CLIENT_ID', value: empty(entraClientId) ? 'not-configured' : entraClientId }
  { name: 'PARALLELISM', value: '1' }
  { name: 'SECTION_PARALLELISM', value: '3' }
]
var audienceEnv = empty(entraAudience) ? [] : [{ name: 'ENTRA_AUDIENCE', value: entraAudience }]
var bingEnv = empty(bingConnectionId) ? [] : [{ name: 'BING_CONNECTION_ID', value: bingConnectionId }]

resource apiApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: apiAppName
  location: location
  // azd locates the deploy target through this tag.
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  // The registry pull role must exist before the app is created.
  dependsOn: [acrPullRole]
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: apiPort
        transport: 'auto'
        allowInsecure: false
        ipSecurityRestrictions: [for (range, i) in ipRanges: {
          name: 'allow-${i}'
          action: 'Allow'
          ipAddressRange: trim(range)
        }]
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'appinsights-connection-string'
          value: appInsights.properties.ConnectionString
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiAppExists ? fetchLatestImage.outputs.containers[0].image : placeholderImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: concat(baseEnv, audienceEnv, bingEnv)
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: apiPort
              }
              periodSeconds: 5
              failureThreshold: 24
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: apiPort
              }
              // Generous so CPU-heavy work in worker threads (parsing, document generation)
              // can't get the container restarted by a slow /health response.
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 5
            }
          ]
        }
      ]
      // One replica: the log buffer, WebSocket clients and metrics live in process memory.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// ── RBAC for the app's managed identity ─────────────────

resource miStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, identity.id, roleStorageBlobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobDataContributor)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Index Data Contributor reads/writes documents; Service Contributor lets the app create the index.
var searchRoles = [roleSearchIndexDataContributor, roleSearchServiceContributor]

resource miSearchRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in searchRoles: {
  scope: search
  name: guid(search.id, identity.id, role)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}]

// Azure AI User covers the Agents API; OpenAI User covers embeddings.
var aiRoles = [roleAzureAIUser, roleCognitiveServicesOpenAIUser]

resource miAiRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in aiRoles: {
  scope: aiServices
  name: guid(aiServices.id, identity.id, role)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}]

// ── RBAC for the developer running azd (local runs, scripts) ──

resource userStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: storage
  name: guid(storage.id, principalId, roleStorageBlobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobDataContributor)
    principalId: principalId
    principalType: principalType
  }
}

resource userSearchRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in searchRoles: if (!empty(principalId)) {
  scope: search
  name: guid(search.id, principalId, role)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
    principalId: principalId
    principalType: principalType
  }
}]

resource userAiRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in aiRoles: if (!empty(principalId)) {
  scope: aiServices
  name: guid(aiServices.id, principalId, role)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
    principalId: principalId
    principalType: principalType
  }
}]

// ── Outputs ─────────────────────────────────────────────

output containerRegistryEndpoint string = registry.properties.loginServer
output containerRegistryName string = registry.name
output apiUrl string = 'https://${apiApp.properties.configuration.ingress.fqdn}'
output identityPrincipalId string = identity.properties.principalId
output foundryProjectEndpoint string = foundryProjectEndpoint
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output searchName string = search.name
output storageAccountUrl string = storage.properties.primaryEndpoints.blob
