import base64
import io
import json
import os

import requests
from PIL import Image

API_KEY = os.environ["FINUIT_API_KEY"]
BASE_URL = "https://gocr.finuit.ai/api/v1"
STRUCTURE_URL = f"{BASE_URL}/structure:document"

# Must match the "name" fields declared in function.yaml's metadata.annotations.spec
LABEL_TABLE = "master layout"
LABEL_CELL = "table data"


def init_context(context):
    context.logger.info("Init context...  0%")
    context.logger.info("Init context...100%")


def _call_pocr_structure(image_bytes: bytes) -> dict:
    """POST the frame to the pocr:Structure endpoint and return the parsed JSON body."""
    payload = {
        "use_region_detection": "true",     # layout detection -> table bbox
        "use_table_recognition": "true",    # table recognition -> cell bboxes
        "use_general_ocr": "false",
        "use_formula_recognition": "false",
        "use_seal_recognition": "false",
        "use_chart_recognition": "false",
    }
    headers = {
        "accept": "application/json",
        "X-API-Key": API_KEY,
    }
    files = {
        "file": ("frame.jpg", image_bytes, "image/jpeg"),
    }

    response = requests.post(
        STRUCTURE_URL, data=payload, files=files, headers=headers, timeout=60,
    )
    response.raise_for_status()
    return response.json()


def _to_cvat_results(api_response: dict) -> list:
    """
    Flatten a pocr:Structure response into the list of detections CVAT's
    detector contract expects:

        [{"label": ..., "points": [xtl, ytl, xbr, ybr], "type": "rectangle"}, ...]

    """
    items = api_response.get("items", [])
    results = []
    L = ["table","footer","header","figure_title"]
    # 1) master/table layout boxes first
    for item in items:
        if item.get("source") != "layout_detection":
            continue
        bbox = item.get("bbox")
        label = item.get("label")
        if label not in L:
            label = LABEL_CELL
        if not label or not bbox or len(bbox) != 4:
            continue
        results.append({
            "label": label,
            "points": [float(v) for v in bbox],
            "type": "rectangle",
        })

    # 2) table cell boxes second, so they stack above the table boxes
    for item in items:
        if item.get("source") != "table_recognition":
            continue
        for cell_bbox in item.get("cells", []):
            if not cell_bbox or len(cell_bbox) != 4:
                continue
            results.append({
                "label": LABEL_CELL,
                "points": [float(v) for v in cell_bbox],
                "type": "rectangle",
            })

    return results


def handler(context, event):
    context.logger.info("Run pocr:Structure detector")
    data = event.body
    if isinstance(data, bytes):
        context.logger.info("Received bytes data, decoding...")
        data = json.loads(data.decode("utf-8"))

    frame_base64 = data.get("image")
    if not frame_base64:
        return context.Response(
            body=json.dumps({"status": "error", "message": "Missing image."}),
            headers={},
            content_type="application/json",
            status_code=400,
        )

    try:
        image_bytes = base64.b64decode(frame_base64)

        # Normalize to a valid JPEG payload regardless of the source frame's encoding
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        buf = io.BytesIO()
        image.save(buf, format="JPEG")

        api_response = _call_pocr_structure(buf.getvalue())
        results = _to_cvat_results(api_response)

        return context.Response(
            body=json.dumps(results),
            headers={},
            content_type="application/json",
            status_code=200,
        )

    except requests.exceptions.Timeout:
        context.logger.error("pocr:Structure request timed out.")
        return context.Response(
            body=json.dumps({"status": "error", "message": "External API timeout."}),
            headers={},
            content_type="application/json",
            status_code=504,
        )
    except requests.exceptions.HTTPError as e:
        context.logger.error(f"pocr:Structure HTTP error: {e}")
        return context.Response(
            body=json.dumps({"status": "error", "message": str(e)}),
            headers={},
            content_type="application/json",
            status_code=502,
        )
    except Exception as e:
        context.logger.error(f"pocr:Structure execution error: {e}")
        return context.Response(
            body=json.dumps({"status": "error", "message": str(e)}),
            headers={},
            content_type="application/json",
            status_code=500,
        )
