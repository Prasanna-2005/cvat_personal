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

TASK_INSTRUCTION = (
    "Extract every row in the given table image , for each row, return a array of "
    "strings representing the values of each cell in that row. "
)

mlflow.set_experiment("cvat_extract_table")
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

    # vlm = ChatOpenAI(
    #     model="google/gemma-4-26B-A4B-it",
    #     base_url="https://openrouter.ai/api/v1",
    #     temperature=0.1,
    #     extra_body={
    #         "provider": {
    #             "require_parameters": True,
    #             "only": ["google-vertex"],
    #             "allow_fallbacks": False,
    #         }
    #     },
    # )

    rate_limiter = InMemoryRateLimiter(
        requests_per_second=5,
        check_every_n_seconds=0.1,
        max_bucket_size=5,
    )

    vlm = ChatOpenAI(
        model="google/gemini-3.1-flash-lite",
        base_url="https://openrouter.ai/api/v1",
        temperature=0.1,
        rate_limiter=rate_limiter
    )
    ContextVariables(vlm).set_cvars(context)
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


async def run_vlm_extraction(
    cvars: ContextVariables,
    image_b64: str,
) -> list[list[str]]:
    """
    Queries the VLM with structured output for table rows.
    Returns a list of cell-value arrays.
    """
    prompt_text = mlflow.genai.load_prompt("prompts:/extract_table@prod").format(
        TASK_INSTRUCTION=TASK_INSTRUCTION
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

    extracted_rows = await run_vlm_extraction(cvars, image_b64)
    updated_count = await map_into_sheet(cvars, logger, spreadsheet_id, extracted_rows)

    logger.info(
        "extract-table: updated %d row(s) in %s", updated_count, spreadsheet_id
    )

    return context.Response(
        body=json.dumps({"status": "success", "rows_updated": updated_count}),
        headers={"Content-Type": "application/json"},
        status_code=200,
    )
