# Merged product workflow runtime

Each merged application can provide `workflows.json` beside its `manifest.json`.
Version 1 turns those definitions into a persistent product workspace at
`/workflows`, backed by the application's existing `database.sqlite`.

## Configuration

```json
{
  "version": 1,
  "workflows": [
    {
      "id": "case-review",
      "title": "Case review",
      "description": "Review and approve a case.",
      "fields": [
        {"key": "caseName", "label": "Case name", "type": "text", "required": true},
        {"key": "priority", "label": "Priority", "type": "select", "options": ["Low", "High"], "default": "Low"}
      ],
      "states": ["draft", "review", "approved"],
      "transitions": [
        {"id": "submit", "label": "Submit", "from": ["draft"], "to": "review"}
      ],
      "seeds": [
        {"id": "case-1001", "state": "draft", "caseName": "Example", "priority": "High"}
      ]
    }
  ],
  "resources": [
    {"name": "case_records", "type": "table", "workflowId": "case-review"},
    {"name": "active_cases", "type": "view", "workflowId": "case-review"}
  ]
}
```

Workflow, state, and transition IDs allow letters, numbers, underscores, and
hyphens. Field keys support camelCase or underscores. Supported field types are
`text`, `textarea`, `number`, `integer`, `boolean`, `date`, `datetime`, `email`,
`url`, `json`, `select`, and `multiselect`.

Compatibility resources provide exact SQLite names required by product
contracts. Runtime-created tables mirror the workflow's current records and
runtime-created views query them directly. An existing imported table or view is
preserved without alteration and reported as an externally owned, satisfied
resource.

## HTTP API

- `GET /api/product/status`
- `GET /api/product/dashboard`
- `GET /api/workflows`
- `GET|POST /api/workflows/{workflowId}/records`
- `GET|PATCH|DELETE /api/workflows/{workflowId}/records/{recordId}`
- `POST /api/workflows/{workflowId}/records/{recordId}/transitions/{transitionId}`
- `GET /api/workflows/{workflowId}/records/{recordId}/events`
- `GET /api/workflows/{workflowId}/export.csv`
- `GET /workflows` or `/workflows/{workflowId}` for the product UI

Create requests accept `{"values": {...}}`. Patch and transition requests must
include the current numeric `version`; `If-Match` is also accepted. Delete uses
`?version=N` or `If-Match`. Stale writes return HTTP 409. Mutations record the
validated `X-Actor` identity (default `local-admin`) in an immutable SHA-256
hash-chained audit event. SQLite triggers reject event updates and deletions.

List endpoints support `state`, `q`, `limit`, `offset`, and
`field.{fieldKey}=value` query filters.

## Tests

```sh
python3 -m unittest discover -s merged/_runtime/tests -v
```
