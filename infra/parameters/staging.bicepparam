using '../main.bicep'

param projectName = 'costpipe'
param environmentName = 'staging'
param appServicePlanSku = 'S1'
param storageAccountSku = 'Standard_GRS'
param functionRuntime = 'dotnet'
param appConfigSku = 'standard'
param tags = {
  CostCenter: '12345'
  Owner: 'DevOps'
}
