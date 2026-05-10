// =============================================================================
// main.bicep — Foundry Operations CI/CD Lab
// Scope: subscription
// =============================================================================

targetScope = 'subscription'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name. Controls naming and environment-specific settings.')
@allowed(['dev', 'qa', 'prod'])
param environmentName string

@description('Azure region for all resources.')
param location string = 'eastus2'

@description('Entra ID object ID of a user/group for optional Foundry admin assignment. Leave empty to skip.')
param foundryAdminObjectId string = ''

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var locationAbbreviations = {
  eastus: 'eus'
  eastus2: 'eus2'
  westus2: 'wus2'
  westus3: 'wus3'
  northeurope: 'neu'
  westeurope: 'weu'
  uksouth: 'uks'
}

var locationAbbr = locationAbbreviations[location] ?? location
var resourceGroupName = 'rg-${environmentName}-foundry-operation-lab-${location}'
var tags = {
  environment: environmentName
  project: 'foundry-operations-cicd-lab'
  managedBy: 'bicep'
}

// ---------------------------------------------------------------------------
// Resource Group
// ---------------------------------------------------------------------------

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// ---------------------------------------------------------------------------
// Resource Module
// ---------------------------------------------------------------------------

module resources './modules/resources.bicep' = {
  name: 'deploy-foundry-resources-${environmentName}'
  scope: rg
  params: {
    location: location
    locationAbbr: locationAbbr
    environmentName: environmentName
    foundryAdminObjectId: foundryAdminObjectId
    enableSearchIndexBootstrap: false
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output resourceGroupName string = rg.name
output resourceGroupId string = rg.id
output foundryResourceId string = resources.outputs.foundryResourceId
output foundryProjectId string = resources.outputs.foundryProjectId
output managedIdentityClientId string = resources.outputs.managedIdentityClientId
output containerRegistryLoginServer string = resources.outputs.containerRegistryLoginServer
output containerAppsEnvironmentId string = resources.outputs.containerAppsEnvironmentId
output keyVaultUri string = resources.outputs.keyVaultUri
output aiSearchEndpoint string = resources.outputs.aiSearchEndpoint
output aiSearchIndexName string = resources.outputs.aiSearchIndexName
output foundryIqConnectionName string = resources.outputs.foundryIqConnectionName
output foundryIqConnectionTarget string = resources.outputs.foundryIqConnectionTarget
