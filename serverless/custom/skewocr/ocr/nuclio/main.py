import os
import io
import json
import math
import base64
import asyncio
import httpx
import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import Polygon
from typing import Dict, List, Any


API_KEY = os.environ.get("FINUIT_OCR_KEY", "")
BASE_URL = "https://gocr.finuit.ai/api/v1"
OCR_TEXT_URL = f"{BASE_URL}/ocr:text"


async def call_ocr_async(
    client: httpx.AsyncClient,
    cropped_image: Image.Image,
    semaphore: asyncio.Semaphore
) -> str:
    """
    Sends the cropped image to the OCR API asynchronously using httpx.
    The semaphore ensures we do not exceed the specified concurrency limit.
    """
    async with semaphore:
        img_byte_arr = io.BytesIO()
        cropped_image.save(img_byte_arr, format='PNG')

        headers = {
            "accept": "application/json",
            "X-API-Key": API_KEY,
        }

        # httpx uses the 'files' parameter for multipart/form-data
        files = {
            "file": ("cropped.png", img_byte_arr.getvalue(), "image/png")
        }

        try:
            # Send the async POST request with a 180-second timeout
            response = await client.post(OCR_TEXT_URL, headers=headers, files=files, timeout=180.0)

            if response.status_code == 200:
                api_result = response.json()

                if api_result.get("ok") and api_result.get("result"):
                    res_obj = api_result["result"][0].get("res", {})
                    rec_texts = res_obj.get("rec_texts", [])
                    valid_texts = [text.strip() for text in rec_texts if text.strip()]
                    return " ".join(valid_texts)

            # Fallback for empty or failed extraction
            return ""

        except Exception as e:
            print(f"Async API Error: {str(e)}")
            return ""


async def process_image_crops(image: Image.Image, bboxes: Dict[str, List[float]]) -> Dict[str, str]:
    """
    Processes all bounding boxes concurrently and routes them to the correct cropping strategy.
    """
    results: Dict[str, str] = {}
    tasks: List[asyncio.Task] = []
    cell_ids: List[str] = []

    # Restrict to 10 concurrent requests
    semaphore = asyncio.Semaphore(10)

    # Use httpx.AsyncClient for concurrent network requests
    async with httpx.AsyncClient() as client:
        for cell_id, pts in bboxes.items():
            coords = [(pts[i], pts[i+1]) for i in range(0, len(pts) - 1, 2)]

            # --- STRATEGY 1: Pure Rectangle (Fast Path) ---
            if len(coords) == 2 or len(coords) == 4:
                # Handle standard 2-point [x1, y1, x2, y2] format
                x_vals = [p[0] for p in coords]
                y_vals = [p[1] for p in coords]
                box = (min(x_vals), min(y_vals), max(x_vals), max(y_vals))
                crop_pil = image.crop(box)

            # --- STRATEGY 2: Polygon Masking (Complex Path) ---
            else:
                poly = Polygon(coords)
                minx, miny, maxx, maxy = poly.bounds

                x1, y1 = math.floor(minx), math.floor(miny)
                x2, y2 = math.ceil(maxx), math.ceil(maxy)

                # Step A: Crop to bounding box using PIL first to save memory
                region_pil = image.crop((x1, y1, x2, y2))
                region_np = np.array(region_pil)

                # Step B: Create a binary mask using PIL Draw
                local_coords = [(x - x1, y - y1) for x, y in coords]
                mask_img = Image.new("L", (region_pil.width, region_pil.height), 0)
                ImageDraw.Draw(mask_img).polygon(local_coords, fill=255)
                mask = np.array(mask_img)

                # Step C: Apply mask via NumPy vectorization (white background outside polygon)
                crop_np = np.where(mask[:, :, None] > 0, region_np, 255)
                crop_pil = Image.fromarray(crop_np.astype(np.uint8))

            # Queue the async OCR task
            tasks.append(asyncio.create_task(call_ocr_async(client, crop_pil, semaphore)))
            cell_ids.append(cell_id)

        # Wait for all OCR requests to complete concurrently
        gathered_texts = await asyncio.gather(*tasks)

        # Map the results back to their respective cell IDs
        for cid, text in zip(cell_ids, gathered_texts):
            results[cid] = text
            print(f"{cid} : {text}")  #debug

    return results


async def handler(context: Any, event: Any) -> Any:
    """
    Synchronous entry point for the Nuclio function.
    """
    try:
        data = event.body
        if isinstance(data, bytes):
            data = json.loads(data.decode('utf-8'))

        b64_image = data.get("image", "")
        x_data = data.get("x-data", {})
        bboxes = x_data.get("bboxes", {})

        # Decode base64 image ONCE into memory as a PIL Image
        image_bytes = base64.b64decode(b64_image)
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        # Execute the async event loop to process all boxes and API calls
        ocr_results = await process_image_crops(image, bboxes)

        return context.Response(
            body=json.dumps(ocr_results),
            headers={},
            content_type="application/json",
            status_code=200
        )

    except Exception as e:
        context.logger.error(f"Execution Error: {str(e)}")
        # Check if the error is a timeout from httpx or asyncio
        if isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)):
            return context.Response(
                body=json.dumps({"status": "error", "message": "External API timeout."}),
                headers={},
                content_type="application/json",
                status_code=504
            )

        return context.Response(
            body=json.dumps({"status": "error", "message": str(e)}),
            headers={},
            content_type="application/json",
            status_code=500
        )