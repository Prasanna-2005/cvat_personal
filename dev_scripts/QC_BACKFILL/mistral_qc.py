#!/usr/bin/env python3
"""Run table OCR QC for an existing Google Sheet.

Unlike ``main.py``, this is a normal command-line script: it does not invoke
the Flash Lite model or write extracted data.  It reads the table already in
the first worksheet (starting at A2), runs Mistral OCR on the supplied image,
and writes the same QC highlights and ``QC Diff`` worksheet as the Nuclio
function.

Example:
    MISTRAL_API_KEY=... python only_qc.py \
        --sheet-id <spreadsheet-id> --image /path/to/table.png
"""

import base64
import difflib
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from PIL import Image

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
QC_DIFF_SHEET_TITLE = "QC Diff"
_SKIP_VALUES = frozenset(("", "-", "—", "–"))
_PERFECT_THRESHOLD = 1.0
_TYPE_PARTIAL = "partial"
_TYPE_POOR = "poor"

_BG_QC = {"red": 0.7, "green": 0.9, "blue": 1.0}
_BLACK = {"red": 0.0, "green": 0.0, "blue": 0.0}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_TEXT_RED = {"red": 1.0, "green": 0.0, "blue": 0.0}
_TEXT_GREEN = {"red": 0.2039, "green": 0.6588, "blue": 0.3255}
_BG_GRAY = {"red": 0.95, "green": 0.95, "blue": 0.95}
_BG_YELLOW = {"red": 1.0, "green": 1.0, "blue": 0.8}


def default_credentials_path() -> Path:
    """Use the repo credentials when run locally, otherwise Nuclio's mount."""
    return Path(__file__).resolve().parent / "cvat-sheets-integration.json"


retry_http = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=60, jitter=2),
    retry=retry_if_exception_type(httpx.HTTPError),
)


class SheetsClient:
    def __init__(self, credentials_file: Path):
        if not credentials_file.is_file():
            raise FileNotFoundError(f"Credentials file not found: {credentials_file}")
        self.credentials = service_account.Credentials.from_service_account_file(
            str(credentials_file), scopes=SCOPES
        )
        self._client = None

    def _refresh_token(self) -> None:
        if not self.credentials.valid:
            self.credentials.refresh(Request())

    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            base_url="https://sheets.googleapis.com", timeout=60, http2=True
        )

    def _ensure_client(self) -> httpx.Client:
        self._refresh_token()
        if self._client is None or getattr(self._client, "is_closed", False):
            self._client = self._new_client()
        return self._client

    @retry_http
    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        client = self._ensure_client()
        client.headers["Authorization"] = f"Bearer {self.credentials.token}"
        try:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPError:
            self._client = None
            raise


def normalize_spaces_between_digits(
    rows: list[list[str]]
) -> list[list[str]]:
    pattern = re.compile(r"(?<=\d)\s+(?=\d)")
    return [[pattern.sub("", cell) for cell in row] for row in rows]


def read_primary_rows(
    sheets: SheetsClient, spreadsheet_id: str
) -> tuple[int, list[list[str]]]:
    """
    Read the table already populated in the first worksheet, from A2.
    """
    metadata = sheets.request("GET", f"/v4/spreadsheets/{spreadsheet_id}").json()
    first_sheet = metadata["sheets"][0]["properties"]
    sheet_title = first_sheet["title"].replace("'", "''")
    sheet_range = quote(f"'{sheet_title}'!A2:ZZ", safe="")
    values = (
        sheets.request(
            "GET",
            f"/v4/spreadsheets/{spreadsheet_id}/values/{sheet_range}",
            params={"majorDimension": "ROWS"},
        )
        .json()
        .get("values", [])
    )
    rows = [[str(value) for value in row] for row in values]
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    if not rows:
        raise ValueError("No table data found in the first worksheet from A2 onward.")
    return first_sheet["sheetId"], rows


def image_to_data_uri(image: Path | bytes | BinaryIO) -> str:
    """Build a data-URI from a path or in-memory image bytes (no disk required)."""
    if isinstance(image, Path):
        if not image.is_file():
            raise FileNotFoundError(f"Image file not found: {image}")
        raw = image.read_bytes()
    else:
        if isinstance(image, (bytes, bytearray)):
            raw = bytes(image)
        else:
            raise ValueError(f"Invalid image type: {type(image)}")

    with Image.open(BytesIO(raw)) as img:
        if max(img.size) > 1080:
            scale = 1080 / max(img.size)
            img = img.resize(
                (round(img.width * scale), round(img.height * scale)),
                Image.Resampling.LANCZOS,
            )
            buf = BytesIO()
            img.save(buf, format="PNG")
            raw = buf.getvalue()
            mime_type = "image/png"
        else:
            mime_type = Image.MIME.get(img.format, "image/png")
    return f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"


@retry_http
def run_mistral_ocr(image_uri: str) -> list[list[str]]:
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY must be set.")
    response = httpx.post(
        "https://api.mistral.ai/v1/ocr",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "mistral-ocr-latest",
            "document": {"type": "image_url", "image_url": image_uri},
        },
        timeout=120,
    )
    response.raise_for_status()
    pages = response.json().get("pages", [])
    return parse_markdown(pages[0].get("markdown", "") if pages else "")


def parse_markdown(markdown: str) -> list[list[str]]:
    rows = []
    for line in markdown.strip().splitlines():
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            if re.match(r"^\|[\s:\-|]+\|$", line):
                continue
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def flatten_with_coords(rows: list[list[str]]) -> list[tuple[str, int, int]]:
    return [
        (value, row, col)
        for row, cells in enumerate(rows)
        for col, value in enumerate(cells)
    ]


def _formats_to_text_runs(formats: list[dict]) -> list[dict]:
    runs, current = [], None
    for index, text_format in enumerate(formats):
        if text_format != current:
            cell_format = {"foregroundColor": text_format}
            if text_format == _TEXT_RED or text_format == _TEXT_GREEN:
                cell_format["bold"] = True
            runs.append({"startIndex": index, "format": cell_format})
            current = text_format
    return runs


def _paint_diff_span(formats: list[dict], start: int, end: int, color: dict) -> None:
    if start < end:
        formats[start:end] = [color] * (end - start)
    elif formats:
        index = start if start < len(formats) else start - 1
        if index >= 0:
            formats[index] = color


def _build_diff_format_runs(
    primary: str, matched: str
) -> tuple[list[dict], list[dict]]:
    primary_formats, matched_formats = [_BLACK] * len(primary), [_BLACK] * len(matched)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, primary, matched
    ).get_opcodes():
        if tag != "equal":
            color = _TEXT_RED if tag == "delete" else _TEXT_GREEN
            _paint_diff_span(primary_formats, i1, i2, color)
            _paint_diff_span(matched_formats, j1, j2, color)
    return _formats_to_text_runs(primary_formats), _formats_to_text_runs(
        matched_formats
    )


def run_qc_comparison(
    primary_rows: list[list[str]], secondary_rows: list[list[str]]
) -> list[dict]:
    candidates = [
        value
        for value, _, _ in flatten_with_coords(secondary_rows)
        if value.strip() not in _SKIP_VALUES
    ]
    annotations = []
    for value, row, col in flatten_with_coords(primary_rows):
        if value.strip() in _SKIP_VALUES:
            continue
        if not candidates:
            annotations.append(
                {"row": row, "col": col, "type": _TYPE_POOR, "matched_val": ""}
            )
            continue
        match_index, ratio = max(
            enumerate(
                difflib.SequenceMatcher(None, value, candidate).ratio()
                for candidate in candidates
            ),
            key=lambda item: item[1],
        )
        matched = candidates.pop(match_index)
        if ratio == _PERFECT_THRESHOLD:
            continue
        primary_runs, matched_runs = _build_diff_format_runs(value, matched)
        annotations.append(
            {
                "row": row,
                "col": col,
                "type": _TYPE_PARTIAL,
                "matched_val": matched,
                "primary_text_format_runs": primary_runs,
                "matched_text_format_runs": matched_runs,
            }
        )
    return annotations


def apply_qc_highlights(
    sheets: SheetsClient,
    spreadsheet_id: str,
    sheet_id: int,
    primary_rows: list[list[str]],
    annotations: list[dict],
) -> None:
    col_count = max(map(len, primary_rows), default=0)
    annotation_map = {
        (annotation["row"], annotation["col"]): annotation for annotation in annotations
    }
    rows = []
    for row_index, row in enumerate(primary_rows):
        cells = []
        for col in range(col_count):
            value = row[col] if col < len(row) else ""
            cells.append(
                {
                    "userEnteredValue": {"stringValue": value},
                    "userEnteredFormat": {
                        "backgroundColor": _BG_QC
                        if (row_index, col) in annotation_map
                        else _WHITE,
                        "textFormat": {"foregroundColor": _BLACK},
                        "numberFormat": {"type": "TEXT"},
                    },
                    "note": "",
                }
            )
        rows.append({"values": cells})
    sheets.request(
        "POST",
        f"/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
        json={
            "requests": [
                {
                    "updateCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": len(primary_rows) + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": col_count,
                        },
                        "rows": rows,
                        "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.foregroundColor,userEnteredFormat.numberFormat,note,textFormatRuns,userEnteredValue.stringValue",
                    }
                }
            ]
        },
    )


def qc_diff_cell(value: str, background: dict, runs: list[dict] | None = None) -> dict:
    cell = {
        "userEnteredValue": {"stringValue": value},
        "userEnteredFormat": {
            "backgroundColor": background,
            "textFormat": {"foregroundColor": _BLACK},
            "numberFormat": {"type": "TEXT"},
        },
    }
    if runs:
        cell["textFormatRuns"] = runs
    return cell


def write_qc_diff_sheet(
    sheets: SheetsClient,
    spreadsheet_id: str,
    primary_rows: list[list[str]],
    annotations: list[dict],
) -> None:
    metadata = sheets.request("GET", f"/v4/spreadsheets/{spreadsheet_id}").json()
    sheet_id = next(
        (
            sheet["properties"]["sheetId"]
            for sheet in metadata["sheets"]
            if sheet["properties"]["title"] == QC_DIFF_SHEET_TITLE
        ),
        None,
    )

    prep_requests: list[dict] = []
    if sheet_id is None:
        sheet_id = sheets.request(
            "POST",
            f"/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
            json={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {"title": QC_DIFF_SHEET_TITLE, "index": 1}
                        }
                    }
                ]
            },
        ).json()["replies"][0]["addSheet"]["properties"]["sheetId"]
    else:
        # Wipe existing QC Diff contents before rewriting.
        prep_requests.append(
            {
                "updateCells": {
                    "range": {"sheetId": sheet_id},
                    "fields": "userEnteredValue,userEnteredFormat,note,textFormatRuns",
                }
            }
        )

    col_count = max(map(len, primary_rows), default=0)
    annotation_map = {
        (annotation["row"], annotation["col"]): annotation for annotation in annotations
    }
    rows = [
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
                    "userEnteredValue": {"stringValue": "Model2 MISTRAL OCR 4"},
                    "userEnteredFormat": {
                        "textFormat": {"bold": True, "foregroundColor": _BLACK},
                        "numberFormat": {"type": "TEXT"},
                    },
                }
            ]
        },
        {"values": []},
    ]
    for row_index, row in enumerate(primary_rows):
        primary_cells, secondary_cells = [], []
        for col in range(col_count):
            value, annotation = (
                (row[col] if col < len(row) else ""),
                annotation_map.get((row_index, col)),
            )
            if annotation is None:
                primary_cells.append(qc_diff_cell(value, _BG_GRAY))
                secondary_cells.append(qc_diff_cell("", _BG_GRAY))
            elif annotation["type"] == _TYPE_PARTIAL:
                primary_cells.append(
                    qc_diff_cell(
                        value, _BG_YELLOW, annotation["primary_text_format_runs"]
                    )
                )
                secondary_cells.append(
                    qc_diff_cell(
                        annotation["matched_val"],
                        _BG_YELLOW,
                        annotation["matched_text_format_runs"],
                    )
                )
            else:
                primary_cells.append(qc_diff_cell(value, _BG_YELLOW))
                secondary_cells.append(
                    qc_diff_cell(annotation["matched_val"], _BG_YELLOW)
                )
        rows.extend(
            ({"values": primary_cells}, {"values": secondary_cells}, {"values": []})
        )

    sheets.request(
        "POST",
        f"/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
        json={
            "requests": [
                *prep_requests,
                {
                    "updateCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": len(rows),
                            "startColumnIndex": 0,
                            "endColumnIndex": max(col_count, 1),
                        },
                        "rows": rows,
                        "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat,userEnteredFormat.numberFormat,textFormatRuns,userEnteredValue.stringValue",
                    }
                },
            ]
        },
    )


def run_table_qc(
    sheet_id: str,
    image: Path | bytes,
    credentials: Path | None = None,
    *,
    task_id: int | None = None,
    job_id: int | None = None,
    frame: int | None = None,
    object_id: int | None = None,
) -> dict[str, Any]:
    """
    Run Mistral OCR QC against an existing Google Sheet using a table crop image.

    ``image`` may be a filesystem path or in-memory PNG/JPEG bytes / file-like object.
    Returns a result dict with status, rows_checked, and qc_annotations.
    Emits exactly two lines: QC start / QC end (with task/job/frame when provided).
    """
    loc = f"task={task_id} job={job_id} frame={frame} obj={object_id}"
    print(f"QC start  {loc}", flush=True)

    credentials_path = credentials or default_credentials_path()
    image_uri = image_to_data_uri(image)
    # Drop large binary as soon as the URI exists (URI holds the payload for OCR).
    del image

    mistral_rows = normalize_spaces_between_digits(run_mistral_ocr(image_uri))
    if not mistral_rows:
        raise RuntimeError(
            "Mistral OCR returned no Markdown table rows; no sheet changes were made."
        )
    sheets = SheetsClient(credentials_path)
    first_sheet_id, primary_rows = read_primary_rows(sheets, sheet_id)
    primary_rows = normalize_spaces_between_digits(primary_rows)
    annotations = run_qc_comparison(primary_rows, mistral_rows)
    apply_qc_highlights(sheets, sheet_id, first_sheet_id, primary_rows, annotations)
    write_qc_diff_sheet(sheets, sheet_id, primary_rows, annotations)

    print(
        f"QC end    {loc}  rows={len(primary_rows)} diffs={len(annotations)}",
        flush=True,
    )
    return {
        "status": "success",
        "rows_checked": len(primary_rows),
        "qc_annotations": len(annotations),
    }
