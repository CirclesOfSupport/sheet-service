import os
import json
import logging
from flask import Flask, request, jsonify
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
PASSWORD = os.environ.get("SHEET_SERVICE_PASSWORD", "WhoWhatNow?42?!")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_sheets_client():
    """Build a Google Sheets API client using OAuth refresh token.

    Authenticates as the user who authorized the app (logan@circlesofsupport.net),
    so all sheets accessible to that user are accessible to this service.

    Required environment variables:
      OAUTH_CLIENT_ID      - OAuth 2.0 client ID
      OAUTH_CLIENT_SECRET  - OAuth 2.0 client secret
      OAUTH_REFRESH_TOKEN  - Refresh token obtained via OAuth Playground
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    creds = Credentials(
        token=None,
        refresh_token=os.environ.get("OAUTH_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ.get("OAUTH_CLIENT_ID"),
        client_secret=os.environ.get("OAUTH_CLIENT_SECRET"),
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("sheets", "v4", credentials=creds)


def check_password(body: dict) -> bool:
    return body.get("password") == PASSWORD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def col_letter(n: int) -> str:
    """Convert 1-based column index to A1 letter(s). e.g. 1→A, 26→Z, 27→AA."""
    result = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def get_header_row(service, sheet_id: str, tab: str) -> list[str]:
    """Return the header row as a list of strings."""
    range_name = f"'{tab}'!1:1"
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=range_name
    ).execute()
    values = result.get("values", [[]])
    return [str(h) for h in (values[0] if values else [])]


def find_key_col_index(headers: list[str], key_col: str) -> int:
    """Return 0-based index of key_col in headers, or raise ValueError."""
    for i, h in enumerate(headers):
        if h.strip().lower() == key_col.strip().lower():
            return i
    raise ValueError(f"Key column '{key_col}' not found in headers: {headers}")


def find_next_empty_row(service, sheet_id: str, tab: str, key_col_index: int) -> int:
    """
    Find the next functionally empty row by scanning the key column for the
    first blank value below the header row.

    This avoids the Apps Script bug where array formulas or dropdown lists
    make cells appear non-empty even when no real data has been written.

    Returns 1-based row number.
    """
    col = col_letter(key_col_index + 1)
    range_name = f"'{tab}'!{col}2:{col}10000"
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=range_name,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    values = result.get("values", [])
    # values is a list of single-element lists for non-empty cells.
    # If a row is truly empty OR has only whitespace, treat it as the insert point.
    for i, row in enumerate(values):
        cell_val = str(row[0]).strip() if row else ""
        if cell_val == "":
            return i + 2  # +1 for header, +1 for 1-based
    # All scanned rows are non-empty — append after the last one
    return len(values) + 2


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/read", methods=["POST"])
def read():
    """
    Read rows from a Google Sheet.

    Request body (JSON):
    {
        "password": "...",
        "sheetid": "<Google Sheet ID>",
        "tab": "<Tab Name>",
        "mode": "match",              // only "match" supported for now
        "key": "<column header>",     // column to match against
        "<key>": "<value to match>",  // value to find (same key name as above)
        "columns": ["col1", "col2"]   // optional: columns to return; omit for all
    }

    Response:
    {
        "status": "success",
        "rows": [{"_rowNumber": N, "col1": "val1", ...}]
    }
    """
    body = request.get_json(force=True, silent=True) or {}

    if not check_password(body):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    sheet_id = body.get("sheetid")
    tab = body.get("tab")
    mode = body.get("mode", "match")
    key_col = body.get("key")
    return_cols = body.get("columns")  # optional list

    if not sheet_id or not tab:
        return jsonify({"status": "error", "message": "sheetid and tab are required"}), 400

    if mode != "match":
        return jsonify({"status": "error", "message": f"Unsupported mode: {mode}"}), 400

    if not key_col:
        return jsonify({"status": "error", "message": "key is required for match mode"}), 400

    match_value = body.get(key_col)
    if match_value is None:
        return jsonify({"status": "error", "message": f"Match value for key '{key_col}' not provided"}), 400

    try:
        service = get_sheets_client()
        headers = get_header_row(service, sheet_id, tab)
        if not headers:
            return jsonify({"status": "success", "rows": []}), 200

        # Fetch all data
        range_name = f"'{tab}'!A2:ZZ"
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=range_name,
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        all_rows = result.get("values", [])

        key_idx = find_key_col_index(headers, key_col)

        # Determine which column indices to return
        if return_cols:
            col_indices = []
            for rc in return_cols:
                try:
                    col_indices.append((rc, find_key_col_index(headers, rc)))
                except ValueError:
                    pass  # silently skip unknown return columns
        else:
            col_indices = [(h, i) for i, h in enumerate(headers)]

        matched = []
        for row_offset, row in enumerate(all_rows):
            cell_val = str(row[key_idx]).strip() if key_idx < len(row) else ""
            if cell_val == str(match_value).strip():
                row_data = {"_rowNumber": row_offset + 2}  # 1-based, skip header
                for col_name, idx in col_indices:
                    row_data[col_name] = str(row[idx]) if idx < len(row) else ""
                matched.append(row_data)

        return jsonify({"status": "success", "rows": matched}), 200

    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except HttpError as e:
        logger.exception("Google Sheets API error in /read")
        return jsonify({"status": "error", "message": str(e)}), 502
    except Exception as e:
        logger.exception("Unexpected error in /read")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/write", methods=["POST"])
def write():
    """
    Write to a Google Sheet. Supports two modes:

    APPEND (newrow=yes): Appends a new row. Finds the next functionally-empty
    row by scanning the key column for a blank value — correctly handles sheets
    with array formulas or dropdown lists.

    UPDATE (newrow=no): Finds an existing row where the key column matches the
    key value, then writes only the specified columns into that row.

    The payload format mirrors the existing Apps Script convention: data columns
    are sent flat in the body alongside metadata fields. Reserved fields
    (password, sheetid, tab, newrow, rewrite, key, prepend) are stripped out;
    everything else is treated as column → value pairs to write.

    Request body (JSON):
    {
        "password": "...",
        "sheetid": "<Google Sheet ID>",
        "tab": "<Tab Name>",
        "newrow": "yes" | "no",          // "yes" = append, "no" = update existing
        "key": "<column header>",        // key column name
        "<key>": "<value>",              // key value (used for update mode lookup,
                                         // and also written to the row in append mode)
        "Column Header 1": "value1",     // data columns to write (flat, not nested)
        "Column Header 2": "value2",
        "rewrite": "no",                 // accepted but ignored (legacy compat)
        "prepend": false                 // append mode only: insert at top instead of bottom
    }

    Response:
    {
        "status": "success",
        "row": <row number written>,
        "matched": <number of rows updated>  // update mode only
    }
    """
    # Fields that are not data columns
    RESERVED = {"password", "sheetid", "tab", "newrow", "rewrite", "key", "prepend"}

    body = request.get_json(force=True, silent=True) or {}

    if not check_password(body):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    sheet_id = body.get("sheetid")
    tab = body.get("tab")
    key_col = body.get("key")
    newrow = str(body.get("newrow", "yes")).lower()
    prepend = body.get("prepend", False)

    if not sheet_id or not tab:
        return jsonify({"status": "error", "message": "sheetid and tab are required"}), 400
    if not key_col:
        return jsonify({"status": "error", "message": "key is required"}), 400

    # key_col may be a list in some legacy payloads — normalise to string
    if isinstance(key_col, list):
        key_col = key_col[0]

    # Extract data columns: everything not in RESERVED
    data = {k: v for k, v in body.items() if k not in RESERVED}

    if not data:
        return jsonify({"status": "error", "message": "No data columns found in request body"}), 400

    try:
        service = get_sheets_client()
        headers = get_header_row(service, sheet_id, tab)
        if not headers:
            return jsonify({"status": "error", "message": "Sheet has no header row"}), 400

        key_col_index = find_key_col_index(headers, key_col)

        # ------------------------------------------------------------------
        # UPDATE MODE: find existing row(s) by key and patch columns in place
        # ------------------------------------------------------------------
        if newrow == "no":
            key_value = str(data.get(key_col, "")).strip()
            if not key_value:
                return jsonify({"status": "error", "message": f"Key value for '{key_col}' not found in body"}), 400

            # Fetch key column to find matching rows
            col = col_letter(key_col_index + 1)
            range_name = f"'{tab}'!{col}2:{col}100000"
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=range_name,
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute()
            key_values = result.get("values", [])

            matched_rows = []
            for i, row in enumerate(key_values):
                cell = str(row[0]).strip() if row else ""
                if cell == key_value:
                    matched_rows.append(i + 2)  # 1-based, skip header

            if not matched_rows:
                return jsonify({"status": "error", "message": f"No row found where '{key_col}' = '{key_value}'"}), 404

            # For each matched row, write only the data columns that exist in headers
            # Use individual cell updates to avoid overwriting columns we're not touching
            value_updates = []
            for target_row in matched_rows:
                for col_name, value in data.items():
                    try:
                        col_idx = find_key_col_index(headers, col_name)
                        cell_range = f"'{tab}'!{col_letter(col_idx + 1)}{target_row}"
                        value_updates.append({
                            "range": cell_range,
                            "values": [[value]]
                        })
                    except ValueError:
                        logger.warning("Column '%s' not found in headers — skipping", col_name)

            if value_updates:
                service.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={
                        "valueInputOption": "USER_ENTERED",
                        "data": value_updates,
                    },
                ).execute()

            return jsonify({"status": "success", "matched": len(matched_rows), "rows": matched_rows}), 200

        # ------------------------------------------------------------------
        # APPEND MODE: write a new row
        # ------------------------------------------------------------------
        if prepend:
            target_row = 2
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "requests": [{
                        "insertDimension": {
                            "range": {
                                "sheetId": _get_sheet_gid(service, sheet_id, tab),
                                "dimension": "ROWS",
                                "startIndex": 1,
                                "endIndex": 2,
                            },
                            "inheritFromBefore": False,
                        }
                    }]
                },
            ).execute()
        else:
            target_row = find_next_empty_row(service, sheet_id, tab, key_col_index)

        # Build full row array aligned to header positions
        row_values = [""] * len(headers)
        for col_name, value in data.items():
            try:
                idx = find_key_col_index(headers, col_name)
                row_values[idx] = value
            except ValueError:
                logger.warning("Column '%s' not found in headers — skipping", col_name)

        range_name = f"'{tab}'!A{target_row}"
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body={"values": [row_values]},
        ).execute()

        return jsonify({"status": "success", "row": target_row}), 200

    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except HttpError as e:
        logger.exception("Google Sheets API error in /write")
        return jsonify({"status": "error", "message": str(e)}), 502
    except Exception as e:
        logger.exception("Unexpected error in /write")
        return jsonify({"status": "error", "message": str(e)}), 500


def _get_sheet_gid(service, sheet_id: str, tab: str) -> int:
    """Return the numeric sheetId (gid) for a tab by name."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["title"] == tab:
            return sheet["properties"]["sheetId"]
    raise ValueError(f"Tab '{tab}' not found in spreadsheet")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
