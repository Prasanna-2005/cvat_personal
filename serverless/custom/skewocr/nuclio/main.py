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


API_KEY = os.environ["FINUIT_OCR_KEY"]
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
                    rec_boxes = res_obj.get("rec_boxes", [])  # [x1, y1, x2, y2]

                    if rec_boxes and len(rec_boxes) == len(rec_texts):
                        # Compute centre point of each box
                        centres = [
                            ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)   # (cx, cy)
                            for b in rec_boxes
                        ]

                        # Estimate a typical line height to use as row-grouping tolerance
                        heights = [b[3] - b[1] for b in rec_boxes]
                        median_h = sorted(heights)[len(heights) // 2]
                        row_tolerance = median_h * 0.6   # centres within 60% of line-height → same row

                        # Group into rows by cy proximity
                        items = sorted(zip(centres, rec_texts), key=lambda p: p[0][1])  # sort all by cy first
                        rows: list[list[tuple]] = []
                        for centre, text in items:
                            # If rows list is empty, start the first row
                            if not rows:
                                rows.append([(centre, text)])
                                continue

                            last_row = rows[-1]
                            row_cy = sum(c[1] for c, _ in last_row) / len(last_row) 

                            # Check against only the last row
                            if abs(centre[1] - row_cy) <= row_tolerance:
                                last_row.append((centre, text))
                            else:
                                rows.append([(centre, text)])
                        # Within each row sort by cx left→right
                        ordered_texts = []
                        for row in rows:
                            row.sort(key=lambda p: p[0][0])
                            ordered_texts.extend(text for _, text in row)

                    else:
                        ordered_texts = rec_texts

                    valid_texts = [t.strip() for t in ordered_texts if t.strip()]
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

            if len(pts) == 4:   # exactly [x1,y1,x2,y2]
                box = (min(pts[0],pts[2]), min(pts[1],pts[3]),
                    max(pts[0],pts[2]), max(pts[1],pts[3]))
                crop_pil = image.crop(box)

            else:               # polygon: 6,8,10,... values
                coords = [(pts[i], pts[i+1]) for i in range(0, len(pts), 2)]
                poly = Polygon(coords)

                obb = poly.minimum_rotated_rectangle
                obb_coords = list(obb.exterior.coords)[:-1]  # 4 corners, exclude closing point

                # Always use the longest edge as the rotation reference
                # (longest edge = text runs along it, regardless of which corner Shapely started from)
                edges = [
                    (obb_coords[i], obb_coords[(i + 1) % 4])
                    for i in range(4)
                ]
                longest = max(edges, key=lambda e: (e[1][0]-e[0][0])**2 + (e[1][1]-e[0][1])**2)
                p1, p2 = longest

                dx, dy = p2[0] - p1[0], p2[1] - p1[1]
                angle_deg = math.degrees(math.atan2(dy, dx))  # in y-down space: +angle = clockwise tilt

                # Normalize to (-90, 90] so we always take the shortest rotation path
                if angle_deg > 90:
                    angle_deg -= 180
                if angle_deg <= -90:
                    angle_deg += 180

                minx, miny, maxx, maxy = poly.bounds     # AABB of original polygon
                x1, y1 = math.floor(minx), math.floor(miny)
                x2, y2 = math.ceil(maxx),  math.ceil(maxy)

                # Step A: Crop to AABB
                region_pil = image.crop((x1, y1, x2, y2)).convert("RGB")
                region_np  = np.array(region_pil)

                # Step B: Numpy mask — white out pixels outside the polygon
                local_coords = [(x - x1, y - y1) for x, y in coords]
                mask_img = Image.new("L", (region_pil.width, region_pil.height), 0)
                ImageDraw.Draw(mask_img).polygon(local_coords, fill=255)
                mask = np.array(mask_img)
                masked_np  = np.where(mask[:, :, None] > 0, region_np, 255)
                masked_pil = Image.fromarray(masked_np.astype(np.uint8))

                # Step C: Rotate the masked crop to make text upright
                crop_pil = masked_pil.rotate(angle_deg, expand=True, fillcolor="white")

            # Queue the async OCR task
            tasks.append(asyncio.create_task(call_ocr_async(client, crop_pil, semaphore)))
            cell_ids.append(cell_id)

        # Wait for all OCR requests to complete concurrently
        gathered_texts = await asyncio.gather(*tasks)

        # Map the results back to their respective cell IDs
        for cid, text in zip(cell_ids, gathered_texts):
            results[cid] = text

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
