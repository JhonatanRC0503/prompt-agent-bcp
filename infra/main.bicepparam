using './main.bicep'

param location = 'westus'

param tags = {
  proyecto: 'RMIAD02'
  entorno: 'dev'
}

param foundryAccountName = 'aaifs1rmiad02'
param foundryProjectName = 'prj_aaifs1rmiad02'
param searchServiceName = 'azcssc1rmiad02'
param storageAccountName = 'stacs1miabackd02'
param documentIntelligenceName = 'aidieu2rmiad02'
param appServicePlanName = 'aspleu2rmiad02'
param botServiceName = 'azbseu2rmiad02'
