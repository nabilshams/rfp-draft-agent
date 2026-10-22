// ─────────────────────────────────────────────────────────
// RFP Draft Agent – azd entry point (subscription scope)
//
// Creates the resource group and deploys everything in resources.bicep:
//   • Azure AI Services account + Foundry project + gpt-4o / embedding deployments
//   • Azure AI Search, Storage (3 containers), Log Analytics, Application Insights
//   • Azure Container Registry + Container Apps environment + the API container app
//   • User-assigned managed identity with the RBAC the app needs
// ─────────────────────────────────────────────────────────

targetScope = 'subscription'

@minLength(1)
@maxLength(32)
@description('Name of the azd environment. Used for the resource group name, resource-name token and tags.')
param environmentName string

@minLength(1)
@description('Primary Azure region for all resources.')
param location string

@description('Object ID of the user/service principal running azd. Granted data-plane roles for local development. Leave empty to skip.')
param principalId string = ''

@allowed(['User', 'ServicePrincipal', 'Group'])
param principalType string = 'User'

@description('Set by azd (SERVICE_API_RESOURCE_EXISTS) so re-provisioning keeps the image that was last deployed.')
param apiAppExists bool = false

@allowed(['disabled', 'easyauth', 'jwt'])
@description('How requests are authenticated. "disabled" = no authentication (demo only; the app logs a warning). "easyauth" = Container Apps built-in authentication in front of the app (needs entraClientId and entraClientSecret; see scripts/setup_auth.py). "jwt" = the app validates Entra bearer tokens itself (needs entraClientId).')
param authMode string = 'disabled'

@description('Entra tenant used for sign-in and token validation. Defaults to the tenant of the deployment.')
param entraTenantId string = ''

@description('Entra app registration (client) ID. Required when authMode is not "disabled".')
param entraClientId string = ''

@secure()
@description('Client secret of the Entra app registration. Required when authMode is "easyauth".')
param entraClientSecret string = ''

@description('Comma-separated object IDs of users who can manage every RFP and KB document, re-index the knowledge base and replace the shared template. scripts/setup_auth.py sets this to the signed-in user.')
param adminOids string = ''

@description('Expected token audience (jwt mode). Defaults to the client ID when empty.')
param entraAudience string = ''

@description('Resource ID of an existing Grounding-with-Bing connection in the Foundry project. Leave empty to disable web search. When set, users can opt in per request; the query text is then sent to Bing, outside the Azure compliance boundary.')
param bingConnectionId string = ''

@secure()
@description('Semicolon-separated customer names to redact from RFPs, requests, knowledge-base chunks and drafts (e.g. "Contoso Ltd;Fabrikam"). Kept as a secret, not in the repository.')
param redactionCustomerNames string = ''

@description('Comma-separated IPv4 CIDRs allowed to reach the app. Empty = open to the internet.')
param allowedIpRanges string = ''

@description('Chat model deployed to the Foundry account (also used as the deployment name).')
param chatModelName string = 'gpt-4o'
param chatModelVersion string = '2024-11-20'
param chatModelSku string = 'GlobalStandard'
param chatModelCapacity int = 30

@description('Embedding model. Must output 1536 dimensions to match the search index schema.')
param embeddingModelName string = 'text-embedding-3-small'
param embeddingModelVersion string = '1'
param embeddingModelSku string = 'Standard'
param embeddingModelCapacity int = 50

@allowed(['basic', 'standard', 'standard2', 'standard3'])
param searchSku string = 'basic'

@description('Email address that receives alerts (failed requests, exceptions, restarts). Leave empty to create the alerts without notifications.')
param alertEmail string = ''

var tags = {
  'azd-env-name': environmentName
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    tags: tags
    environmentName: environmentName
    principalId: principalId
    principalType: principalType
    apiAppExists: apiAppExists
    authMode: authMode
    entraTenantId: empty(entraTenantId) ? tenant().tenantId : entraTenantId
    // resources.bicep rejects empty values for these, so a half-configured deployment fails
    // template validation instead of creating an app that is unprotected or crash-looping.
    entraClientId: authMode == 'disabled' ? 'not-configured' : entraClientId
    entraClientSecret: authMode == 'easyauth' ? entraClientSecret : 'not-used'
    entraAudience: entraAudience
    adminOids: adminOids
    bingConnectionId: bingConnectionId
    redactionCustomerNames: redactionCustomerNames
    allowedIpRanges: allowedIpRanges
    chatModelName: chatModelName
    chatModelVersion: chatModelVersion
    chatModelSku: chatModelSku
    chatModelCapacity: chatModelCapacity
    embeddingModelName: embeddingModelName
    embeddingModelVersion: embeddingModelVersion
    embeddingModelSku: embeddingModelSku
    embeddingModelCapacity: embeddingModelCapacity
    searchSku: searchSku
    alertEmail: alertEmail
  }
}

// ── Outputs (azd stores these in the environment; names in caps become env vars) ──

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name

// Required by azd for remote image builds / container app deploys
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.containerRegistryEndpoint
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.containerRegistryName

output SERVICE_API_ENDPOINT_URL string = resources.outputs.apiUrl
output AUTH_MODE string = authMode
output SERVICE_API_IDENTITY_PRINCIPAL_ID string = resources.outputs.identityPrincipalId

// Values for a local .env file (see .env.example)
output FOUNDRY_PROJECT_ENDPOINT string = resources.outputs.foundryProjectEndpoint
output FOUNDRY_MODEL_DEPLOYMENT_NAME string = chatModelName
output EMBEDDING_MODEL_DEPLOYMENT_NAME string = embeddingModelName
output AZURE_AI_SEARCH_ENDPOINT string = resources.outputs.searchEndpoint
output AZURE_AI_SEARCH_CONNECTION_NAME string = resources.outputs.searchName
output AZURE_STORAGE_ACCOUNT_URL string = resources.outputs.storageAccountUrl
output COSMOS_ENDPOINT string = resources.outputs.cosmosEndpoint
