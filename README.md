# sheet-service

Cloud Run service that provides generic Google Sheets read and write functionality. Replaces the `@globals.googleappscriptreadfromsheeturl` and `@globals.googleappscriptwritetospreadsheeturl` Apps Script webhooks.

## Endpoints

### `GET /health`
Health check. Returns `{"status": "ok"}`.

---

### `POST /read`

Read rows from a Google Sheet by key/value match.

**Request:**
```json
{
    "password": "...",
    "sheetid": "<Google Sheet ID>",
    "tab": "<Tab Name>",
    "mode": "match",
    "key": "<column header to match on>",
    "<key>": "<value to match>",
    "columns": ["col1", "col2"]
}
```

- `columns` is optional. Omit to return all columns.
- The `<key>` field name must match the value of `key` — same as the existing Apps Script reader convention.

**Response:**
```json
{
    "status": "success",
    "rows": [
        {"_rowNumber": 3, "col1": "val1", "col2": "val2"}
    ]
}
```

---

### `POST /write`

Write to a Google Sheet. Supports append and update modes.

**Request:**
```json
{
    "password": "...",
    "sheetid": "<Google Sheet ID>",
    "tab": "<Tab Name>",
    "newrow": "yes",
    "key": "<column header>",
    "<key>": "<value>",
    "Column Header 1": "value1",
    "Column Header 2": "value2",
    "prepend": false
}
```

Data columns go flat in the body alongside the metadata fields -- there is no `data` wrapper.

- `newrow: "yes"` -- append a new row. Finds the next functionally-empty row by scanning the key column for a blank value, which correctly handles sheets with array formulas or dropdown validation lists.
- `newrow: "no"` -- find an existing row where the key column matches the key value and update only the specified columns in place.
- `prepend: true` -- append mode only. Inserts at row 2 (top of data, below header) and pushes existing rows down. Defaults to `false`.
- Column matching is case-insensitive.

**Append response:**
```json
{
    "status": "success",
    "row": 47
}
```

**Update response:**
```json
{
    "status": "success",
    "matched": 1,
    "rows": [47]
}
```

---

## Deployment

Deploys automatically to Cloud Run on push to `main` via Cloud Build trigger. Region: `us-east1`. Project: `early-alert-responses`.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SHEET_SERVICE_PASSWORD` | Yes | Shared secret for request authentication. |
| `OAUTH_CLIENT_ID` | Yes | OAuth 2.0 client ID for Google Sheets access. |
| `OAUTH_CLIENT_SECRET` | Yes | OAuth 2.0 client secret. |
| `OAUTH_REFRESH_TOKEN` | Yes | Refresh token authorizing Sheets access. |

## Local development

```bash
pip install -r requirements.txt
export SHEET_SERVICE_PASSWORD="..."
export OAUTH_CLIENT_ID="..."
export OAUTH_CLIENT_SECRET="..."
export OAUTH_REFRESH_TOKEN="..."
python src/main.py
```
