import base64
import io
import json
import os
import traceback
from typing import Any, TypedDict

import httpx
from httpx import HTTPStatusError
from PIL import Image
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

API_KEY: str = os.environ["FINUIT_API_KEY"]
BASE_URL: str = "https://gocr.finuit.ai/api/v1"
STRUCTURE_URL: str = f"{BASE_URL}/structure:document"

# Must match the "name" fields declared in function.yaml's metadata.annotations.spec
LABEL_CELL: str = "table data"

LAYOUT_LABELS: set[str] = {"table", "footer", "header", "figure_title"}


class CvatDetection(TypedDict):
    label: str
    points: list[float]
    type: str


def init_context(context: Any) -> None:
    context.logger.info("Init context...  0%")
    context.logger.info("Init context...100%")


def _log_retry(retry_state: Any) -> None:
    """Print full traceback before each tenacity retry."""
    exc = retry_state.outcome.exception()
    if exc:
        print(f"Attempt {retry_state.attempt_number} failed:")
        traceback.print_exception(type(exc), exc, exc.__traceback__)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=15, jitter=2),
    retry=retry_if_exception_type(HTTPStatusError),
    before_sleep=_log_retry,
    reraise=True,
)
async def _call_pocr_structure(image_bytes: bytes) -> dict[str, Any]:
    """POST the frame to the pocr:Structure endpoint and return the parsed JSON."""
    payload: dict[str, str] = {
        "use_region_detection": "true",
        "use_table_recognition": "true",
        "use_general_ocr": "false",
        "use_formula_recognition": "false",
        "use_seal_recognition": "false",
        "use_chart_recognition": "false",
    }
    headers: dict[str, str] = {
        "accept": "application/json",
        "X-API-Key": API_KEY,
    }
    files: dict[str, tuple[str, bytes, str]] = {
        "file": ("frame.jpg", image_bytes, "image/jpeg"),
    }

    async with httpx.AsyncClient(timeout=60, http2=True) as client:
        response = await client.post(
            STRUCTURE_URL, data=payload, files=files, headers=headers,
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
    return result


def _to_cvat_results(api_response: dict[str, Any]) -> list[CvatDetection]:
    """
    Flatten a pocr:Structure response into the list of detections CVAT's
    detector contract expects:

        [{"label": ..., "points": [xtl, ytl, xbr, ybr], "type": "rectangle"}, ...]

    """
    items: list[dict[str, Any]] = api_response.get("items", [])
    results: list[CvatDetection] = []

    for item in items:
        if item.get("source") != "layout_detection":
            continue
        bbox: list[float] | None = item.get("bbox")
        label: str | None = item.get("label")
        if label not in LAYOUT_LABELS:
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


async def handler(context: Any, event: Any) -> Any:
    """
    Nuclio HTTP handler — runs pocr:Structure detection on a base64-encoded frame.
    Exceptions propagate to Nuclio's built-in panic handler.
    """
    context.logger.info("Running pocr: Structure detector")
    data: dict[str, Any] = event.body
    if isinstance(data, bytes):
        data = json.loads(data.decode("utf-8"))

    frame_base64: str | None = data.get("image")
    if not frame_base64:
        return context.Response(
            body=json.dumps({"status": "error", "message": "Missing image."}),
            headers={},
            content_type="application/json",
            status_code=400,
        )

    image_bytes: bytes = base64.b64decode(frame_base64)

    # Normalize to a valid JPEG payload regardless of the source frame's encoding
    image: Image.Image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buf: io.BytesIO = io.BytesIO()
    image.save(buf, format="JPEG")

    api_response: dict[str, Any] = await _call_pocr_structure(buf.getvalue())
    results: list[CvatDetection] = _to_cvat_results(api_response)

    context.logger.info(f"pocr : Structure responsed successfully")

    return context.Response(
        body=json.dumps(results),
        headers={},
        content_type="application/json",
        status_code=200,
    )
