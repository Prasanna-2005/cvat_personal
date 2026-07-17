import json
import os
import re
import mlflow.langchain
import mlflow
from google.oauth2 import service_account
from googleapiclient.discovery import build
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

SERVICE_ACCOUNT_FILE = '/opt/nuclio/creds/cvat-sheets-integration.json'
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
]

TASK_INSTRUCTION = (
    'Extract every row in the given table image , for each row, return a array of strings representing the values of each cell in that row. '
)

mlflow.set_experiment("cvat_extract_table")
mlflow.langchain.autolog()


def init_context(context):
    """
    Runs once when the container starts.
    Initializes the VLM chain and Sheets client (for reading headers & writing results).
    """
    context.logger.info("extract-table: Initializing VLM chain and Sheets client...")

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"Credentials file not found at {SERVICE_ACCOUNT_FILE}")

    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    context.sheets = build('sheets', 'v4', credentials=creds)

    context.vlm = ChatOpenAI(
        model="google/gemma-4-31b-it",
        temperature=0.1,
        openai_api_base="https://openrouter.ai/api/v1",
    )

    context.logger.info("extract-table: initialization complete.")


def run_vlm_extraction(context, image_b64):
    """
    Queries the VLM with strict structural parameters ensuring clean row normalization.
    Returns a list of [..., ..., ...] arrays.
    """
    prompt_text = (
        f"Core Objective: {TASK_INSTRUCTION}\n\n"
        "You are a document information extraction engine specialized in parsing table data from document images. "

        "Extraction Rules:\n"
        "1. Identify every row within the document table image.\n"
        "2. For each extracted row, return a list of strings representing the values of each cell in that row.\n"
        "3. Preserve each cell value exactly as it appears in the document. "
        "4. Do not paraphrase, normalize, or infer missing content.\n"
        "5. The order of the cell values in the list must match the visual order of the cells in the row. "
        "If it spans multiple visual lines, reconstruct it in natural reading order (top-to-bottom, left-to-right). "
        "Remove line breaks. Use spaces when joining lines that form a continuous phrase, sentence, name, or address. "
        "Use commas only when the lines represent logically separate items, list elements, or independent values.\n"
        "6. If a cell is empty, return an empty string for that cell.\n"
        "7. Do not attempt to infer or fill in missing values.\n"
        "8. Do not hallucinate or invent any content. "
        "9. Ignore decorative text, page headers, footers, logos, watermarks, scratchpad and unrelated content "
        "unless they are part of a valid key-value field.\n"
        "10. Do not output conversational text, preambles, explanations, or chain-of-thought markdown blocks.\n\n"

        "Output Format:\n"
        "Return only a raw JSON array of arrays. "
        "Do not wrap the output in markdown or ```json fences. "
        "Do not include explanations, comments, or additional text.\n"
        "Expected format:\n"
        '[["cell_value1", "cell_value2", "cell_value3"], ...]'
    )

    message = HumanMessage(content=[
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
    ])

    with mlflow.start_span(name="extract_table_vlm_call"):
        response = context.vlm.invoke([message])

    raw = response.content.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()

    try:
        extracted = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"VLM returned invalid JSON: {e}. Raw response: {raw[:500]}"
        )

    if not isinstance(extracted, list):
        raise ValueError(
            f"VLM returned {type(extracted).__name__} instead of a list. Raw response: {raw[:500]}"
        )

    extracted = [
        row for row in extracted if isinstance(row, list) and all(isinstance(cell, str) for cell in row)
    ]

    return extracted

def map_into_sheet(context, spreadsheet_id, extracted_rows):
    """
    Write extracted_rows to the sheet.
    Clears the target range first, then writes all rows compactly with no gaps.
    """
    # Determine the widest row to build a range covering all columns
    max_cols = max((len(row) for row in extracted_rows), default=0)
    if max_cols == 0:
        return 0

    # Convert column count to letter (A=1 .. Z=26, AA=27, etc.)
    def col_letter(n: int) -> str:
        result = ""
        while n > 0:
            n, remainder = divmod(n - 1, 26)  # n-1 : convert to 0-indexing (remainder : 0 to 25)
            result = chr(65 + remainder) + result
        return result

    end_col = col_letter(max_cols)
    max_row = len(extracted_rows) + 1  # +1 because data starts at row 2
    sheet_range = f"Sheet1!A2:{end_col}{max_row + 5}"  # clear generously 5 is safety margin

    context.sheets.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=sheet_range,
    ).execute()

    context.sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="Sheet1!A2",
        valueInputOption="USER_ENTERED",
        body={"values": extracted_rows},
    ).execute()

    return len(extracted_rows)


def handler(context, event):
    """
    Accepts an internal HTTP request from the sheet-populator Orchestrator.
    Expects JSON: {"image_b64": "...", "spreadsheet_id": "..."}
    1. Reads standardized headers from the spreadsheet
    2. Runs VLM extraction against the image
    3. Populates the sheet with extracted data
    Returns JSON: {"status": "success", "rows_updated": N}
    """
    try:
        data = event.body
        if isinstance(data, (bytes, str)):
            data = json.loads(data if isinstance(data, str) else data.decode('utf-8'))

        image_b64 = data.get("image_b64")
        spreadsheet_id = data.get("spreadsheet_id")

        if not image_b64 or not spreadsheet_id:
            return context.Response(
                body=json.dumps({"status": "error", "message": "Missing image_b64 or spreadsheet_id"}),
                headers={"Content-Type": "application/json"},
                status_code=400,
            )

        # Step 1:: Run VLM extraction
        extracted_rows = run_vlm_extraction(context, image_b64)

        # Step 2: Populate the sheet with extracted data
        updated_count = map_into_sheet(context, spreadsheet_id, extracted_rows)

        context.logger.info(f"extract-table: updated {updated_count} row(s) in {spreadsheet_id}")

        return context.Response(
            body=json.dumps({"status": "success", "rows_updated": updated_count}),
            headers={"Content-Type": "application/json"},
            status_code=200,
        )

    except Exception as e:
        context.logger.error(f"extract-table error: {str(e)}")
        return context.Response(
            body=json.dumps({"status": "error", "message": str(e)}),
            headers={"Content-Type": "application/json"},
            status_code=500,
        )
