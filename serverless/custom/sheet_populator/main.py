import json
import os
import re
import base64
import io
import requests
from PIL import Image
from google.oauth2 import service_account
from googleapiclient.discovery import build

SERVICE_ACCOUNT_FILE = '/opt/nuclio/creds/cvat-sheets-integration.json'
SCOPES = [
    'https://www.googleapis.com/auth/drive',
]

# Internal Nuclio-to-Nuclio routing map.
# Keys are the ai_task strings sent by the CVAT UI.
# Values are the internal Docker DNS URLs of downstream VLM functions
# on the shared cvat_cvat network (container name: nuclio-nuclio-<function-name>).
TASK_ROUTE_MAP = {
    "Extract Header": "http://nuclio-nuclio-extract-header:8080",
}


def extract_google_id(url):
    """
    Extracts the file/folder ID from a standard Google URL.
    """
    match = re.search(r"/d/([a-zA-Z0-9-_]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"folders/([a-zA-Z0-9-_]+)", url)
    if match:
        return match.group(1)
    return url


def init_context(context):
    """
    Runs once when the container starts.
    Initializes Drive/Sheets clients only — VLM and sheet population
    are handled by downstream functions.
    """
    context.logger.info("sheet-populator (orchestrator): Initializing Google services...")

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"Credentials file not found at {SERVICE_ACCOUNT_FILE}")

    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    context.drive = build('drive', 'v3', credentials=creds)

    context.logger.info("sheet-populator (orchestrator): initialization complete.")


def duplicate_sheet(context, template_url: str, folder_url: str, new_file_name: str):
    template_id = extract_google_id(template_url)
    folder_id = extract_google_id(folder_url)

    file_metadata = {'name': new_file_name, 'parents': [folder_id]}
    copied_file = context.drive.files().copy(
        fileId=template_id,
        body=file_metadata,
        fields='id,webViewLink',
        supportsAllDrives=True,
    ).execute()

    return copied_file.get('id'), copied_file.get('webViewLink')


def invoke_downstream(context, downstream_url, image_b64, spreadsheet_id):
    """
    Sends a synchronous HTTP POST to a downstream Nuclio VLM function
    over the internal cvat_cvat Docker network.
    The downstream function handles everything: reading headers, VLM extraction,
    and populating the sheet. Returns the downstream response dict.
    """
    payload = {
        "image_b64": image_b64,
        "spreadsheet_id": spreadsheet_id,
    }

    context.logger.info(f"sheet-populator: invoking downstream at {downstream_url}")

    resp = requests.post(
        downstream_url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=150,  # Stay under the orchestrator's 180s eventTimeout
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"Downstream function returned {resp.status_code}: {resp.text}"
        )

    result = resp.json()
    if result.get("status") != "success":
        raise RuntimeError(
            f"Downstream function error: {result.get('message', 'unknown error')}"
        )

    return result


def handler(context, event):
    """
    Orchestrator: Triggered every time CVAT sends an interaction request.
    1. Duplicates the Google Sheet template
    2. Delegates VLM extraction + sheet population to a downstream Nuclio function
    3. Returns the sheet URL to CVAT UI
    """
    try:
        data = event.body
        if isinstance(data, bytes):
            data = json.loads(data.decode('utf-8'))

        image_b64 = data.get("image")

        payload = data.get("x-data", {})
        # context.logger.info(f"payload: {payload} && image_b64: {image_b64[:100]}")
        template_url = payload.get("template_url")
        folder_url = payload.get("folder_url")
        new_file_name = payload.get("new_file_name")
        ai_task = payload.get("ai_task")

        if not image_b64:
            raise ValueError("Missing base64 image in payload.")

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

        cropped_image = image.crop([left, top, right, bottom])

        # Save cropped image to BytesIO buffer
        img_byte_arr = io.BytesIO()
        cropped_image.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0) # Reset pointer to the start of the stream

        # Convert to base64 for the downstream function
        cropped_image_b64 = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')

        if not all([cropped_image_b64, template_url, folder_url, new_file_name, ai_task]):
            return context.Response(
                body=json.dumps({"error": "Missing image, template_url, folder_url, new_file_name or ai_task"}),
                headers={"Content-Type": "application/json"},
                status_code=400,
            )

        # --- Route to downstream VLM function ---
        downstream_url = TASK_ROUTE_MAP.get(ai_task)
        if not downstream_url:
            return context.Response(
                body=json.dumps({"error": f"Unknown ai_task: '{ai_task}'. Available: {list(TASK_ROUTE_MAP.keys())}"}),
                headers={"Content-Type": "application/json"},
                status_code=400,
            )

        # --- Duplicate the Google Sheet template ---
        spreadsheet_id, sheet_url = duplicate_sheet(context, template_url, folder_url, new_file_name)

        # --- Delegate VLM extraction + sheet population to downstream ---
        result = invoke_downstream(context, downstream_url, cropped_image_b64, spreadsheet_id)

        context.logger.info(
            f"sheet-populator: downstream completed — "
            f"{result.get('rows_updated', 0)} row(s) updated in {spreadsheet_id}"
        )

        return context.Response(
            body=json.dumps({"status": "success", "url": sheet_url, "rows_updated": result.get("rows_updated", 0)}),
            headers={"Content-Type": "application/json"},
            status_code=200,
        )

    except Exception as e:
        context.logger.error(f"sheet-populator error: {str(e)}")
        return context.Response(
            body=json.dumps({"status": "error", "message": str(e)}),
            headers={"Content-Type": "application/json"},
            status_code=500,
        )