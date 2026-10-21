// Reads the container image currently deployed to an existing container app.
// Kept in its own module (= its own nested deployment) so the app that consumes the
// result does not end up depending on itself.

param exists bool
param name string

resource existingApp 'Microsoft.App/containerApps@2024-03-01' existing = if (exists) {
  name: name
}

output containers array = exists ? existingApp!.properties.template.containers : []
