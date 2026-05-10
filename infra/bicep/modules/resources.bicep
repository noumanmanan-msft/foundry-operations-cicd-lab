// =============================================================================
// modules/resources.bicep — Foundry Operations CI/CD Lab
// Scope: resource group
// =============================================================================

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Azure region for all resources.')
param location string

@description('Short region abbreviation used in selected resource names (e.g., eus2).')
param locationAbbr string

@description('Environment name (dev, qa, prod).')
param environmentName string

@description('Optional Entra ID object ID for Foundry admin role assignment. Leave empty to skip.')
param foundryAdminObjectId string = ''

@description('Enable Azure deployment script to create/update the default Azure AI Search index. Keep false when shared-key auth is blocked by policy.')
param enableSearchIndexBootstrap bool = false

@description('Tags applied to all resources.')
param tags object

// ---------------------------------------------------------------------------
// Naming variables (derived from env + location)
// ---------------------------------------------------------------------------

var foundryResourceName = 'aif-${environmentName}-foundry-operation-${location}'
var foundryProjectName = 'default-${environmentName}-project'
var storageAccountName = 'st${environmentName}foundryoplab'
var keyVaultSuffix = toLower(substring(uniqueString(subscription().subscriptionId, location), 0, 3))
var keyVaultName = 'kv-${environmentName}-foundry-oplab${keyVaultSuffix}'
var containerRegistryName = 'acr${environmentName}foundryoplab'
var logAnalyticsWorkspaceName = 'law-${environmentName}-foundry-oplab-${locationAbbr}'
var appInsightsName = 'appi-${environmentName}-foundry-oplab-${locationAbbr}'
var containerAppsEnvironmentName = 'cae-${environmentName}-foundry-oplab-${locationAbbr}'
var aiSearchName = 'srch-${environmentName}-foundry-oplab-${locationAbbr}'
var aiSearchIndexName = 'default'
var knowledgeBaseName = 'kb-iq-v1'
var foundryIqConnectionName = 'kb-${knowledgeBaseName}-${environmentName}'
var foundryIqTarget = 'https://${aiSearch.name}.search.windows.net/knowledgebases/${knowledgeBaseName}/mcp?api-version=2025-11-01-Preview'
var managedIdentityName = 'id-${environmentName}-foundry-oplab-${locationAbbr}'

// ---------------------------------------------------------------------------
// Environment-specific settings
// ---------------------------------------------------------------------------

var logRetentionDays = environmentName == 'prod' ? 90 : environmentName == 'qa' ? 60 : 30
var acrSkuName = environmentName == 'prod' ? 'Standard' : 'Basic'
// Semantic ranking is required for Foundry knowledge sources; keep all envs on non-free SKU.
var aiSearchSkuName = environmentName == 'prod' ? 'standard' : 'basic'
var aiSearchLocation = location == 'eastus2' ? 'eastus' : location
var enableKvPurgeProtection = environmentName == 'prod'

var roles = {
  azureAIDeveloper: '64702f94-c441-49e6-a78b-ef80e0188fee'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
}

// =============================================================================
// Identity
// =============================================================================

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
  tags: tags
}

// =============================================================================
// Monitoring
// =============================================================================

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
  tags: tags
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalyticsWorkspace.id
    RetentionInDays: logRetentionDays
  }
  tags: tags
}

// =============================================================================
// Storage
// =============================================================================

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    encryption: {
      services: {
        blob: {
          enabled: true
        }
        file: {
          enabled: true
        }
      }
      keySource: 'Microsoft.Storage'
    }
  }
  tags: tags
}

// =============================================================================
// Key Vault
// =============================================================================

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: enableKvPurgeProtection ? 90 : 7
    enablePurgeProtection: enableKvPurgeProtection ? true : null
    publicNetworkAccess: 'Enabled'
  }
  tags: tags
}

// =============================================================================
// Container Registry
// =============================================================================

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: containerRegistryName
  location: location
  sku: {
    name: acrSkuName
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
  }
  tags: tags
}

// =============================================================================
// Azure AI Search
// =============================================================================

resource aiSearch 'Microsoft.Search/searchServices@2024-03-01-preview' = {
  name: aiSearchName
  location: aiSearchLocation
  sku: {
    name: aiSearchSkuName
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    publicNetworkAccess: 'enabled'
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
  }
  tags: tags
}

// Creates or updates the Azure AI Search index required by Foundry grounding.
resource aiSearchDefaultIndex 'Microsoft.Resources/deploymentScripts@2023-08-01' = if (enableSearchIndexBootstrap) {
  name: 'create-default-search-index-${environmentName}'
  location: location
  kind: 'AzureCLI'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  properties: {
    azCliVersion: '2.64.0'
    timeout: 'PT10M'
    retentionInterval: 'P1D'
    cleanupPreference: 'OnSuccess'
    environmentVariables: [
      {
        name: 'SEARCH_ENDPOINT'
        value: 'https://${aiSearch.name}.search.windows.net'
      }
      {
        name: 'SEARCH_INDEX_NAME'
        value: aiSearchIndexName
      }
      {
        name: 'SEARCH_ADMIN_KEY'
        secureValue: aiSearch.listAdminKeys().primaryKey
      }
    ]
    scriptContent: '''
set -euo pipefail

payload=$(cat <<JSON
{
  "name": "${SEARCH_INDEX_NAME}",
  "fields": [
    {
      "name": "id",
      "type": "Edm.String",
      "key": true,
      "searchable": false,
      "filterable": true,
      "sortable": false,
      "facetable": false
    },
    {
      "name": "title",
      "type": "Edm.String",
      "searchable": true,
      "filterable": true,
      "sortable": true,
      "facetable": false
    },
    {
      "name": "category",
      "type": "Edm.String",
      "searchable": true,
      "filterable": true,
      "sortable": true,
      "facetable": true
    },
    {
      "name": "content",
      "type": "Edm.String",
      "searchable": true,
      "filterable": false,
      "sortable": false,
      "facetable": false
    },
    {
      "name": "source",
      "type": "Edm.String",
      "searchable": true,
      "filterable": true,
      "sortable": true,
      "facetable": false
    }
  ],
  "semantic": {
    "configurations": [
      {
        "name": "default",
        "prioritizedFields": {
          "titleField": {
            "fieldName": "title"
          },
          "prioritizedContentFields": [
            {
              "fieldName": "content"
            }
          ],
          "prioritizedKeywordsFields": [
            {
              "fieldName": "category"
            }
          ]
        }
      }
    ]
  }
}
JSON
)

curl -sS -f -X PUT "${SEARCH_ENDPOINT}/indexes/${SEARCH_INDEX_NAME}?api-version=2024-07-01" \
  -H "Content-Type: application/json" \
  -H "api-key: ${SEARCH_ADMIN_KEY}" \
  --data "${payload}"

echo "Created or updated Azure AI Search index: ${SEARCH_INDEX_NAME}"
'''
  }
  tags: tags
}

// =============================================================================
// Azure AI Foundry (new model) + Foundry Project
// =============================================================================

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryResourceName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: foundryResourceName
    publicNetworkAccess: 'Enabled'
  }
  tags: tags
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundryAccount
  name: foundryProjectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: foundryProjectName
    description: 'Foundry project for ${environmentName}'
  }
  tags: tags
}

resource foundryIqConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2026-03-01' = {
  parent: foundryProject
  name: foundryIqConnectionName
  properties: {
    category: 'RemoteTool'
    authType: any('ProjectManagedIdentity')
    audience: 'https://search.azure.com'
    group: 'GenericProtocol'
    isDefault: true
    isSharedToAll: false
    metadata: {
      knowledgeBaseName: knowledgeBaseName
      type: 'knowledgeBase_MCP'
    }
    peRequirement: 'NotRequired'
    peStatus: 'NotApplicable'
    sharedUserList: []
    target: foundryIqTarget
    useWorkspaceManagedIdentity: false
  }
}

// =============================================================================
// Container Apps Environment
// =============================================================================

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvironmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsWorkspace.properties.customerId
        sharedKey: logAnalyticsWorkspace.listKeys().primarySharedKey
      }
    }
  }
  tags: tags
}

// =============================================================================
// RBAC assignments
// =============================================================================

resource miFoundryRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, managedIdentity.id, roles.azureAIDeveloper)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAIDeveloper)
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource miStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, managedIdentity.id, roles.storageBlobDataContributor)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource miKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, managedIdentity.id, roles.keyVaultSecretsUser)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource miAcrRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, managedIdentity.id, roles.acrPull)
  scope: containerRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource miSearchRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiSearch.id, managedIdentity.id, roles.searchIndexDataContributor)
  scope: aiSearch
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataContributor)
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryAdminRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(foundryAdminObjectId)) {
  name: guid(foundryAccount.id, foundryAdminObjectId, roles.azureAIDeveloper)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAIDeveloper)
    principalId: foundryAdminObjectId
    principalType: 'User'
  }
}

// =============================================================================
// Outputs
// =============================================================================

output foundryResourceId string = foundryAccount.id
output foundryProjectId string = foundryProject.id
output managedIdentityClientId string = managedIdentity.properties.clientId
output containerRegistryLoginServer string = containerRegistry.properties.loginServer
output containerAppsEnvironmentId string = containerAppsEnvironment.id
output keyVaultUri string = keyVault.properties.vaultUri
output aiSearchEndpoint string = 'https://${aiSearch.name}.search.windows.net'
output aiSearchIndexName string = aiSearchIndexName
output foundryIqConnectionName string = foundryIqConnectionName
output foundryIqConnectionTarget string = foundryIqTarget
