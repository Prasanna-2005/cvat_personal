import json
import os
import re
import base64
import io
from PIL import Image
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
    'Extract every key-value field visible in the header region of this document crop.'
)

mlflow.set_experiment("cvat_extract_header")
mlflow.langchain.autolog()


def init_context(context):
    """
    Runs once when the container starts.
    Initializes the VLM chain and Sheets client (for reading headers & writing results).
    """
    context.logger.info("extract-header: Initializing VLM chain and Sheets client...")

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"Credentials file not found at {SERVICE_ACCOUNT_FILE}")

    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    context.sheets = build('sheets', 'v4', credentials=creds)

    context.vlm = ChatOpenAI(
        model="google/gemma-4-31b-it",
        temperature=0.2,
        openai_api_base="https://openrouter.ai/api/v1",
    )

    context.logger.info("extract-header: initialization complete.")


def get_standardized_headers(context, spreadsheet_id):
    """
    Reads Column 1 from the spreadsheet to collect true grounding anchor keys.
    """
    sheet_range = "Sheet1!A2:A75"
    result = context.sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=sheet_range,
    ).execute()
    rows = result.get('values', [])
    # Flatten and filter out empty entries
    return [row[0].strip() for row in rows if row and row[0].strip()]


def run_vlm_extraction(context, image_b64, standard_headers):
    """
    Queries the VLM with strict structural parameters ensuring clean row normalization.
    Returns a list of [standardized_label, label, value] arrays.
    """
    prompt_text = (
    f"Core Objective: {TASK_INSTRUCTION}\n\n"
    "You are a document information extraction and mapping engine. "
    "You are provided with a document image and a list of ground-truth standardized labels.\n\n"

    f"Ground-Truth Standardized Labels:\n{json.dumps(standard_headers)}\n\n"

    "Extraction Rules:\n"
    "1. Identify every visible key-value field within the document image.\n"
    "2. For each extracted field, return exactly three elements in the following order:\n"
    "   [standardized_label, label, value]\n"
    "3. The 'label' is the exact key text visible in the document. Preserve it verbatim.\n"
    "4. Match the extracted 'label' to exactly one entry from the provided 'Ground-Truth Standardized Labels'. "
    "Choose the closest semantic equivalent. The 'standardized_label' must be an exact string from the provided list. "
    "Never invent, modify, or hallucinate a standardized label.\n"
    "5. The 'value' is the text associated with the extracted label. Preserve it exactly as it appears in the document. "
    "Do not paraphrase, normalize, or infer missing content.\n"
    "6. The 'value' must always be returned as a single-line string. "
    "If it spans multiple visual lines, reconstruct it in natural reading order (top-to-bottom, left-to-right). "
    "Remove line breaks. Use spaces when joining lines that form a continuous phrase, sentence, name, or address. "
    "Use commas only when the lines represent logically separate items, list elements, or independent values.\n"
    "7. Each standardized_label may appear at most once in the output. "
    "If multiple extracted fields could map to the same standardized_label, return only the best matching field.\n"
    "8. Ignore decorative text, page headers, footers, logos, watermarks, scratchpad and unrelated content unless they are part of a valid key-value field.\n\n"
    "9. Do not output conversational text, preambles, explanations, or chain-of-thought markdown blocks."

    "Output Format:\n"
    "Return only a raw JSON array of arrays. "
    "Do not wrap the output in markdown or ```json fences. "
    "Do not include explanations, comments, or additional text.\n"
    'Expected format:\n'
    '[["standardized_label", "label", "value"], ...]'
)

    message = HumanMessage(content=[
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
    ])

    with mlflow.start_span(name="extract_header_vlm_call"):
        response = context.vlm.invoke([message])

    raw = response.content.strip()
    # Robust cleanup regex if the VLM leaks code block delimiters regardless of instructions
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


def map_into_sheet(context, spreadsheet_id, extracted_rows):
    """
    Read Column 1, match standardized_label, populate Column 2 (label) & Column 3 (value).
    """
    sheet_range = "Sheet1!A2:C75"
    result = context.sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=sheet_range,
    ).execute()
    gt_rows = result.get('values', [])

    label_lookup_vlm = {row[0]: [row[1], row[2]] for row in extracted_rows if len(row) == 3}

    updates = []
    for idx, gt_row in enumerate(gt_rows):
        if not gt_row:
            continue
        gt_label = gt_row[0]
        if gt_label in label_lookup_vlm:
            label, value = label_lookup_vlm[gt_label]
            sheet_row = idx + 2  # offset for header row + 1-indexing
            updates.append({
                "range": f"Sheet1!B{sheet_row}:C{sheet_row}",
                "values": [[label, value]],
            })

    if updates:
        context.sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()

    return len(updates)


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
        if isinstance(data, bytes):
            data = json.loads(data.decode('utf-8'))

        image_b64 = data.get("image_b64")
        spreadsheet_id = data.get("spreadsheet_id")

        if not image_b64 or not spreadsheet_id:
            return context.Response(
                body=json.dumps({"status": "error", "message": "Missing image_b64 or spreadsheet_id"}),
                headers={"Content-Type": "application/json"},
                status_code=400,
            )

        # Step 1: Read ground-truth headers from the sheet
        standard_headers = get_standardized_headers(context, spreadsheet_id)

        # Step 2: Run VLM extraction
        extracted_rows = run_vlm_extraction(context, image_b64, standard_headers)

        # Step 3: Populate the sheet with extracted data
        updated_count = map_into_sheet(context, spreadsheet_id, extracted_rows)

        context.logger.info(f"extract-header: updated {updated_count} row(s) in {spreadsheet_id}")

        return context.Response(
            body=json.dumps({"status": "success", "rows_updated": updated_count}),
            headers={"Content-Type": "application/json"},
            status_code=200,
        )

    except Exception as e:
        context.logger.error(f"extract-header error: {str(e)}")
        return context.Response(
            body=json.dumps({"status": "error", "message": str(e)}),
            headers={"Content-Type": "application/json"},
            status_code=500,
        )
