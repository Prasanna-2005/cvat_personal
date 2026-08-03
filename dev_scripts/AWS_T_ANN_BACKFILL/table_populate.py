#!/usr/bin/env python3
"""
For every task in [LOW, HIGH] of a CVAT project:
  - enumerate all jobs, and all frames of each job
  - for each frame already recorded in progress.txt, skip it (resume support)
  - for each remaining frame (all run concurrently, bounded by a global
    Google-API semaphore):
      - fetch the frame's Textract JSON from Google Drive (named
        "<frame_filename>.json")
      - take the (guaranteed) single TABLE block in that JSON
      - look for an existing 'table' polygon object on the frame:
          - if it exists AND already has a non-empty excel_link attribute:
              clear that sheet's contents and rewrite the Textract rows
              into it (no new sheet, no new CVAT object)
          - otherwise:
              create (or reuse) the CVAT polygon object, duplicate the
              template sheet, write the rows, and PATCH excel_link with
              the new sheet's URL

Progress is appended to progress.txt as "projectid:taskid:jobid:frameid"
after each frame finishes. On startup, every line already in progress.txt
is skipped, so a rerun after a crash/Ctrl-C picks up where it left off.

Requires:
    pip install cvat-sdk httpx google-auth tenacity

Auth:
    - CVAT: set CVAT_TOKEN env var (personal access token)
    - Google: a service account JSON key file must sit next to this script;
      set SERVICE_ACCOUNT_FILE below to its filename.

Usage:
    python process_tables.py --project-id 123 --low 45 --high 50
"""

import argparse
import asyncio
import os
import sys
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

from cvat_sdk import make_client
from cvat_sdk.core.client import Client
from cvat_sdk.core.proxies.annotations import AnnotationUpdateAction
from cvat_sdk.api_client import models as cvat_models
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
# =========================== CONFIG ===========================

CVAT_HOST = os.environ["CVAT_URL"]

CDIR = Path(__file__).resolve().parent
# Filename of the service account key, expected next to this script.
SERVICE_ACCOUNT_FILE = CDIR / "cvat-sheets-integration.json"

# Google Sheet to duplicate for every table found.
TEMPLATE_SHEET_ID = os.environ["TEMPLATE_SHEET_ID"]

# Drive folder ID where the Textract JSON files live (named "<frame_filename>.json").
TEXTRACT_JSON_FOLDER_ID = os.environ["TEXTRACT_JSON_FOLDER_ID"]

# Drive folder ID where duplicated sheets should be created.
OUTPUT_SHEETS_FOLDER_ID = os.environ["OUTPUT_SHEETS_FOLDER_ID"]

TABLE_LABEL_NAME = "Table"
EXCEL_LINK_ATTR_NAME = "excel_link"

PROGRESS_FILE = CDIR / "progress.txt"

GOOGLE_CONCURRENCY = 8
FRAME_CONCURRENCY = 16

# ================================================================

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

DRIVE_API = "https://www.googleapis.com/drive/v3"
SHEETS_API = "https://sheets.googleapis.com/v4"


# ----------------------------- Progress file -----------------------------
#
# progress.txt lines look like "projectid:taskid:jobid:frameid". We read it
# once at startup into a set for O(1) skip checks, and append to it as we
# go. Appends happen from many concurrent frame-processing coroutines, so a
# lock guards the write (open/write/close isn't atomic once multiple
# coroutines are mid-flight, which is now the normal case).

progress_lock = asyncio.Lock()


def load_progress() -> Set[Tuple[int, int, int, int]]:
    if not os.path.exists(PROGRESS_FILE):
        return set()
    done = set()
    with open(PROGRESS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                p, t, j, fr = line.split(":")
                done.add((int(p), int(t), int(j), int(fr)))
            except ValueError:
                continue
    return done


async def record_progress(
    project_id: int, task_id: int, job_id: int, frame_num: int
) -> None:
    async with progress_lock:
        with open(PROGRESS_FILE, "a") as f:
            f.write(f"{project_id}:{task_id}:{job_id}:{frame_num}\n")


# ----------------------------- Google REST client -----------------------------
#
# We call the Drive v3 / Sheets v4 REST endpoints directly. Calling the REST
# paths directly skips that schema fetch and round-trips faster.
#
# GoogleSession lazily creates an httpx.AsyncClient on first use and keeps it
# alive until a connection-level error (server-side close, socket death) or a
# token expiry forces a replacement.  When that happens, the old client object
# is simply abandoned — GC reaps it and its sockets internally.  No explicit
# close is ever called, so we don't need context-manager plumbing.
#
# Before every request, _ensure_client() verifies the credential is still
# valid; if it expired, it refreshes the token and — since the old bearer
# header is now stale — creates a fresh client.  On transport-level errors
# (ConnectError, RemoteProtocolError, ReadError), the client is replaced and
# the request is retried.  HTTP 429 / 503 are also retried with backoff
# (honoring Retry-After when present).
#
# The GOOGLE_CONCURRENCY semaphore still caps in-flight requests across all
# coroutines.


class RetryableGoogleError(Exception):
    """Raised for transient Google API failures that should be retried."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


def _should_retry_google(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            RetryableGoogleError,
        ),
    )


def _wait_google(retry_state) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, RetryableGoogleError) and exc.retry_after is not None:
        return max(exc.retry_after, 0.0)
    return wait_exponential_jitter(initial=1, max=60, jitter=5)(retry_state)


google_retry = retry(
    retry=retry_if_exception(_should_retry_google),
    wait=_wait_google,
    stop=stop_after_attempt(5),
    reraise=True,
)


class GoogleSession:
    def __init__(self):
        self._creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=GOOGLE_SCOPES
        )
        self._creds_lock = asyncio.Lock()
        self._http: Optional[httpx.AsyncClient] = None
        self.semaphore = asyncio.Semaphore(GOOGLE_CONCURRENCY)

    async def _refresh_token(self) -> None:
        """Refresh the Google credential token (thread-safe, blocks only the caller)."""
        async with self._creds_lock:
            if not self._creds.valid:
                await asyncio.to_thread(self._creds.refresh, GoogleAuthRequest())

    def _new_client(self) -> httpx.AsyncClient:
        """Spin up a fresh httpx client. The old one (if any) is simply
        abandoned — GC will collect it and its sockets in due course."""
        return httpx.AsyncClient(timeout=60.0)

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return a usable httpx client, creating a fresh one if the current
        client is missing or its underlying transport has been closed."""
        await self._refresh_token()
        if self._http is None or self._http.is_closed:
            self._http = self._new_client()
        return self._http

    @google_retry
    async def _request_with_retry(
        self, method: str, url: str, extra_headers: Dict[str, str], **kwargs
    ) -> httpx.Response:
        """Execute a single HTTP request, invalidating the client on
        transport-level errors so the next tenacity retry gets a fresh one."""
        client = await self._ensure_client()
        headers = {"Authorization": f"Bearer {self._creds.token}"}
        headers.update(extra_headers)
        try:
            resp = await client.request(method, url, headers=headers, **kwargs)
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
            # Mark client as stale so _ensure_client() builds a new one on
            # the next retry attempt.  The old object is left for GC.
            self._http = None
            raise
        if resp.status_code in (429, 503):
            retry_after = None
            raw = resp.headers.get("Retry-After")
            if raw:
                try:
                    retry_after = float(raw)
                except ValueError:
                    retry_after = None
            print(
                f"  HTTP {resp.status_code} {method} {url} — retrying"
                + (f" after {retry_after}s" if retry_after is not None else ""),
                flush=True,
            )
            raise RetryableGoogleError(
                f"Google API {resp.status_code} for {method} {url}",
                retry_after=retry_after,
            )
        if resp.is_error:
            print(f"  HTTP {resp.status_code} {method} {url}", flush=True)
            print(f"  Response body: {resp.text}", flush=True)
        resp.raise_for_status()
        return resp

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        extra_headers = kwargs.pop("headers", {}) or {}
        async with self.semaphore:
            return await self._request_with_retry(method, url, extra_headers, **kwargs)


async def find_drive_files_by_name(
    gs: GoogleSession, folder_id: Optional[str], filename: str
) -> List[str]:
    """Return all file ids named `filename` inside `folder_id` (Drive allows duplicate names)."""
    safe_name = filename.replace("'", "\\'")
    if folder_id:
        query = f"'{folder_id}' in parents and name = '{safe_name}' and trashed = false"
    else:
        query = f"name = '{safe_name}' and trashed = false"

    params: Dict[str, Any] = {
        "q": query,
        "fields": "nextPageToken, files(id, name, parents)",
        "pageSize": 100,
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }

    ids: List[str] = []
    page_token: Optional[str] = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        else:
            params.pop("pageToken", None)
        resp = await gs.request("GET", f"{DRIVE_API}/files", params=params)
        body = resp.json()
        for file in body.get("files", []):
            if folder_id is None or folder_id in (file.get("parents") or []):
                ids.append(file["id"])
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    return ids


async def find_drive_file_by_name(
    gs: GoogleSession, folder_id: Optional[str], filename: str
) -> Optional[str]:
    """Return the file id of the first file named `filename` inside `folder_id`, or None."""
    ids = await find_drive_files_by_name(gs, folder_id, filename)
    return ids[0] if ids else None


async def trash_drive_file(gs: GoogleSession, file_id: str) -> None:
    """Move a Drive file to trash by id. Permanent DELETE needs organizer on
    shared drives and often 404s for this service account; trash works."""
    await gs.request(
        "PATCH",
        f"{DRIVE_API}/files/{file_id}",
        params={"supportsAllDrives": "true"},
        json={"trashed": True},
    )


async def get_or_create_task_folder(
    gs: GoogleSession,
    folder_named_as_taskid: str,
    folder_locks: Dict[str, asyncio.Lock],
) -> str:
    """
    Return the id of <OUTPUT_SHEETS_FOLDER_ID>/<folder_named_as_taskid>, creating it if
    needed. Guarded by a per-task-id lock so concurrent frames of the same
    task don't race and create two folders with the same name.
    """
    lock = folder_locks.setdefault(folder_named_as_taskid, asyncio.Lock())
    async with lock:
        existing_id = await find_drive_file_by_name(
            gs, OUTPUT_SHEETS_FOLDER_ID, folder_named_as_taskid
        )
        if existing_id:
            return existing_id

        body = {
            "name": folder_named_as_taskid,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [OUTPUT_SHEETS_FOLDER_ID],
        }
        resp = await gs.request(
            "POST",
            f"{DRIVE_API}/files",
            params={"fields": "id", "supportsAllDrives": "true"},
            json=body,
        )
        return resp.json()["id"]


async def download_drive_json(gs: GoogleSession, file_id: str) -> Dict[str, Any]:
    resp = await gs.request(
        "GET",
        f"{DRIVE_API}/files/{file_id}",
        params={"alt": "media", "supportsAllDrives": "true"},
    )
    return resp.json()


async def duplicate_template_sheet(
    gs: GoogleSession, task_folder_id: str, new_name: str
) -> Tuple[str, str]:
    """
    Copy TEMPLATE_SHEET_ID into task_folder_id with the given name.
    Drive allows multiple files with the same name in one folder, so trash
    *every* existing match first (by file_id), then copy.
    Returns (new_file_id, webViewLink).
    """
    existing_ids = await find_drive_files_by_name(gs, task_folder_id, new_name)
    for existing_id in existing_ids:
        await trash_drive_file(gs, existing_id)
    if existing_ids:
        print(f"  replaced {len(existing_ids)} existing sheet(s) named '{new_name}'")

    body = {"name": new_name, "parents": [task_folder_id]}
    resp = await gs.request(
        "POST",
        f"{DRIVE_API}/files/{TEMPLATE_SHEET_ID}/copy",
        params={"fields": "id, webViewLink", "supportsAllDrives": "true"},
        json=body,
    )
    new_file = resp.json()
    return new_file["id"], new_file.get(
        "webViewLink"
    ) or f"https://docs.google.com/spreadsheets/d/{new_file['id']}"


def extract_sheet_id_from_link(link: str) -> Optional[str]:
    """Pull the spreadsheet id out of a Drive/Sheets URL like
    https://docs.google.com/spreadsheets/d/<id>/edit ."""
    marker = "/d/"
    idx = link.find(marker)
    if idx == -1:
        return None
    rest = link[idx + len(marker) :]
    return rest.split("/")[0].split("?")[0] or None


async def clear_sheet(gs: GoogleSession, spreadsheet_id: str, sheet_title: str) -> None:
    """Clear all values from the first sheet's data range before rewriting it."""
    range_name = f"'{sheet_title}'!A1:ZZ"
    encoded_range = urllib.parse.quote(range_name, safe="")
    await gs.request(
        "POST",
        f"{SHEETS_API}/spreadsheets/{spreadsheet_id}/values/{encoded_range}:clear",
    )


async def get_first_sheet_title(gs: GoogleSession, spreadsheet_id: str) -> str:
    meta_resp = await gs.request(
        "GET",
        f"{SHEETS_API}/spreadsheets/{spreadsheet_id}",
        params={"fields": "sheets.properties.title"},
    )
    return meta_resp.json()["sheets"][0]["properties"]["title"]


async def write_rows_to_sheet(
    gs: GoogleSession, spreadsheet_id: str, rows: List[List[str]]
) -> None:
    """Write rows starting at A2 of the first sheet."""
    if not rows:
        return
    first_sheet_title = await get_first_sheet_title(gs, spreadsheet_id)
    range_name = f"'{first_sheet_title}'!A2"
    encoded_range = urllib.parse.quote(range_name, safe="")

    await gs.request(
        "PUT",
        f"{SHEETS_API}/spreadsheets/{spreadsheet_id}/values/{encoded_range}",
        params={"valueInputOption": "RAW"},
        json={"values": rows},
    )


# ----------------------------- Textract parsing -----------------------------


def _block_map(blocks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {b["Id"]: b for b in blocks}


def _children_of(block: Dict[str, Any], rel_type: str) -> List[str]:
    for rel in block.get("Relationships", []) or []:
        if rel.get("Type") == rel_type:
            return rel.get("Ids", [])
    return []


def _cell_text(cell_block: Dict[str, Any], block_map: Dict[str, Dict[str, Any]]) -> str:
    """Join WORD children text with spaces; represent a checked SELECTION_ELEMENT as 'X'."""
    words = []
    for child_id in _children_of(cell_block, "CHILD"):
        child = block_map.get(child_id)
        if not child:
            continue
        if child["BlockType"] == "WORD":
            words.append(child.get("Text", ""))
        elif child["BlockType"] == "SELECTION_ELEMENT":
            if child.get("SelectionStatus") == "SELECTED":
                words.append("-")
    return " ".join(w for w in words if w).strip()


def extract_table_rows(
    table_block: Dict[str, Any], block_map: Dict[str, Dict[str, Any]]
) -> List[List[str]]:
    """
    Build a list[list[str]] grid from a Textract TABLE block, preserving
    column order. Handles MERGED_CELL by expanding the merged value into
    every row/col position it spans.
    """
    cell_ids = _children_of(table_block, "CHILD")
    cells = [block_map[cid] for cid in cell_ids if cid in block_map]

    max_row = 0
    max_col = 0
    grid: Dict[Tuple[int, int], str] = {}

    for cell in cells:
        block_type = cell.get("BlockType")
        row_idx = cell.get("RowIndex")
        col_idx = cell.get("ColumnIndex")
        if row_idx is None or col_idx is None:
            continue

        if block_type == "MERGED_CELL":
            row_span = cell.get("RowSpan", 1)
            col_span = cell.get("ColumnSpan", 1)
            merged_text_parts = []
            for sub_id in _children_of(cell, "CHILD"):
                sub_cell = block_map.get(sub_id)
                if sub_cell and sub_cell.get("BlockType") == "CELL":
                    text = _cell_text(sub_cell, block_map)
                    if text:
                        merged_text_parts.append(text)
            merged_text = " ".join(merged_text_parts).strip()
            for r in range(row_idx, row_idx + row_span):
                for c in range(col_idx, col_idx + col_span):
                    grid[(r, c)] = merged_text
                    max_row = max(max_row, r)
                    max_col = max(max_col, c)
        elif block_type == "CELL":
            row_span = cell.get("RowSpan", 1)
            col_span = cell.get("ColumnSpan", 1)
            text = _cell_text(cell, block_map)
            for r in range(row_idx, row_idx + row_span):
                for c in range(col_idx, col_idx + col_span):
                    grid.setdefault((r, c), text)
                    max_row = max(max_row, r)
                    max_col = max(max_col, c)

    rows: List[List[str]] = []
    for r in range(1, max_row + 1):
        row_values = [grid.get((r, c), "") for c in range(1, max_col + 1)]
        rows.append(row_values)
    return rows


def get_table_block(textract_json: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Return the single TABLE block from the Textract response. Exactly one
    transaction table per page is guaranteed by the upstream pipeline, so we
    take the first TABLE block found and don't loop over multiple.
    """
    for b in textract_json.get("Blocks", []):
        if b.get("BlockType") == "TABLE":
            return b
    return None


def polygon_points_from_table_block(
    table_block: Dict[str, Any], frame_width: int, frame_height: int
) -> List[float]:
    """Flat [x1, y1, x2, y2, ...] pixel-coordinate point list from Geometry.Polygon (ratio points)."""
    polygon = table_block["Geometry"]["Polygon"]
    points: List[float] = []
    for pt in polygon:
        points.append(pt["X"] * frame_width)
        points.append(pt["Y"] * frame_height)
    return points


# ----------------------------- CVAT helpers -----------------------------
#
# The CVAT SDK is synchronous. Each call below is wrapped in
# asyncio.to_thread() at the call site (in process_frame / process_task) so
# it runs on a worker thread and doesn't block the event loop while many
# frames' Google I/O is in flight concurrently.


def get_table_label_and_attr(client: Client, project_id: int) -> Tuple[int, int]:
    """
    Look up only the 'table' label and its 'excel_link' attribute spec id
    (no need to map every label/attribute in the project).
    Returns (table_label_id, excel_link_spec_id).
    """
    project = client.projects.retrieve(project_id)
    labels = project.get_labels()

    for label in labels:
        if label.name != TABLE_LABEL_NAME:
            continue
        for attr in label.attributes or []:
            if attr.name == EXCEL_LINK_ATTR_NAME:
                return label.id, attr.id
        print(
            f"ERROR: label '{TABLE_LABEL_NAME}' has no '{EXCEL_LINK_ATTR_NAME}' attribute.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"ERROR: project has no '{TABLE_LABEL_NAME}' label.", file=sys.stderr)
    sys.exit(1)


def get_project_org_slug(client: Client, project_id: int) -> Optional[str]:
    """
    Resolve the organization slug that this project belongs to, if any.
    ProjectRead only exposes a numeric organization_id, so we look up the
    Organization by that id to get its slug. Returns None if the project
    isn't in an organization (personal workspace).
    """
    project = client.projects.retrieve(project_id)
    org_id = getattr(project, "organization", None) or getattr(
        project, "organization_id", None
    )
    if not org_id:
        return None
    org = client.organizations.retrieve(org_id)
    return org.slug


def get_job_frame_ids(job) -> List[int]:
    meta = job.get_meta()
    if meta.included_frames:
        return list(meta.included_frames)
    return list(range(meta.start_frame, meta.stop_frame + 1))


def find_existing_table_shape(job, frame_num: int, table_label_id: int):
    """Return the existing 'table' shape on this frame, or None."""
    annotations = job.get_annotations()
    candidates = [
        s
        for s in annotations.shapes
        if s.frame == frame_num and s.label_id == table_label_id
    ]
    if not candidates:
        return None
    # Deterministic pipeline guarantees at most one; if more than one is
    # somehow present, prefer the most recently created.
    return max(candidates, key=lambda s: s.id)


def get_shape_excel_link(shape, excel_link_spec_id: int) -> str:
    for attr in shape.attributes or []:
        if attr.spec_id == excel_link_spec_id:
            return attr.value or ""
    return ""


def create_table_polygon(
    job,
    frame_num: int,
    table_label_id: int,
    excel_link_spec_id: int,
    points: List[float],
) -> int:
    """Create a polygon 'table' shape (with an empty excel_link attribute) and return its new object id."""
    create_request = cvat_models.PatchedLabeledDataRequest(
        shapes=[
            cvat_models.LabeledShapeRequest(
                type="polygon",
                label_id=table_label_id,
                frame=frame_num,
                points=points,
                attributes=[
                    cvat_models.AttributeValRequest(
                        spec_id=excel_link_spec_id, value=""
                    )
                ],
            )
        ],
        tags=[],
        tracks=[],
    )
    job.update_annotations(create_request, action=AnnotationUpdateAction.CREATE)

    annotations = job.get_annotations()
    candidates = [
        s
        for s in annotations.shapes
        if s.frame == frame_num and s.label_id == table_label_id
    ]
    if not candidates:
        raise RuntimeError(
            f"could not find newly created table object on frame {frame_num}"
        )
    new_shape = max(candidates, key=lambda s: s.id)
    return new_shape.id


def update_excel_link_attribute(
    job,
    frame_num: int,
    object_id: int,
    table_label_id: int,
    excel_link_spec_id: int,
    excel_link_value: str,
    points: List[float],
) -> None:
    """Patch the excel_link attribute of an existing object (separate call, as required)."""
    update_request = cvat_models.PatchedLabeledDataRequest(
        shapes=[
            cvat_models.LabeledShapeRequest(
                type="polygon",
                label_id=table_label_id,
                frame=frame_num,
                id=object_id,
                points=points,
                attributes=[
                    cvat_models.AttributeValRequest(
                        spec_id=excel_link_spec_id, value=excel_link_value
                    )
                ],
            )
        ],
        tags=[],
        tracks=[],
    )
    job.update_annotations(update_request, action=AnnotationUpdateAction.UPDATE)


def build_frame_info_map(job) -> Dict[int, Any]:
    """
    job.get_frames_info() returns FrameMeta entries positionally aligned with
    the job's frame id sequence, so zip it against get_job_frame_ids() to get
    an absolute-frame-number -> FrameMeta map.
    """
    frame_ids = get_job_frame_ids(job)
    frames_info = job.get_frames_info()
    if len(frame_ids) != len(frames_info):
        raise RuntimeError(
            f"Job {job.id}: frame id count ({len(frame_ids)}) doesn't match "
            f"frames_info count ({len(frames_info)})"
        )
    return dict(zip(frame_ids, frames_info))


def get_frame_filename(frame_info_map: Dict[int, Any], frame_num: int) -> str:
    info = frame_info_map.get(frame_num)
    if info is None:
        raise RuntimeError(f"Could not resolve filename for frame {frame_num}")
    return info.name


def get_frame_dimensions(
    frame_info_map: Dict[int, Any], frame_num: int
) -> Tuple[int, int]:
    info = frame_info_map.get(frame_num)
    if info is None:
        raise RuntimeError(f"Could not resolve dimensions for frame {frame_num}")
    return info.width, info.height


# ----------------------------- Main pipeline -----------------------------


async def process_frame(
    gs: GoogleSession,
    project_id: int,
    project_name: str,
    task_id: int,
    task_folder_id: str,
    textract_task_folder_id: Optional[str],
    job,
    frame_num: int,
    frame_info_map: Dict[int, Any],
    table_label_id: int,
    excel_link_spec_id: int,
) -> bool:
    frame_filename = get_frame_filename(frame_info_map, frame_num)
    frame_width, frame_height = get_frame_dimensions(frame_info_map, frame_num)
    stem, _, _ = frame_filename.rpartition(".")
    json_filename = f"{stem}.json" if stem else f"{frame_filename}.json"

    json_file_id = await find_drive_file_by_name(
        gs, textract_task_folder_id, json_filename
    )
    if not json_file_id:
        print(f"  Textract JSON not found: {json_filename}, skipping frame")
        return False

    textract_json = await download_drive_json(gs, json_file_id)
    block_map = _block_map(textract_json.get("Blocks", []))
    table_block = get_table_block(textract_json)

    if not table_block:
        print(f"  no TABLE block in {json_filename}")
        return False

    points = polygon_points_from_table_block(table_block, frame_width, frame_height)
    rows = extract_table_rows(table_block, block_map)

    existing_shape = await asyncio.to_thread(
        find_existing_table_shape, job, frame_num, table_label_id
    )
    existing_link = (
        get_shape_excel_link(existing_shape, excel_link_spec_id)
        if existing_shape
        else ""
    )

    if existing_shape and existing_link:
        # Table object already exists and already points at a sheet:
        # reuse it in place - just clear and rewrite that sheet's contents.
        sheet_id = extract_sheet_id_from_link(existing_link)
        if not sheet_id:
            raise RuntimeError(
                f"could not parse spreadsheet id from existing link: {existing_link}"
            )

        sheet_title = await get_first_sheet_title(gs, sheet_id)
        await clear_sheet(gs, sheet_id, sheet_title)
        await write_rows_to_sheet(gs, sheet_id, rows)
        print(
            f"  object {existing_shape.id}: reused existing sheet, rewritten -> {existing_link}"
        )
        return True
    else:
        # No table object yet, or one exists without a linked sheet:
        # create the object if needed, duplicate the template, write rows,
        # then attach the new link.
        if existing_shape:
            object_id = existing_shape.id
        else:
            object_id = await asyncio.to_thread(
                create_table_polygon,
                job,
                frame_num,
                table_label_id,
                excel_link_spec_id,
                points,
            )

        sheet_name = (
            f"{project_name}_{task_id}_{job.id}_table{object_id}_frame{frame_num}"
        )
        new_sheet_id, sheet_link = await duplicate_template_sheet(
            gs, task_folder_id, sheet_name
        )
        await write_rows_to_sheet(gs, new_sheet_id, rows)

        await asyncio.to_thread(
            update_excel_link_attribute,
            job,
            frame_num,
            object_id,
            table_label_id,
            excel_link_spec_id,
            sheet_link,
            points,
        )
        print(f"  object {object_id}: new sheet -> {sheet_link}")
        return True


async def process_task(
    gs: GoogleSession,
    client: Client,
    project_id: int,
    project_name: str,
    task_id: int,
    table_label_id: int,
    excel_link_spec_id: int,
    done: Set[Tuple[int, int, int, int]],
    folder_locks: Dict[str, asyncio.Lock],
    frame_semaphore: asyncio.Semaphore,
) -> None:
    print(f"task {task_id}")
    try:
        task = await asyncio.to_thread(client.tasks.retrieve, task_id)
    except Exception as exc:
        print(f"  could not retrieve task: {exc}")
        return

    task_folder_id = await get_or_create_task_folder(gs, str(task.id), folder_locks)

    # Resolve the task's subfolder inside the Textract JSON root folder.
    # JSONs live at TEXTRACT_JSON_FOLDER_ID/<task_name>/<file>.json
    textract_task_folder_id = await find_drive_file_by_name(
        gs, TEXTRACT_JSON_FOLDER_ID, task.name
    )
    if not textract_task_folder_id:
        print(f"  Textract subfolder '{task.name}' not found in Drive, skipping task")
        return

    jobs = await asyncio.to_thread(task.get_jobs)

    async def run_frame(job, frame_num: int, frame_info_map: Dict[int, Any]) -> None:
        key = (project_id, task_id, job.id, frame_num)
        if key in done:
            print(f" job {job.id} frame {frame_num} - already done, skipping")
            return

        async with frame_semaphore:
            print(f" job {job.id} frame {frame_num}")
            try:
                success = await process_frame(
                    gs,
                    project_id,
                    project_name,
                    task_id,
                    task_folder_id,
                    textract_task_folder_id,
                    job,
                    frame_num,
                    frame_info_map,
                    table_label_id,
                    excel_link_spec_id,
                )
                if success:
                    await record_progress(project_id, task_id, job.id, frame_num)
                    done.add(key)
            except Exception as exc:
                print(f"  FAILED job {job.id} frame {frame_num}: {exc}")

    frame_tasks = []
    for job in jobs:
        frame_ids = get_job_frame_ids(job)
        frame_info_map = await asyncio.to_thread(build_frame_info_map, job)
        for frame_num in frame_ids:
            frame_tasks.append(
                asyncio.create_task(run_frame(job, frame_num, frame_info_map))
            )

    if frame_tasks:
        await asyncio.gather(*frame_tasks)


async def async_main(args) -> None:
    done = load_progress()
    if done:
        print(f"resuming: {len(done)} frame(s) already recorded in {PROGRESS_FILE}")

    with make_client(host=args.host, access_token=os.environ["CVAT_TOKEN"]) as client:
        org_slug = get_project_org_slug(client, args.project_id)
        if org_slug:
            client.organization_slug = org_slug
            print(f"using organization context: {org_slug}")

        project = client.projects.retrieve(args.project_id)
        project_name = project.name
        table_label_id, excel_link_spec_id = get_table_label_and_attr(
            client, args.project_id
        )

        folder_locks: Dict[str, asyncio.Lock] = {}
        frame_semaphore = asyncio.Semaphore(FRAME_CONCURRENCY)

        gs = GoogleSession()

        task_coros = [
            process_task(
                gs,
                client,
                args.project_id,
                project_name,
                task_id,
                table_label_id,
                excel_link_spec_id,
                done,
                folder_locks,
                frame_semaphore,
            )
            for task_id in range(args.low, args.high + 1)
        ]
        await asyncio.gather(*task_coros)

    print("done")


def main():
    parser = argparse.ArgumentParser(
        description="Process CVAT tasks: create/update table polygons and populate Google Sheets from Textract JSON."
    )
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument(
        "--low", type=int, required=True, help="Lower bound task id (inclusive)"
    )
    parser.add_argument(
        "--high", type=int, required=True, help="Upper bound task id (inclusive)"
    )
    parser.add_argument("--host", default=CVAT_HOST)
    args = parser.parse_args()

    if not os.environ.get("CVAT_TOKEN"):
        print("ERROR: set CVAT_TOKEN environment variable.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print(
            f"ERROR: service account file '{SERVICE_ACCOUNT_FILE}' not found next to this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
