import base64
import io
import json
import requests
from PIL import Image
import os



API_KEY = os.environ.get("FINUIT_API_KEY")
BASE_URL = "https://gocr.finuit.ai/api/v1"
OCR_TEXT_URL  = f"{BASE_URL}/ocr:text"
# ──────────────────────────────────────────────

def handler(context, event):
    try:
        data = event.body
        if isinstance(data, bytes):
            data = json.loads(data.decode('utf-8'))

        # 1. Extract image and bounding box from CVAT payload
        frame_base64 = data.get("image")
        obj_bbox = data.get("obj_bbox", None)

        if not frame_base64 or not obj_bbox or len(obj_bbox) < 2:
            return context.Response(
                body=json.dumps({"status": "error", "message": "Missing image or bounding box."}),
                headers={},
                content_type="application/json",
                status_code=400
            )

        # 2. Decode the base64 image
        image_bytes = base64.b64decode(frame_base64)
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_width, img_height = image.size

        # 3. Calculate crop coordinates (handles any drawing direction)
        x1, y1 = obj_bbox[0][0], obj_bbox[0][1]
        x2, y2 = obj_bbox[1][0], obj_bbox[1][1]

        left = max(0, int(min(x1, x2)))
        top = max(0, int(min(y1, y2)))
        right = min(img_width, int(max(x1, x2)))
        bottom = min(img_height, int(max(y1, y2)))

        if right <= left or bottom <= top:
            return context.Response(
                body=json.dumps({"status": "error", "message": "Invalid bounding box dimensions."}),
                headers={},
                content_type="application/json",
                status_code=400
            )

        # 4. Crop the image to the exact bounding box
        cropped_image = image.crop((left, top, right, bottom))

        # 5. Convert cropped PIL Image to an in-memory byte stream (no disk saving required)
        img_byte_arr = io.BytesIO()
        cropped_image.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0) # Reset pointer to the start of the stream

        # 6. Call the external API
        headers = {
            "accept": "application/json",
            "X-API-Key": API_KEY,
        }
        files = {
            "file": ("cropped.png", img_byte_arr, "image/png")
        }

        context.logger.info(f"Sending request to {OCR_TEXT_URL}")
        response = requests.post(OCR_TEXT_URL, headers=headers, files=files, timeout=120)

        if response.status_code == 200:
            api_result = response.json()
            extracted_text = ""

            # Navigate the nested JSON structure to find "rec_texts"
            if api_result.get("ok") and api_result.get("result"):
                try:
                    res_obj = api_result["result"][0].get("res", {})
                    rec_texts = res_obj.get("rec_texts", [])

                    # Filter out any empty strings and join the remaining words with a space
                    valid_texts = [text.strip() for text in rec_texts if text.strip()]
                    extracted_text = " ".join(valid_texts)
                except (IndexError, AttributeError) as e:
                    context.logger.error(f"Unexpected JSON structure: {e}")
                    extracted_text = ""
        else:
            raise Exception(f"API Error {response.status_code}: {response.text}")

        # 7. Return the CVAT Interactor contract
        return context.Response(
            body=json.dumps({
                "status": "ok",
                "text": extracted_text.strip(),
                "confidence": 1.0,
                "bbox": [[left, top], [right, bottom]]
            }),
            headers={},
            content_type="application/json",
            status_code=200
        )

    except requests.exceptions.Timeout:
        context.logger.error("External API request timed out.")
        return context.Response(
            body=json.dumps({"status": "error", "message": "External API timeout."}),
            headers={},
            content_type="application/json",
            status_code=504
        )
    except Exception as e:
        context.logger.error(f"OCR Execution Error: {str(e)}")
        return context.Response(
            body=json.dumps({"status": "error", "message": str(e)}),
            headers={},
            content_type="application/json",
            status_code=500
        )