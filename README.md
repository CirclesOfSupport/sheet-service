# sheet-service

Cloud Run service that provides generic Google Sheets read and write functionality. Replaces the `@globals.googleappscriptreadfromsheeturl` Apps Script webhook and the `sheet-service-v2` Cloud Function.

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

Write a row to a Google Sheet. Finds the next functionally-empty row by scanning the key column for a blank value, which correctly handles sheets with array formulas or dropdown validation lists.

**Request:**
```json
{
    "password": "...",
    "sheetid": "<Google Sheet ID>",
    "tab": "<Tab Name>",
    "key": "<column header used to find next empty row>",
    "data": {
        "Column Header 1": "value1",
        "Column Header 2": "value2"
    },
    "prepend": false
}
```

- `key` specifies which column to scan when finding the next empty row. Use your primary identifier column (e.g. UUID, ID, Name).
- `prepend: true` inserts a new row at the top (row 2, just below the header) and pushes existing data down. Useful for append-to-top patterns.
- Column matching is case-insensitive.

**Response:**
```json
{
    "status": "success",
    "row": 47
}
```

---

## Deployment

Deploys automatically to Cloud Run on push to `main` via Cloud Build trigger. Region: `us-east1`. Project: `early-alert-responses`.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SHEET_SERVICE_PASSWORD` | Yes | Shared secret; set in Cloud Run env vars. Same value as existing Apps Script password. |
| `GOOGLE_APPLICATION_CREDENTIALS` | No | Path to service account JSON. Not needed on Cloud Run — ADC is used automatically. |

## Credentials

On Cloud Run the service account attached to the Cloud Run service must have **Google Sheets API** access for the sheets it needs to read/write. Grant the service account `roles/sheets.editor` on the relevant sheets, or use domain-wide delegation if all sheets are in the same Workspace org.

## Local development

```bash
pip install -r src/requirements.txt
export SHEET_SERVICE_PASSWORD="WhoWhatNow?42?!"
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/sa.json"
python src/main.py
```
