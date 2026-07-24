# sheet-service

Cloud Run service that provides generic Google Sheets read and write functionality. Replaces the `@globals.googleappscriptreadfromsheeturl` and `@globals.googleappscriptwritetospreadsheeturl` Apps Script webhooks.

## Endpoints

### `GET /health`
Health check. Returns `{"status": "ok"}`.

---

### `POST /read`

Read rows from a Google Sheet. Two modes: `match` (filter by key/value) and `all`
(return every row).

**Request — `mode: "match"` (default):**
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

**Request — `mode: "all"`:**
```json
{
    "password": "...",
    "sheetid": "<Google Sheet ID>",
    "tab": "<Tab Name>",
    "mode": "all",
    "columns": ["col1"],
    "limit": 100
}
```

- Returns every row in the tab. `key` is not required and is ignored.
- `columns` works the same as in match mode — use it to pull a single column cheaply.
- `limit` is optional; caps the number of rows returned.
- Useful for scanning or aggregating a column (e.g. taking `max()` of a timestamp
  column to derive an incremental-ingest watermark).

**Response:**
```json
{
    "status": "success",
    "mode": "match",
    "rows": [
        {"_rowNumber": 3, "col1": "val1", "col2": "val2"}
    ]
}
```

- `_rowNumber` is the 1-based sheet row.
- An unknown mode returns `400 {"status":"error","message":"Unsupported mode: <x>"}`.

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

### `POST /coordinates`

Column×row **intersection** lookup with 500-char segmentation. Mirrors the legacy "Fetch" Apps Script `doPost` (`resourceMapWebappInternal`) exactly, so the TextIt `Fetch Resources V4` flow can re-point to this service by URL only — the request body and the segmented response keys its downstream nodes consume (`<result_name>`, `<result_name>_1`, `<result_name>_2`, …, `<result_name>_segments`) are unchanged.

**Request:**
```json
{
    "password": "...",
    "sheetid": "<Google Sheet ID>",
    "coordinates": [
        {
            "sheet": "<Tab Name>",
            "column": "<column header, case-insensitive>",
            "row": "<row key matched in the first column, case-insensitive>",
            "result_name": "ResourcesPrimary"
        }
    ]
}
```

- For each coordinate, `column` is matched case-insensitively against the **first row** (headers) and `row` against the **first column** (keys); the value returned is the cell at their intersection.
- `row` may instead be `"_N"` (e.g. `"_5"`) for a 1-based literal row index.
- The cell value is segmented at 500 chars on blank lines and before `(N)`-style markers, identical to the Apps Script: full value under `<result_name>`, each segment under `<result_name>_<n>`, count under `<result_name>_segments`.
- A column or row miss is **not** an HTTP error — it returns HTTP 200 with `"Column value not found."` / `"Row value not found."` placed under `result_name` (same as the Apps Script). Distinguish a content miss from a transport failure by body, not status.
- `sheetid` is the one addition vs. the Apps Script (which used its bound spreadsheet). Supply it in the body, or set the `RESOURCE_SHEET_ID` env var as a default.

**Response (HTTP 200):** flat JSON merging every coordinate's segmented keys:
```json
{
    "ResourcesPrimary": "...full value...",
    "ResourcesPrimary_1": "...",
    "ResourcesPrimary_2": "...",
    "ResourcesPrimary_segments": 2,
    "ResourcesSelfHelp": "...",
    "ResourcesSelfHelp_1": "...",
    "ResourcesSelfHelp_segments": 1
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
| `RESOURCE_SHEET_ID` | No | Default Google Sheet ID for `/coordinates` when the request body omits `sheetid`. |

## Local development

```bash
pip install -r requirements.txt
export SHEET_SERVICE_PASSWORD="..."
export OAUTH_CLIENT_ID="..."
export OAUTH_CLIENT_SECRET="..."
export OAUTH_REFRESH_TOKEN="..."
python src/main.py
```
