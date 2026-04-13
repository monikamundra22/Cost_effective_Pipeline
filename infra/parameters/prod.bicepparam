using '../main.bicep'

param projectName = 'costpipe'
param environmentName = 'prod'
param appServicePlanSku = 'P1v3'
param storageAccountSku = 'Standard_ZRS'
param functionRuntime = 'dotnet'
param appConfigSku = 'standard'
param tags = {
  CostCenter: '12345'
  Owner: 'DevOps'
}
