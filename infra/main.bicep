// Provisions the base resources for RSGRSC1RMIAD02.
// Scope: resource-only provisioning. No RBAC / cross-resource permissions are configured here on purpose.
targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = 'westus'

@description('Common tags applied to every resource.')
param tags object = {}

@description('Name of the Microsoft Foundry account (AIServices, formerly "hub").')
param foundryAccountName string = 'aaifs1rmiad02'

@description('Name of the Microsoft Foundry project (child of the Foundry account).')
param foundryProjectName string = 'prj_aaifs1rmiad02'

@description('Name of the Azure AI Search service.')
param searchServiceName string = 'azcssc1rmiad02'

@description('Name of the Blob Storage account.')
param storageAccountName string = 'stacs1miabackd02'

@description('Name of the Document Intelligence (Form Recognizer) account.')
param documentIntelligenceName string = 'aidieu2rmiad02'

@description('Name of the App Service Plan.')
param appServicePlanName string = 'aspleu2rmiad02'

@description('Name of the Azure Bot resource.')
param botServiceName string = 'azbseu2rmiad02'

@description('Placeholder messaging endpoint for the bot. Replace once a real backend is deployed.')
param botEndpoint string = 'https://${botServiceName}.azurewebsites.net/api/messages'

@description('Name of the user-assigned managed identity used as the bot Microsoft App ID (UserAssignedMSI auth, no role assignments granted here).')
param botIdentityName string = '${botServiceName}-identity'

// ---------------------------------------------------------------------------
// Blob Storage
// ---------------------------------------------------------------------------
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
  }
}

// ---------------------------------------------------------------------------
// Azure AI Search
// ---------------------------------------------------------------------------
resource searchService 'Microsoft.Search/searchServices@2025-05-01' = {
  name: searchServiceName
  location: location
  tags: tags
  sku: {
    name: 'basic'
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'Default'
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Document Intelligence
// ---------------------------------------------------------------------------
resource documentIntelligence 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: documentIntelligenceName
  location: location
  tags: tags
  kind: 'FormRecognizer'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: toLower(documentIntelligenceName)
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Microsoft Foundry account (AIServices) + project
// ---------------------------------------------------------------------------
resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryAccountName
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
    customSubDomainName: toLower(foundryAccountName)
    allowProjectManagement: true
    publicNetworkAccess: 'Enabled'
  }
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundryAccount
  name: foundryProjectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: foundryProjectName
  }
}

// ---------------------------------------------------------------------------
// App Service Plan (Linux)
// ---------------------------------------------------------------------------
resource appServicePlan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: appServicePlanName
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: 'B1'
    tier: 'Basic'
  }
  properties: {
    reserved: true
  }
}

// ---------------------------------------------------------------------------
// Azure Bot
// ---------------------------------------------------------------------------
// User-assigned identity used as the bot's Microsoft App ID (MultiTenant auth is deprecated).
// This only creates the identity resource; no role assignments are made.
// Permisos (estado actual): no se asignó ningún rol/RBAC a tu usuario ni entre recursos.
// La única identidad creada es esta managed identity, usada solo como Microsoft App ID del bot.

resource botIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: botIdentityName
  location: location
  tags: tags
}

resource botService 'Microsoft.BotService/botServices@2022-09-15' = {
  name: botServiceName
  location: 'global'
  tags: tags
  kind: 'azurebot'
  sku: {
    name: 'F0'
  }
  properties: {
    displayName: botServiceName
    endpoint: botEndpoint
    msaAppId: botIdentity.properties.clientId
    msaAppType: 'UserAssignedMSI'
    msaAppMSIResourceId: botIdentity.id
    msaAppTenantId: subscription().tenantId
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output storageAccountId string = storageAccount.id
output storageAccountBlobEndpoint string = storageAccount.properties.primaryEndpoints.blob
output searchServiceId string = searchService.id
output searchServiceEndpoint string = 'https://${searchService.name}.search.windows.net'
output documentIntelligenceId string = documentIntelligence.id
output documentIntelligenceEndpoint string = documentIntelligence.properties.endpoint
output foundryAccountId string = foundryAccount.id
output foundryAccountEndpoint string = foundryAccount.properties.endpoint
output foundryProjectId string = foundryProject.id
output appServicePlanId string = appServicePlan.id
output botServiceId string = botService.id
output botIdentityId string = botIdentity.id
output botIdentityClientId string = botIdentity.properties.clientId
