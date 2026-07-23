import asyncio
import base64
import io
import json
import os
import re
import traceback
from typing import TypedDict, cast

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from httpx import HTTPError
from nuclio_sdk.context import Context
from nuclio_sdk.logger import Logger
from PIL import Image
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

SERVICE_ACCOUNT_FILE = "/opt/nuclio/creds/cvat-sheets-integration.json"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
]


def log_before_retry(retry_state):
    print(f"Attempt {retry_state.attempt_number} failed:")
    traceback.print_exception(retry_state.outcome.exception())


default_retry = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=60 , jitter=2 ),
    retry=retry_if_exception_type(HTTPError),
    before_sleep=log_before_retry,
)

# Internal Nuclio-to-Nuclio routing map.
# Keys are the ai_task strings sent by the CVAT UI.
# Values are in-cluster Service DNS names (MicroK8s / namespace cvat).

if os.getenv("KUBERNETES_SERVICE_HOST"):
    TASK_ROUTE_MAP = {
        "Extract Header": "http://nuclio-extract-header:8080",
        "Extract Table": "http://nuclio-extract-table:8080",
    }
else:

    TASK_ROUTE_MAP = {
        "Extract Header": "http://nuclio-nuclio-extract-header:8080",
        "Extract Table": "http://nuclio-nuclio-extract-table:8080",
    }


def extract_google_id(url: str) -> str:
    """
    Extracts the file/folder ID from a standard Google URL.
    """
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"folders/([a-zA-Z0-9-_]+)", url)
    if m:
        return m.group(1)
    return url


def get_logger(context: Context) -> Logger:
    return cast(Logger, context.logger)


class ContextVariables:
    def __init__(self):
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
            return httpx.AsyncClient(
                base_url="https://www.googleapis.com/",
                headers={"Authorization": f"Bearer {self._creds.token}"},
                http2=True,
                timeout=30,
            )

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

    def get_creds(self):
        if self._creds.expired:
            self._creds.refresh(Request())


def init_context(context: Context):
    """
    Runs once when the container starts.
    Initializes Drive/Sheets clients only — VLM and sheet population
    are handled by downstream functions.
    """
    logger = get_logger(context)
    logger.info("sheet-populator (orchestrator): Initializing Google services...")

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"Credentials file not found at {SERVICE_ACCOUNT_FILE}")

    cvars = ContextVariables()
    cvars.set_cvars(context)

    logger.info("sheet-populator (orchestrator): initialization complete.")


@default_retry
async def list_drive_files(
    cvars: ContextVariables,
    logger: Logger,
    query: str,
) -> dict:
    client = await cvars.get_client()
    response = await client.get(
        "/drive/v3/files",
        params={
            "q": query,
            "spaces": "drive",
            "fields": "files(id,name)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        },
    )
    response_json = response.json()
    files = response_json["files"]
    logger.debug("Listing files in '%s' -> '%s'", query, response_json)
    response.raise_for_status()
    return files


async def trash_drive_file(
    cvars: ContextVariables, logger: Logger, file_id: str
) -> None:
    client = await cvars.get_client()
    response = await client.patch(
        f"/drive/v3/files/{file_id}",
        params={"supportsAllDrives": "true"},
        json={"trashed": True},
    )
    response_json = response.json()
    logger.info(
        "Deleted file_id=%s response=%s status_code=%d",
        file_id,
        response_json,
        response.status_code,
    )


@default_retry
async def copy_drive_file(
    cvars: ContextVariables,
    logger: Logger,
    file_id: str,
    new_file_name: str,
    parents: list[str],
) -> tuple[str, str]:
    client = await cvars.get_client()
    response = await client.post(
        f"/drive/v3/files/{file_id}/copy",
        params={
            "fields": "id,webViewLink",
            "supportsAllDrives": "true",
        },
        json={"name": new_file_name, "parents": parents},
    )
    response_json = response.json()
    logger.info("Copying file_id=%s response=%s", file_id, response_json)
    response.raise_for_status()
    return response_json["id"], response_json["webViewLink"]


async def duplicate_sheet(
    cvars: ContextVariables,
    logger: Logger,
    template_url: str,
    folder_url: str,
    new_file_name: str,
):
    template_id = extract_google_id(template_url)
    folder_id = extract_google_id(folder_url)

    logger.info("Duplicating sheet '%s' into folder: '%s'", template_id, folder_id)

    escaped_name = new_file_name.replace("'", "\\'")
    query = f"name = '{escaped_name}' and '{folder_id}' in parents and trashed = false"
    existing_files = await list_drive_files(cvars, logger, query)
    logger.info(
        "Found %d existing files in folder_id=%s", len(existing_files), folder_id
    )

    await asyncio.gather(
        *[trash_drive_file(cvars, logger, file_obj["id"]) for file_obj in existing_files],
        return_exceptions=True,
    )

    logger.info(
        f"sheet-populator: Batch trashed {len(existing_files)} existing file(s)"
    )

    return await copy_drive_file(cvars, logger, template_id, new_file_name, [folder_id])


async def invoke_downstream(
    logger: Logger,
    downstream_url: str,
    image_b64: str,
    spreadsheet_id: str,
):
    logger.info(
        "sheet-populator: invoking downstream at %s for %s",
        downstream_url,
        spreadsheet_id,
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            downstream_url,
            json={"image_b64": image_b64, "spreadsheet_id": spreadsheet_id},
            headers={"Content-Type": "application/json"},
            timeout=300,  # Stay under the orchestrator's 300s eventTimeout
        )
        result = resp.read()
        logger.info("Downstream url returned %s, status_code=%d", result, resp.status_code)
        resp.raise_for_status()
        return resp.json()


class XDataFunctionPayload(TypedDict):
    template_url: str
    folder_url: str
    new_file_name: str
    ai_task: str | None


async def handler(context: Context, event):
    """
    Orchestrator: Triggered every time CVAT sends an interaction request.
    1. Duplicates the Google Sheet template
    2. Delegates VLM extraction + sheet population to a downstream Nuclio function
    3. Returns the sheet URL to CVAT UI
    """
    cvars = ContextVariables.get_cvars(context)
    logger = get_logger(context)

    data = event.body
    if isinstance(data, bytes):
        data = json.loads(data.decode("utf-8"))

    image_b64 = data.get("image")

    payload: XDataFunctionPayload = data.get("x-data", {})
    # context.logger.info(f"payload: {payload} && image_b64: {image_b64[:100]}")
    template_url = payload["template_url"]
    folder_url = payload["folder_url"]
    new_file_name = payload["new_file_name"]
    ai_task = payload.get("ai_task", None)

    if not image_b64:
        raise ValueError("Missing base64 image in payload.")

    # Sheet - Duplication logic when user presses create button in CVAT-UI
    if not ai_task:
        spreadsheet_id, sheet_url = await duplicate_sheet(
        cvars, logger, template_url, folder_url, new_file_name
    )
        logger.info("Custom template Duplicated: %s", sheet_url)
        return context.Response(
            body=json.dumps(
                {
                    "status": "success",
                    "url": sheet_url
                }
            ),
            headers={"Content-Type": "application/json"},
            status_code=200,
        )

    img_bytes = base64.b64decode(image_b64)
    img_io = io.BytesIO(img_bytes)
    image = Image.open(img_io)
    img_width, img_height = image.size

    obj_bbox = payload.get("obj_bbox", [])
    if len(obj_bbox) >= 2:
        x1, y1 = obj_bbox[0]
        x2, y2 = obj_bbox[1]
    else:
        x1, y1 = 0, 0
        x2, y2 = img_width, img_height

    left = max(0, int(min(x1, x2)))
    top = max(0, int(min(y1, y2)))
    right = min(img_width, int(max(x1, x2)))
    bottom = min(img_height, int(max(y1, y2)))

    cropped_image = image.crop((left, top, right, bottom))

    img_byte_arr = io.BytesIO()
    cropped_image.save(img_byte_arr, format="PNG")
    img_byte_arr.seek(0)  # Reset pointer to the start of the stream

    cropped_image_b64 = base64.b64encode(img_byte_arr.getvalue()).decode("utf-8")

    downstream_url = TASK_ROUTE_MAP[ai_task]

    spreadsheet_id, sheet_url = await duplicate_sheet(
        cvars, logger, template_url, folder_url, new_file_name
    )
    logger.info("Duplicated sheet '%s' with url '%s'", spreadsheet_id, sheet_url)

    result = await invoke_downstream(
        logger, downstream_url, cropped_image_b64, spreadsheet_id
    )

    return context.Response(
        body=json.dumps(
            {
                "status": "success",
                "url": sheet_url,
                "rows_updated": result.get("rows_updated", 0),
            }
        ),
        headers={"Content-Type": "application/json"},
        status_code=200,
    )
