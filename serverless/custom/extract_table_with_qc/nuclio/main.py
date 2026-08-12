import asyncio
import difflib
import json
import os
import re
import traceback
from typing import cast
from urllib.parse import quote
import base64
import io
from PIL import Image


import httpx
import mlflow
import mlflow.langchain
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from httpx import HTTPError
from langchain_core.messages import HumanMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_openai import ChatOpenAI
from nuclio_sdk.context import Context
from nuclio_sdk.logger import Logger
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

SERVICE_ACCOUNT_FILE = "/opt/nuclio/creds/cvat-sheets-integration.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

QC_DIFF_SHEET_TITLE = "QC Diff"

TASK_INSTRUCTION = (
    "Extract every row in the given table image, including table column headers as the "
    "first row whenever present. For each row, return an array of strings representing "
    "the values of each cell in that row."
)

try:
    mlflow.set_experiment("cvat_extract_table_with_qc")
    mlflow.langchain.autolog()
except Exception as e:
    print(f"Warning: Failed to initialize MLflow experiment: {e}")


def log_before_retry(retry_state):
    print(f"Attempt {retry_state.attempt_number} failed:")
    traceback.print_exception(retry_state.outcome.exception())


default_retry = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=60, jitter=2),
    retry=retry_if_exception_type(HTTPError),
    before_sleep=log_before_retry,
)


class TableExtraction(BaseModel):
    """
    Extracted table data from a document image.
    Each row is a list of cell values as strings.
    """

    rows: list[list[str]] = Field(
        description=(
            "List of rows extracted from the table. "
            "Each row is a list of cell values as strings."
        )
    )


def get_logger(context: Context) -> Logger:
    return cast(Logger, context.logger)


class ContextVariables:
    def __init__(self, vlm: ChatOpenAI, mistral_vlm: ChatOpenAI):
        self.vlm = vlm
        self.mistral_vlm = mistral_vlm
        self._creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        self._client: httpx.AsyncClient | None = None
        self._client_lock: asyncio.Lock = asyncio.Lock()

    async def _build_new_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if not self._creds.expired and self._client is not None:
                return self._client

            await asyncio.to_thread(self._creds.refresh, Request())
            self._client = httpx.AsyncClient(
                base_url="https://sheets.googleapis.com/",
                headers={"Authorization": f"Bearer {self._creds.token}"},
                http2=True,
                timeout=60,
            )
            return self._client

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._creds.expired:
            return self._client

        self._client = await self._build_new_client()
        return self._client

    @staticmethod
    def get_cvars(context: Context) -> "ContextVariables":
        return getattr(context.user_data, "cvars")

    def set_cvars(self, context: Context) -> None:
        setattr(context.user_data, "cvars", self)


def init_context(context: Context):
    """
    Runs once when the container starts.
    Initializes the VLM and an authenticated Sheets HTTP client.
    """
    logger = get_logger(context)
    logger.info("extract-table: Initializing VLM and Sheets client...")

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"Credentials file not found at {SERVICE_ACCOUNT_FILE}")

    rate_limiter = InMemoryRateLimiter(
        requests_per_second=5,
        check_every_n_seconds=0.1,
        max_bucket_size=5,
    )

    vlm = ChatOpenAI(
        model="google/gemini-3.1-flash-lite",
        base_url="https://openrouter.ai/api/v1",
        temperature=0.1,
        rate_limiter=rate_limiter,
        max_completion_tokens=10000,
    )

    mistral_vlm = ChatOpenAI(
        model="mistralai/mistral-small-2603",
        base_url="https://openrouter.ai/api/v1",
        temperature=0.1,
        rate_limiter=rate_limiter,
        max_completion_tokens=10000,
    )

    ContextVariables(vlm, mistral_vlm).set_cvars(context)
    logger.info("extract-table: initialization complete.")


def col_letter(n: int) -> str:
    """
    Converts a 1-based column index to A1 notation letters.
    Example: 1 -> A, 26 -> Z, 27 -> AA.
    """
    result = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


@default_retry
async def clear_sheet_values(
    cvars: ContextVariables,
    logger: Logger,
    spreadsheet_id: str,
    sheet_range: str,
) -> None:
    """
    Clears a values range via the Sheets API.
    Retries transient HTTP failures via tenacity.
    """
    client = await cvars.get_client()
    encoded_range = quote(sheet_range, safe="")
    response = await client.post(
        f"/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}:clear",
    )
    response.raise_for_status()
    logger.info(
        "clear_sheet_values spreadsheet_id=%s range=%s status_code=%d",
        spreadsheet_id,
        sheet_range,
        response.status_code,
    )


@default_retry
async def update_sheet_values(
    cvars: ContextVariables,
    logger: Logger,
    spreadsheet_id: str,
    sheet_range: str,
    values: list[list[str]],
) -> None:
    """
    Writes a values range via the Sheets API.
    Retries transient HTTP failures via tenacity.
    """
    client = await cvars.get_client()
    encoded_range = quote(sheet_range, safe="")
    response = await client.put(
        f"/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}",
        params={"valueInputOption": "RAW"},
        json={"values": values},
    )
    response.raise_for_status()
    logger.info(
        "update_sheet_values spreadsheet_id=%s range=%s status_code=%d",
        spreadsheet_id,
        sheet_range,
        response.status_code,
    )


async def run_gemini_vlm_extraction(
    cvars: ContextVariables,
    image_b64: str,
) -> list[list[str]]:
    """
    Queries the VLM with structured output for table rows.
    Returns a list of cell-value arrays.
    """

    prompt_text = (
    f"Core Objective: {TASK_INSTRUCTION}\n\n"
    "You are a document information extraction engine specialized in parsing table data from document images.\n\n"
    "Extraction Rules:\n"
    "1. Identify every row within the document table image, including any table column header rows as the first row.\n"
    "2. For each extracted row, return a list of strings representing the values of each cell in that row without adding extra spaces\n"
    "3. Preserve each cell value exactly as it appears in the document including empty cells and dashes.\n"
    "4. Do not paraphrase, normalize, or infer missing content.\n"
    "5. The order of the cell values in the list must match the visual order of the cells in the row. "
    "If a cell spans multiple visual lines, reconstruct it in natural reading order (top-to-bottom, left-to-right). "
    "When joining consecutive lines:\n"
    "- Do NOT automatically insert a space.\n"
    "- Join the text exactly as if the newline character were deleted.\n"
    "- Only insert a space if it is visually present in the document or if the first line already ends with a space.\n"
    "6. If a cell is empty, return an empty string for that cell.\n"
    "7. Do not attempt to infer or fill in missing values.\n"
    "8. Do not hallucinate or invent any content.\n"
    "9. Ignore non-table elements such as document page headers, page footers, logos, watermarks, and unrelated text. "
    "Do not skip table column header rows within the table.\n"
    "10. Do not output conversational text, preambles, explanations, or chain-of-thought markdown blocks.\n"
)
    message = HumanMessage(
        content=[
            {"type": "text", "text": prompt_text},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_b64}"},
            },
        ]
    )
    structured_vlm = cvars.vlm.with_structured_output(TableExtraction)

    with mlflow.start_span(name="extract_table_vlm_call"):
        result = await structured_vlm.ainvoke([message])

    return result.rows


@default_retry
async def run_mistral_ocr_extraction(logger: Logger, image_b64: str) -> list[list[str]]:
    """
    Calls the Mistral OCR API with structured output (document_annotation)
    to extract table rows from a base64-encoded image.
    Returns a list of cell-value arrays.
    """

    api_key = os.environ["MISTRAL_API_KEY"]
    image_uri = f"data:image/png;base64,{image_b64}"

    payload = {
        "model": "mistral-ocr-latest",
        "document": {
            "type": "image_url",
            "image_url": image_uri,
        }
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.mistral.ai/v1/ocr",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()

    result = resp.json()
    logger.info(json.dumps(result, indent=4))

    markdown_pages = result.get("pages", [])
    markdown = ""
    if markdown_pages:
        markdown = markdown_pages[0].get("markdown", "")
    convert_to_rows_list = parse_markdown(markdown)

    return convert_to_rows_list


def parse_markdown(markdown: str) -> list[list[str]]:
    """
    Extracts rows from Markdown tables in the text.
    Returns a 2D list matching your [[cell, cell], [cell, cell]] format.
    """
    rows = []
    lines = markdown.strip().split("\n")

    for line in lines:
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            if re.match(r"^\|[\s:\-|]+\|$", line):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            rows.append(cells)

    return rows


# ---------------------------------------------------------------------------
# OCR QC: flatten, greedy match, diff
# ---------------------------------------------------------------------------

# Values treated as "ignorable" — QC team handles these manually
_SKIP_VALUES = frozenset(("", "-", "—", "–"))


def normalize_spaces_between_digits(rows: list[list[str]]) -> list[list[str]]:
    """
    Parses each string in the 2D list to replace spaces between numbers with ''.
    """
    pattern = re.compile(r'(?<=\d)\s+(?=\d)')
    return [
        [pattern.sub('', cell) if isinstance(cell, str) else cell for cell in row]
        for row in rows
    ]


def flatten_with_coords(
    rows: list[list[str]],
) -> list[tuple[str, int, int]]:
    """
    Flatten 2D rows into a 1D list of (value, row_idx, col_idx).
    """
    return [
        (val, i, j)
        for i, row in enumerate(rows)
        for j, val in enumerate(row)
    ]


def _greedy_best_match(
    word_from_op1: str,
    candidates: list[str],
) -> tuple[int, float]:
    """
    Find the candidate with the highest SequenceMatcher ratio to *word_from_op1*.
    Returns (candidate_index_in_list, ratio).  -1 if candidates is empty.
    """
    best_idx = -1
    best_ratio = -1.0
    # Reuse a single matcher — set_seq1 only once, vary seq2
    sm = difflib.SequenceMatcher(None, word_from_op1, "")
    for idx, cand in enumerate(candidates):
        sm.set_seq2(cand)
        ratio = sm.ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = idx
    return best_idx, max(0.0, best_ratio)


# Thresholds
_PERFECT_THRESHOLD = 1.0

# Annotation types
_TYPE_PARTIAL = "partial"  # char-level diff shown on QC Diff sheet
_TYPE_POOR = "poor"        # light-red cell bg on first sheet

# Colours (RGB 0-1 for Google Sheets API)
_BG_QC = {"red": 0.7, "green": 0.9, "blue": 1.0}          # light blue bg

_BLACK = {"red": 0.0, "green": 0.0, "blue": 0.0}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}

_TEXT_RED = {"red": 1.0, "green": 0.0, "blue": 0.0}  # Standard full red
_TEXT_GREEN = {"red": 0.2039, "green": 0.6588, "blue": 0.3255}  # Custom green

_BG_GRAY = {"red": 0.95, "green": 0.95, "blue": 0.95}   # light grey 
_BG_YELLOW = {"red": 1.0, "green": 1.0, "blue": 0.8}      # yellow


def _paint_diff_span(fmts: list[dict], start: int, end: int, color: dict) -> None:
    """
    Colour chars in [start, end). For a zero-width span (start == end), colour
    the character at that index (or the previous one if the index is past the end).
    """
    if start < end:
        for idx in range(start, end):
            fmts[idx] = color
        return
    if not fmts:
        return
    idx = start if start < len(fmts) else start - 1
    if 0 <= idx < len(fmts):
        fmts[idx] = color


def _formats_to_text_runs(fmts: list[dict]) -> list[dict]:
    """Collapse per-character colour/bold formatting into Sheets textFormatRuns."""
    runs: list[dict] = []
    current_format = None
    for idx, fmt in enumerate(fmts):
        if fmt != current_format:
            text_format = {"foregroundColor": fmt}
            if fmt == _TEXT_RED or fmt == _TEXT_GREEN:
                text_format["bold"] = True
            runs.append({"startIndex": idx, "format": text_format})
            current_format = fmt
    return runs


def _build_diff_format_runs(
    primary_val: str,
    matched_val: str,
) -> tuple[list[dict], list[dict]]:
    """
    Build textFormatRuns for both primary and matched strings.

    Uses SequenceMatcher(None, primary_val, matched_val):
    - insert / replace → green on both sides
    - delete → red on both sides
    Zero-width sides (i1==i2 or j1==j2) still get the index highlighted.
    """
    sm = difflib.SequenceMatcher(None, primary_val, matched_val)
    primary_fmts = [_BLACK] * len(primary_val)
    matched_fmts = [_BLACK] * len(matched_val)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        color = _TEXT_RED if tag == "delete" else _TEXT_GREEN
        _paint_diff_span(primary_fmts, i1, i2, color)
        _paint_diff_span(matched_fmts, j1, j2, color)

    return _formats_to_text_runs(primary_fmts), _formats_to_text_runs(matched_fmts)


def run_qc_comparison(
    primary_rows: list[list[str]],
    secondary_rows: list[list[str]],
) -> list[dict]:
    """
    Compare *primary_rows* (written to the sheet) against *secondary_rows*
    using greedy longest-match.

    Empty / dash cells are skipped — the QC team handles those manually.

    Returns a list of annotation dicts, one per non-perfect cell:
        {"row": int, "col": int, "type": str, "matched_val": str, ...}
    Row/col are 0-based indices into primary_rows.
    """
    primary_flat = flatten_with_coords(primary_rows)

    if not primary_flat:
        return []

    # Candidates: only non-empty, non-dash secondary values
    candidates: list[str] = [
        val for val, _, _ in flatten_with_coords(secondary_rows)
        if val.strip() not in _SKIP_VALUES
    ]

    annotations: list[dict] = []

    for val, row, col in primary_flat:
        # Skip ignorable cells entirely — no matching, no annotation
        if val.strip() in _SKIP_VALUES:
            continue

        if not candidates:
            annotations.append({
                "row": row,
                "col": col,
                "type": _TYPE_POOR,
                "matched_val": "",
                "primary_val": val,
            })
            continue

        best_idx, ratio = _greedy_best_match(val, candidates)

        if ratio == _PERFECT_THRESHOLD:
            candidates.pop(best_idx)
            continue

        matched_val = candidates.pop(best_idx)

        # else : PARTIAL_MATCH
        primary_runs, matched_runs = _build_diff_format_runs(val, matched_val)
        annotations.append({
            "row": row,
            "col": col,
            "type": _TYPE_PARTIAL,
            "matched_val": matched_val,
            "primary_val": val,
            "primary_text_format_runs": primary_runs,
            "matched_text_format_runs": matched_runs,
        })
        

    return annotations


# ---------------------------------------------------------------------------
# Google Sheets formatting helpers
# ---------------------------------------------------------------------------

@default_retry
async def apply_qc_highlights(
    cvars: ContextVariables,
    logger: Logger,
    spreadsheet_id: str,
    primary_rows: list[list[str]],
    annotations: list[dict],
) -> None:
    """
    Highlight mismatched cells on the first sheet (sheetId 0).

    Only sets a light-red background — no notes, no red/green char formatting.
    Perfect / unannotated cells are reset to white bg.

    Data starts at row 2 (A2), so API startRowIndex = 1.
    """
    col_count = max((len(r) for r in primary_rows), default=0)
    data_row_count = len(primary_rows)
    if col_count == 0 or data_row_count == 0:
        return

    ann_map: dict[tuple[int, int], dict] = {
        (a["row"], a["col"]): a for a in annotations
    }

    grid_rows: list[dict] = []
    for r in range(data_row_count):
        row_values: list[dict] = []
        row_data = primary_rows[r] if r < len(primary_rows) else []
        for c in range(col_count):
            cell_val = row_data[c] if c < len(row_data) else ""
            ann = ann_map.get((r, c))
            bg = _BG_QC if ann is not None else _WHITE
            row_values.append({
                "userEnteredValue": {"stringValue": cell_val},
                "userEnteredFormat": {
                    "backgroundColor": bg,
                    "textFormat": {"foregroundColor": _BLACK},
                    "numberFormat": {"type": "TEXT"},
                },
                "note": "",
            })
        grid_rows.append({"values": row_values})

    request = {
        "updateCells": {
            "range": {
                "sheetId": 0,
                "startRowIndex": 1,                # A2
                "endRowIndex": 1 + data_row_count,
                "startColumnIndex": 0,
                "endColumnIndex": col_count,
            },
            "rows": grid_rows,
            "fields": (
                "userEnteredFormat.backgroundColor,"
                "userEnteredFormat.textFormat.foregroundColor,"
                "userEnteredFormat.numberFormat,"
                "note,"
                "textFormatRuns,"
                "userEnteredValue.stringValue"
            ),
        }
    }

    client = await cvars.get_client()
    response = await client.post(
        f"/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
        json={"requests": [request]},
    )
    response.raise_for_status()
    logger.info(
        "apply_qc_highlights spreadsheet_id=%s annotations=%d status=%d",
        spreadsheet_id,
        len(annotations),
        response.status_code,
    )


@default_retry
async def create_qc_diff_sheet(
    cvars: ContextVariables,
    logger: Logger,
    spreadsheet_id: str,
) -> int:
    """
    Create the 'QC Diff' sheet and return its sheetId.
    Caller guarantees no sheet with this title already exists.
    """
    client = await cvars.get_client()
    add_resp = await client.post(
        f"/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
        json={
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": QC_DIFF_SHEET_TITLE,
                            "index": 1,
                        }
                    }
                }
            ]
        },
    )
    add_resp.raise_for_status()
    sheet_id = add_resp.json()["replies"][0]["addSheet"]["properties"]["sheetId"]
    logger.info("create_qc_diff_sheet created sheetId=%s", sheet_id)
    return sheet_id


def _qc_diff_cell(
    value: str,
    bg: dict,
    text_format_runs: list[dict] | None = None,
) -> dict:
    """Build one QC Diff cell payload."""
    cell: dict = {
        "userEnteredValue": {"stringValue": value},
        "userEnteredFormat": {
            "backgroundColor": bg,
            "textFormat": {"foregroundColor": _BLACK},
            "numberFormat": {"type": "TEXT"},
        },
    }
    if text_format_runs:
        cell["textFormatRuns"] = text_format_runs
    return cell


def _build_qc_diff_grid(
    primary_rows: list[list[str]],
    annotations: list[dict],
) -> tuple[list[dict], int, int]:
    """
    Build updateCells rows for the QC Diff sheet.

    Layout per primary table row:
      - model1 values (char-level insert/replace=green, delete=red)
      - model2 matched values below (same colouring)
      - one blank spacer row

    Returns (grid_rows, total_sheet_rows, col_count).
    """
    col_count = max((len(r) for r in primary_rows), default=0)
    if col_count == 0:
        return [], 0, 0

    ann_map: dict[tuple[int, int], dict] = {
        (a["row"], a["col"]): a for a in annotations
    }

    grid_rows: list[dict] = [
        {
            "values": [
                {
                    "userEnteredValue": {"stringValue": "Model1 GEMINI FLITE 3.1"},
                    "userEnteredFormat": {
                        "textFormat": {"bold": True, "foregroundColor": _BLACK},
                        "numberFormat": {"type": "TEXT"},
                    },
                }
            ]
        },
        {
            "values": [
                {
                    "userEnteredValue": {
                        "stringValue": (
                            "Model2 MISTRAL OCR 4"
                        )
                    },
                    "userEnteredFormat": {
                        "textFormat": {"bold": True, "foregroundColor": _BLACK},
                        "numberFormat": {"type": "TEXT"},
                    },
                }
            ]
        },
        {"values": []},
    ]

    for r, row_data in enumerate(primary_rows):
        model1_cells: list[dict] = []
        model2_cells: list[dict] = []
        for c in range(col_count):
            cell_val = row_data[c] if c < len(row_data) else ""
            ann = ann_map.get((r, c))

            if ann is None:
                model1_cells.append(_qc_diff_cell(cell_val, _BG_GRAY))
                model2_cells.append(_qc_diff_cell("", _BG_GRAY))
                continue

            if ann["type"] == _TYPE_PARTIAL:
                model1_cells.append(
                    _qc_diff_cell(
                        cell_val,
                        _BG_YELLOW,
                        ann.get("primary_text_format_runs"),
                    )
                )
                model2_cells.append(
                    _qc_diff_cell(
                        ann.get("matched_val", ""),
                        _BG_YELLOW,
                        ann.get("matched_text_format_runs"),
                    )
                )
            else:
                model1_cells.append(_qc_diff_cell(cell_val, _BG_YELLOW))
                model2_cells.append(
                    _qc_diff_cell(ann.get("matched_val", ""), _BG_YELLOW)
                )

        grid_rows.append({"values": model1_cells})
        grid_rows.append({"values": model2_cells})
        grid_rows.append({"values": []})

    return grid_rows, len(grid_rows), col_count


@default_retry
async def write_qc_diff_sheet(
    cvars: ContextVariables,
    logger: Logger,
    spreadsheet_id: str,
    primary_rows: list[list[str]],
    annotations: list[dict],
) -> None:
    """
    Create the QC Diff sheet and write per-row model1 / model2 pairs with
    matching char-level colour highlights on both sides.
    """
    grid_rows, row_count, col_count = _build_qc_diff_grid(primary_rows, annotations)
    if row_count == 0 or col_count == 0:
        return

    sheet_id = await create_qc_diff_sheet(cvars, logger, spreadsheet_id)
    client = await cvars.get_client()
    response = await client.post(
        f"/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
        json={
            "requests": [
                {
                    "updateCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": row_count,
                            "startColumnIndex": 0,
                            "endColumnIndex": col_count,
                        },
                        "rows": grid_rows,
                        "fields": (
                            "userEnteredFormat.backgroundColor,"
                            "userEnteredFormat.textFormat,"
                            "userEnteredFormat.numberFormat,"
                            "textFormatRuns,"
                            "userEnteredValue.stringValue"
                        ),
                    }
                }
            ]
        },
    )
    response.raise_for_status()
    logger.info(
        "write_qc_diff_sheet spreadsheet_id=%s sheet_id=%s rows=%d status=%d",
        spreadsheet_id,
        sheet_id,
        row_count,
        response.status_code,
    )


async def map_into_sheet(
    cvars: ContextVariables,
    logger: Logger,
    spreadsheet_id: str,
    extracted_rows: list[list[str]],
) -> int:
    """
    Write extracted table rows into the sheet starting at A2.
    Clears the target range first, then writes compactly with no gaps.
    """
    max_cols = max((len(row) for row in extracted_rows), default=0)
    if max_cols == 0:
        return 0

    end_col = col_letter(max_cols)
    max_row = len(extracted_rows) + 1
    sheet_range = f"A2:{end_col}{max_row + 5}"

    await clear_sheet_values(cvars, logger, spreadsheet_id, sheet_range)
    await update_sheet_values(cvars, logger, spreadsheet_id, "A2", extracted_rows)

    return len(extracted_rows)


async def handler(context: Context, event):
    """
    Accepts an internal HTTP request from the sheet-populator orchestrator.
    Expects JSON: {"image_b64": "...", "spreadsheet_id": "..."}.
    Runs VLM extraction, populates the sheet, returns rows_updated.
    """
    cvars = ContextVariables.get_cvars(context)
    logger = get_logger(context)

    data = event.body
    if isinstance(data, (bytes, str)):
        data = json.loads(data if isinstance(data, str) else data.decode("utf-8"))

    image_b64 = data.get("image_b64")
    spreadsheet_id = data.get("spreadsheet_id")

    if not image_b64 or not spreadsheet_id:
        return context.Response(
            body=json.dumps(
                {"status": "error", "message": "Missing image_b64 or spreadsheet_id"}
            ),
            headers={"Content-Type": "application/json"},
            status_code=400,
        )

    try:
        image_bytes = base64.b64decode(image_b64)
        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
            if max(w, h) > 1080:
                scale = 1080 / max(w, h)
                new_size = (
                    round(w * scale),
                    round(h * scale),
                )
                resized_img = img.resize(new_size, Image.Resampling.LANCZOS)
                buffered = io.BytesIO()
                resized_img.save(buffered, format="PNG")
                image_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
                logger.info("IMAGE resized to %s", new_size)
            else:
                logger.info("IMAGE not resized, within limit: %s", (w, h))
    except Exception as e:
        logger.error("Failed to load or resize image: %s", traceback.format_exc())
        return context.Response(
            body=json.dumps(
                {"status": "error", "message": f"Invalid image data: {str(e)}"}
            ),
            headers={"Content-Type": "application/json"},
            status_code=400,
        )

    # Run both VLM extractions in parallel
    gemini_rows, mistral_rows = await asyncio.gather(
        run_gemini_vlm_extraction(cvars, image_b64),
        run_mistral_ocr_extraction(logger, image_b64),
    )

    gemini_rows = normalize_spaces_between_digits(gemini_rows)
    mistral_rows = normalize_spaces_between_digits(mistral_rows)

    # Write primary (O/p of first model) rows to the first sheet
    updated_count = await map_into_sheet(cvars, logger, spreadsheet_id, gemini_rows)

    # QC: highlight mismatches on sheet 1; show model2 diffs on a second sheet
    qc_annotations: list[dict] = []
    if gemini_rows and mistral_rows:
        qc_annotations = run_qc_comparison(gemini_rows, mistral_rows)
        await apply_qc_highlights(
            cvars, logger, spreadsheet_id, gemini_rows, qc_annotations
        )
        await write_qc_diff_sheet(
            cvars, logger, spreadsheet_id, gemini_rows, qc_annotations
        )

    logger.info(
        "extract-table: updated %d row(s), %d QC annotation(s) in %s",
        updated_count,
        len(qc_annotations),
        spreadsheet_id,
    )

    return context.Response(
        body=json.dumps(
            {
                "status": "success",
                "rows_updated": updated_count,
                "qc_annotations": len(qc_annotations),
            }
        ),
        headers={"Content-Type": "application/json"},
        status_code=200,
    )
