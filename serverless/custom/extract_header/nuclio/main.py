import asyncio
import json
import os
import traceback
from typing import cast
from urllib.parse import quote

import httpx
import mlflow
import mlflow.langchain
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from httpx import HTTPError
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from nuclio_sdk.context import Context
from nuclio_sdk.logger import Logger
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

SERVICE_ACCOUNT_FILE = "/opt/nuclio/creds/cvat-sheets-integration.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

TASK_INSTRUCTION = (
    "Extract every key-value field visible in the header region of this document crop."
)

mlflow.set_experiment("cvat_extract_header")
mlflow.langchain.autolog()


def log_before_retry(retry_state):
    print(f"Attempt {retry_state.attempt_number} failed:")
    traceback.print_exception(retry_state.outcome.exception())


default_retry = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=60, jitter=2),
    retry=retry_if_exception_type(HTTPError),
    before_sleep=log_before_retry,
)


class HeaderExtraction(BaseModel):
    """
    Extracted header fields from a document image.
    Each row is [standardized_label, label, value].
    """

    rows: list[list[str]] = Field(
        description=(
            "List of extracted fields. Each field is a list of exactly 3 strings: "
            "[standardized_label, label, value]."
        )
    )


def get_logger(context: Context) -> Logger:
    return cast(Logger, context.logger)


class ContextVariables:
    def __init__(self, vlm: ChatOpenAI):
        self.vlm = vlm
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
                base_url="https://sheets.googleapis.com/",
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


def init_context(context: Context):
    """
    Runs once when the container starts.
    Initializes the VLM and an authenticated Sheets HTTP client.
    """
    logger = get_logger(context)
    logger.info("extract-header: Initializing VLM and Sheets client...")

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"Credentials file not found at {SERVICE_ACCOUNT_FILE}")

    vlm = ChatOpenAI(
        model="google/gemma-4-26B-A4B-it",
        base_url="https://openrouter.ai/api/v1",
        temperature=0.1,
        extra_body={
            "provider": {
                "require_parameters": True,
                "only": ["google-vertex"],
                "allow_fallbacks": False,
            }
        },
    )
    ContextVariables(vlm).set_cvars(context)
    logger.info("extract-header: initialization complete.")


@default_retry
async def get_sheet_values(
    cvars: ContextVariables,
    logger: Logger,
    spreadsheet_id: str,
    sheet_range: str,
) -> list[list[str]]:
    """
    Reads a values range from the Google Sheets API over HTTPS.
    Retries transient HTTP failures via tenacity.
    """
    client = await cvars.get_client()
    encoded_range = quote(sheet_range, safe="")
    response = await client.get(
        f"/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}",
    )
    response.raise_for_status()
    response_json = response.json()
    logger.info(
        "get_sheet_values spreadsheet_id=%s range=%s -> %s",
        spreadsheet_id,
        sheet_range,
        response_json,
    )
    return response_json.get("values", [])


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
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": values},
    )
    response.raise_for_status()
    logger.info(
        "update_sheet_values spreadsheet_id=%s range=%s status_code=%d",
        spreadsheet_id,
        sheet_range,
        response.status_code,
    )


async def get_standardized_headers(
    cvars: ContextVariables,
    logger: Logger,
    spreadsheet_id: str,
) -> list[str]:
    """
    Reads Column A grounding labels from the spreadsheet.
    Empty cells are filtered out.
    """
    rows = await get_sheet_values(cvars, logger, spreadsheet_id, "A2:A75")
    return [row[0].strip() for row in rows if row and row[0].strip()]


async def run_vlm_extraction(
    cvars: ContextVariables,
    image_b64: str,
    standard_headers: list[str],
) -> list[list[str]]:
    """
    Queries the VLM with structured output for header fields.
    Returns well-formed [standardized_label, label, value] triples only.
    """
    prompt_text = mlflow.genai.load_prompt("prompts:/extract_header@prod").format(
        TASK_INSTRUCTION=TASK_INSTRUCTION,
        STANDARD_HEADERS=json.dumps(standard_headers)
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
    structured_vlm = cvars.vlm.with_structured_output(HeaderExtraction)

    with mlflow.start_span(name="extract_header_vlm_call"):
        result = await structured_vlm.ainvoke([message])

    return [row for row in result.rows if len(row) == 3]


async def map_into_sheet(
    cvars: ContextVariables,
    logger: Logger,
    spreadsheet_id: str,
    extracted_rows: list[list[str]],
    gt_labels: list[str],
) -> int:
    """
    Write matched extracted rows into the sheet starting at A2.
    Clears the target range first, then rewrites compactly with no gaps.
    """
    num_gt = len(gt_labels)
    sheet_range = f"A2:C{num_gt + 10}"
    matched_rows = [
        row for row in extracted_rows if len(row) == 3 and row[0] in gt_labels
    ]

    await clear_sheet_values(cvars, logger, spreadsheet_id, sheet_range)

    if matched_rows:
        await update_sheet_values(cvars, logger, spreadsheet_id, "A2", matched_rows)

    return len(matched_rows)


async def handler(context: Context, event):
    """
    Accepts an internal HTTP request from the sheet-populator orchestrator.
    Expects JSON: {"image_b64": "...", "spreadsheet_id": "..."}.
    Reads headers, runs VLM extraction, populates the sheet, returns rows_updated.
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

    standard_headers = await get_standardized_headers(cvars, logger, spreadsheet_id)
    extracted_rows = await run_vlm_extraction(cvars, image_b64, standard_headers)
    updated_count = await map_into_sheet(
        cvars, logger, spreadsheet_id, extracted_rows, standard_headers
    )

    logger.info(
        "extract-header: updated %d row(s) in %s", updated_count, spreadsheet_id
    )

    return context.Response(
        body=json.dumps({"status": "success", "rows_updated": updated_count}),
        headers={"Content-Type": "application/json"},
        status_code=200,
    )
