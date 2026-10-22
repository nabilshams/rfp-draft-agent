// The project ledger: Azure Cosmos DB for NoSQL, serverless, reached only through a private endpoint.
//
// * Serverless: no provisioned throughput, so an idle ledger costs almost nothing and a busy one is billed per request.
// * Entra ID only: local (key) authentication is disabled, so the account has no keys to leak. The app's managed
//   identity gets the built-in "Cosmos DB Built-in Data Contributor" data-plane role.
// * No public access: a policy on some subscriptions requires it, and the ledger holds confidential bid content.
//   The private endpoint and DNS zone are created in network.bicep from the account ID this module outputs.
// * One container for a whole project (project, requirements, answer versions and audit events), partitioned by
//   project, so one project's changes can be written atomically in a single transactional batch.

param location string
param tags object
param accountName string
param databaseName string = 'rfp-ledger'
param containerName string = 'ledger'
@description('Object ID of the identity that runs the app.')
param appPrincipalId string

// Built-in data-plane role: read and write items (not account management).
var dataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: accountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    capabilities: [
      { name: 'EnableServerless' }
    ]
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    minimalTlsVersion: 'Tls12'
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource ledger 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: containerName
  properties: {
    resource: {
      id: containerName
      partitionKey: {
        paths: ['/projectId']
        kind: 'Hash'
        version: 2
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          { path: '/*' }
        ]
        excludedPaths: [
          // Large text the app never filters or sorts on
          { path: '/text/?' }
          { path: '/answer_markdown/?' }
          { path: '/evidence/*' }
        ]
      }
    }
  }
}

resource appRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: account
  name: guid(account.id, appPrincipalId, dataContributorRoleId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${dataContributorRoleId}'
    principalId: appPrincipalId
    scope: account.id
  }
}

output accountId string = account.id
output accountName string = account.name
output endpoint string = account.properties.documentEndpoint
output databaseName string = database.name
output containerName string = ledger.name
