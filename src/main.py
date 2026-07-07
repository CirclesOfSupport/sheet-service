import os
import re
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
# /coordinates grid cache (TTL, per-instance)
# ---------------------------------------------------------------------------
# Every /coordinates call reads an entire tab ('<tab>'!A1:ZZ) into memory and
# resolves all column/row lookups against that in-memory grid. Without caching,
# each call re-reads from the Sheets API. Under a cohort burst (many subscribers
# entering a check-in flow at once, each firing Get Org Info / Offboarded /
# Fetch Resources V4 -> /coordinates), this exhausts the Sheets API read quota:
#   ReadRequestsPerMinutePerUser = 60, per-user (all reads bill to the single
#   OAuth user), which returns HTTP 429 -> surfaced to TextIt as 502.
# Confirmed 2026-07-06: an FSU-COM-PA cohort burst blew the 60/min ceiling.
#
# This cache holds each tab's grid keyed by (sheet_id, tab_name) for a short
# TTL, collapsing a burst to ~1 Sheets read per distinct tab per TTL window.
# The tabs read by /coordinates in production (Resources, SelfHelp, PastClients)
# are static, human-edited config -- no flow writes-then-immediately-reads them
# -- so a uniform TTL is safe. Staleness is bounded by the TTL: a manual sheet
# edit becomes visible within COORDINATES_CACHE_TTL seconds.
#
# Per-instance: the cache lives in each Cloud Run instance's memory, so a burst
# fanned across N instances yields ~N reads/tab/TTL, not 1 -- still a large
# reduction, sufficient to stay under quota. A shared cache (Memorystore) is
# unnecessary at this scale.
#
# Scope: /coordinates ONLY. /read and /write are unaffected.

import time
import threading

COORDINATES_CACHE_TTL = int(os.environ.get("COORDINATES_CACHE_TTL", "300"))

# key: (sheet_id, tab_name) -> (fetched_at_epoch, grid)
_grid_cache: dict[tuple[str, str], tuple[float, list]] = {}
_grid_cache_lock = threading.Lock()


def get_cached_grid(service, sheet_id: str, tab_name: str, bypass: bool = False) -> list:
    """Return the tab's grid ('<tab>'!A1:ZZ) from cache if fresh, else read it
    from Sheets and refresh the cache.

    The read + refresh is guarded by a lock so a concurrent burst for the same
    tab triggers a single Sheets read (the first caller refreshes; the rest
    reuse the freshly-cached grid) rather than a thundering herd of reads that
    would itself spend quota.

    bypass=True forces a fresh read (and refreshes the cache) -- used by the
    request-level "nocache" flag to verify a sheet edit without waiting out
    the TTL.
    """
    cache_key = (sheet_id, tab_name)
    now = time.time()

    if not bypass:
        cached = _grid_cache.get(cache_key)
        if cached is not None and (now - cached[0]) < COORDINATES_CACHE_TTL:
            return cached[1]

    with _grid_cache_lock:
        # Re-check under lock: another thread may have refreshed while we waited.
        if not bypass:
            cached = _grid_cache.get(cache_key)
            if cached is not None and (time.time() - cached[0]) < COORDINATES_CACHE_TTL:
                return cached[1]

        rng = f"'{tab_name}'!A1:ZZ"
        res = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=rng,
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        grid = res.get("values", [])
        _grid_cache[cache_key] = (time.time(), grid)
        return grid


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


def segment_value(value: str, result_name: str, max_chars: int = 500) -> dict:
    """
    Reproduce the CANONICAL (fixed) resource-map Apps Script segmentation EXACTLY.

    Ported verbatim from the Copy sheet's doPost (the fixed version that the
    migration targets), captured 2026-06-23:

        sections = cellContent.split(<blank-line OR before (N) >)
                              .map(s => s.trim())
                              .filter(Boolean);     // empties DROPPED
        // pack sections, joining with "\n\n", into <= MAX (500) segments;
        // a section longer than MAX is hard-split into MAX-sized pieces.

    Key point that bit an earlier version: the canonical script FILTERS OUT empty
    sections (`.filter(Boolean)`). It does NOT keep an empty segment for blank
    lines (leading or interior). An earlier "keep empty sections" attempt
    produced _segments=11 with empty alternating _2/_4/_6... for multi-item
    cells; that was wrong. Verified against wsu/sleep (5 numbered items ->
    2 segments, 442 + 308 chars, no empties).

    NOTE: production's OLD gmail-sheet script has a BROKEN regex that
    matches nothing and returns ["", full_text] (segments=2, _1=""). We are NOT
    matching that; we match the FIXED Copy script, which is the sheet being
    migrated to and the intended behavior.

    Emits:
      <result_name>            -> full original value
      <result_name>_<n>        -> 1-based segment text
      <result_name>_segments   -> integer count of segments
    """
    text = value if value is not None else ""
    out = {result_name: text}

    parts = re.split(r"\n\s*\n|(?=\(\d+\)\s)", text)
    sections = [p.strip() for p in parts]
    sections = [p for p in sections if p]  # filter(Boolean) -- drop empties

    segments = []
    segment = ""

    def push_with_hard_split(t):
        while len(t) > max_chars:
            segments.append(t[:max_chars].strip())
            t = t[max_chars:]
        if t.strip():
            segments.append(t.strip())

    for section in sections:
        if len(section) > max_chars:
            if segment.strip():
                segments.append(segment.strip())
            segment = ""
            push_with_hard_split(section)
            continue
        candidate = (segment + "\n\n" + section) if segment else section
        if len(candidate) > max_chars:
            if segment.strip():
                segments.append(segment.strip())
            segment = section
        else:
            segment = candidate

    if segment.strip():
        segments.append(segment.strip())

    for i, seg in enumerate(segments):
        out[f"{result_name}_{i + 1}"] = seg
    out[f"{result_name}_segments"] = len(segments)
    return out


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


@app.route("/coordinates", methods=["POST"])
def coordinates():
    """
    Column x row INTERSECTION lookup with segmentation -- mirrors the legacy
    'Fetch' Apps Script doPost (resource_map_webapp_internal) so Fetch Resources
    V4 can re-point here by URL only, with no change to its request body or to
    the downstream nodes that consume the segmented response.

    Request body (JSON) -- identical to the Apps Script contract, plus sheetid:
    {
        "password": "WhoWhatNow?42?!",
        "sheetid": "<Google Sheet ID>",   // see note below
        "coordinates": [
            {
                "sheet": "<Tab Name>",
                "column": "<column header, case-insensitive>",
                "row": "<row key in first column, case-insensitive>" OR "_<N>"
                       for a 1-based literal row index,
                "result_name": "<key to return the cell value under>"
            }
        ]
    }

    sheetid: the Apps Script used its BOUND spreadsheet and took no sheetid.
    Cloud Run has no 'active' spreadsheet, so the canonical resource sheet id
    must be supplied -- in the body as "sheetid", or via the RESOURCE_SHEET_ID
    env var as a default. This is the ONE contract addition vs. the Apps Script.

    Tab grids are cached per-instance for COORDINATES_CACHE_TTL seconds (default
    300) keyed by (sheet_id, tab) to stay under the Sheets API read quota under
    burst load -- see the /coordinates grid cache section above. Optional body
    flag "nocache": true forces a fresh read for that call (and refreshes the
    cache), for verifying a sheet edit without waiting out the TTL.

    Per-coordinate resolution matches Code.gs:
      - Column: case-insensitive findIndex over the first row.
                Miss -> the coordinate's keys are OMITTED from the response.
      - Row: '_N' -> 1-based literal row index; else case-insensitive findIndex
             over the entire first column. Miss -> keys OMITTED.
      - Single cell at (row, column), then segment_value() (MAX=500), producing
        result_name, result_name_<n>, result_name_segments.

    On a lookup miss the live Apps Script does NOT return an error string under
    the result_name -- it omits that key (an all-miss request returns `{}`).
    Verified against the live endpoint 2026-06-18.

    Apps-Script-parity behaviors:
      - A row/column miss is NOT an HTTP error: HTTP 200, miss string segmented
        under result_name (so a content miss is body-distinguishable only, never
        by status -- same as before).
      - Bad password returns 403 (this service's convention). The flow sends the
        correct password, so production never hits this path.

    Response: HTTP 200, flat JSON merging every coordinate's segmented keys.
    """
    body = request.get_json(force=True, silent=True) or {}

    if not check_password(body):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    coords = body.get("coordinates")
    if isinstance(coords, dict):
        coords = [coords]  # Apps Script normalised a single object to an array
    if not isinstance(coords, list):
        return jsonify({"status": "error", "message": "coordinates array is required"}), 400

    sheet_id = body.get("sheetid") or os.environ.get("RESOURCE_SHEET_ID")
    if not sheet_id:
        return jsonify({
            "status": "error",
            "message": "sheetid not provided and RESOURCE_SHEET_ID not configured",
        }), 400

    # Optional per-request cache bypass: force a fresh Sheets read (and refresh
    # the module cache) for this call. Lets an operator verify a sheet edit
    # landed without waiting out the TTL. Accepts true / "true" (any case).
    nocache = str(body.get("nocache", False)).strip().lower() == "true"

    try:
        service = get_sheets_client()
        response = {}
        # Per-request memo so a single request that references the same tab in
        # multiple coordinates does exactly one cache lookup for it; the module
        # cache (get_cached_grid) is what spans requests and bounds Sheets reads.
        request_grids = {}

        def get_grid(tab_name: str):
            if tab_name not in request_grids:
                request_grids[tab_name] = get_cached_grid(
                    service, sheet_id, tab_name, bypass=nocache
                )
            return request_grids[tab_name]

        for coord in coords:
            tab = coord.get("sheet")
            column = coord.get("column")
            row_key = coord.get("row")
            result_name = coord.get("result_name")

            if not result_name:
                continue

            grid = get_grid(tab) if tab else []
            if not grid:
                # Apps Script omits the key on a failed lookup; do the same.
                continue

            first_row = [str(c) for c in grid[0]]
            col_idx = next(
                (i for i, h in enumerate(first_row)
                 if h.strip().lower() == str(column).strip().lower()),
                -1,
            )
            if col_idx == -1:
                # Column miss -> omit the key (matches live Apps Script behavior).
                continue

            row_idx = -1
            rk = str(row_key) if row_key is not None else ""
            if rk.startswith("_"):
                try:
                    literal = int(rk[1:])
                    if 1 <= literal <= len(grid):
                        row_idx = literal - 1
                except ValueError:
                    row_idx = -1
            else:
                first_col = [str(r[0]) if len(r) > 0 else "" for r in grid]
                row_idx = next(
                    (i for i, v in enumerate(first_col)
                     if v.strip().lower() == rk.strip().lower()),
                    -1,
                )

            if row_idx == -1:
                # Row miss -> omit the key (matches live Apps Script behavior).
                continue

            target = grid[row_idx]
            cell = str(target[col_idx]) if col_idx < len(target) else ""
            response.update(segment_value(cell, result_name))

        return jsonify(response), 200

    except HttpError as e:
        logger.exception("Google Sheets API error in /coordinates")
        return jsonify({"status": "error", "message": str(e)}), 502
    except Exception as e:
        logger.exception("Unexpected error in /coordinates")
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
