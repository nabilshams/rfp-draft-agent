// Alerts for the API: failed requests, unhandled exceptions and container restarts.
// Notifications go to alertEmail when it is set; without it the alerts still fire and show in the portal.

param location string
param tags object
param appInsightsId string
param containerAppId string
@description('Where to send alert emails. Leave empty to create the alerts without notifications.')
param alertEmail string = ''
param namePrefix string

var notify = !empty(alertEmail)

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (notify) {
  name: '${namePrefix}-alerts'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'rfpalerts'
    enabled: true
    emailReceivers: [
      {
        name: 'owner'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

var actions = notify ? { actionGroups: [actionGroup.id] } : null

// Server errors on real requests (health probes are excluded so a restart doesn't page anyone twice).
resource failedRequests 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = {
  name: '${namePrefix}-failed-requests'
  location: location
  tags: tags
  properties: {
    displayName: 'RFP Draft Agent: failed requests'
    description: 'Five or more requests failed with a server error in 15 minutes.'
    severity: 2
    enabled: true
    scopes: [appInsightsId]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      allOf: [
        {
          query: 'requests | where success == false and toint(resultCode) >= 500 and name !has "/health"'
          timeAggregation: 'Count'
          operator: 'GreaterThanOrEqual'
          threshold: 5
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: actions
  }
}

resource exceptions 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = {
  name: '${namePrefix}-exceptions'
  location: location
  tags: tags
  properties: {
    displayName: 'RFP Draft Agent: unhandled exceptions'
    description: 'Ten or more exceptions were recorded in 15 minutes.'
    severity: 3
    enabled: true
    scopes: [appInsightsId]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      allOf: [
        {
          query: 'exceptions'
          timeAggregation: 'Count'
          operator: 'GreaterThanOrEqual'
          threshold: 10
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: actions
  }
}

// A crash loop (for example bad configuration) shows up as restarts, not as failed requests.
resource restarts 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${namePrefix}-restarts'
  location: 'global'
  tags: tags
  properties: {
    description: 'The API container restarted more than twice in 15 minutes.'
    severity: 2
    enabled: true
    scopes: [containerAppId]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'restarts'
          metricNamespace: 'Microsoft.App/containerapps'
          metricName: 'RestartCount'
          operator: 'GreaterThan'
          threshold: 2
          timeAggregation: 'Total'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: notify ? [{ actionGroupId: actionGroup.id }] : []
  }
}
