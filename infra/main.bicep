// ============================================================================
// main.bicep – Azure Infrastructure: Web App, Function App, Storage, App Config
// ============================================================================

targetScope = 'resourceGroup'

// ─── Parameters ─────────────────────────────────────────────────────────────

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Environment name (dev, staging, prod)')
@allowed(['dev', 'staging', 'prod'])
param environmentName string = 'dev'

@description('Project name used as a prefix for resource names')
@minLength(3)
@maxLength(11)
param projectName string

@description('App Service Plan SKU')
@allowed(['F1', 'B1', 'B2', 'S1', 'S2', 'P1v3', 'P2v3'])
param appServicePlanSku string = 'B1'

@description('Storage Account SKU')
@allowed(['Standard_LRS', 'Standard_GRS', 'Standard_ZRS'])
param storageAccountSku string = 'Standard_LRS'

@description('Function App runtime stack')
@allowed(['dotnet', 'node', 'python', 'java'])
param functionRuntime string = 'dotnet'

@description('App Configuration SKU')
@allowed(['free', 'standard'])
param appConfigSku string = 'free'

@description('Tags applied to every resource')
param tags object = {}

// ─── Variables ──────────────────────────────────────────────────────────────

var suffix = '${projectName}-${environmentName}'
var uniqueSuffix = uniqueString(resourceGroup().id, projectName, environmentName)

var appServicePlanName = 'asp-${suffix}'
var webAppName = 'app-${suffix}'
var functionAppName = 'func-${suffix}'
var storageAccountName = 'st${replace(projectName, '-', '')}${uniqueSuffix}'
var appConfigName = 'appcs-${suffix}'
var appInsightsName = 'appi-${suffix}'
var logAnalyticsName = 'log-${suffix}'
var functionStorageName = 'stfunc${replace(projectName, '-', '')}${uniqueSuffix}'

var commonTags = union(tags, {
  Environment: environmentName
  Project: projectName
  ManagedBy: 'Bicep'
})

// ─── Log Analytics Workspace ────────────────────────────────────────────────

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: commonTags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// ─── Application Insights ───────────────────────────────────────────────────

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  tags: commonTags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    RetentionInDays: 30
  }
}

// ─── Storage Account (primary) ──────────────────────────────────────────────

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take(storageAccountName, 24)
  location: location
  tags: commonTags
  kind: 'StorageV2'
  sku: {
    name: storageAccountSku
  }
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}

// ─── Storage Account (Function App) ────────────────────────────────────────

resource functionStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take(functionStorageName, 24)
  location: location
  tags: commonTags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

// ─── App Service Plan ───────────────────────────────────────────────────────

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  tags: commonTags
  kind: 'linux'
  sku: {
    name: appServicePlanSku
  }
  properties: {
    reserved: true
  }
}

// ─── Web App ────────────────────────────────────────────────────────────────

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  tags: commonTags
  kind: 'app,linux'
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOTNETCORE|8.0'
      alwaysOn: appServicePlanSku != 'F1'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'APPCONFIG_ENDPOINT'
          value: appConfig.properties.endpoint
        }
      ]
    }
  }
  identity: {
    type: 'SystemAssigned'
  }
}

// ─── Function App ───────────────────────────────────────────────────────────

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  tags: commonTags
  kind: 'functionapp,linux'
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: functionRuntime == 'dotnet' ? 'DOTNET-ISOLATED|8.0' : ''
      alwaysOn: appServicePlanSku != 'F1'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: 'DefaultEndpointsProtocol=https;AccountName=${functionStorage.name};EndpointSuffix=${az.environment().suffixes.storage};AccountKey=${functionStorage.listKeys().keys[0].value}'
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: functionRuntime
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'APPCONFIG_ENDPOINT'
          value: appConfig.properties.endpoint
        }
      ]
    }
  }
  identity: {
    type: 'SystemAssigned'
  }
}

// ─── App Configuration ─────────────────────────────────────────────────────

resource appConfig 'Microsoft.AppConfiguration/configurationStores@2023-03-01' = {
  name: appConfigName
  location: location
  tags: commonTags
  sku: {
    name: appConfigSku
  }
  properties: {
    disableLocalAuth: false
    enablePurgeProtection: environmentName == 'prod'
  }
  identity: {
    type: 'SystemAssigned'
  }
}

// ─── Outputs ────────────────────────────────────────────────────────────────

output webAppName string = webApp.name
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'
output functionAppName string = functionApp.name
output functionAppUrl string = 'https://${functionApp.properties.defaultHostName}'
output storageAccountName string = storageAccount.name
output storageAccountId string = storageAccount.id
output appConfigName string = appConfig.name
output appConfigEndpoint string = appConfig.properties.endpoint
output appInsightsName string = appInsights.name
output appInsightsInstrumentationKey string = appInsights.properties.InstrumentationKey
