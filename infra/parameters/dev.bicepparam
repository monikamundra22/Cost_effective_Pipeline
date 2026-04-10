using './main.bicep'

param projectName = 'costpipe'
param environmentName = 'dev'
param appServicePlanSku = 'B1'
param storageAccountSku = 'Standard_LRS'
param functionRuntime = 'dotnet'
param appConfigSku = 'free'
param tags = {
  CostCenter: '12345'
  Owner: 'DevOps'
}
