import json
import math
import asyncio
import base64
import time
import os
import re
from io import BytesIO
from datetime import datetime, timezone
from typing import Dict

from PIL import Image, ImageDraw, ImageFont
from rectpack import newPacker, PackingMode, PackingBin
# from motor.motor_asyncio import AsyncIOMotorClient

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langchain_core.rate_limiters import InMemoryRateLimiter
from pydantic import BaseModel, Field

from dotenv import load_dotenv
load_dotenv()

# Save inside the mounted directory
debug_dir = "/opt/nuclio/debug_output"
os.makedirs(debug_dir, exist_ok=True)
# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS & SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

MAX_BIN_WIDTH  = 1200
ABSOLUTE_MAX_BIN_HEIGHT = 400

ID_BOX_HEIGHT = 40
PADDING_X = 20
PADDING_TOP = 20
PADDING_BOTTOM = 5
BORDER_WIDTH = 3

# TEXT_COLOR = "red"
TEXT_COLOR = "black"
BG_COLOR = "yellow"
CELL_BORDER_COLOR = "red"       # Bright red clearly outlines the entire cell data zone
ID_BORDER_COLOR = "black"

# ── ID font: prefer a bold monospace with unambiguous digit shapes.
# DejaVu Sans Mono Bold has wide-open apertures — 6, 8, 0, 9 are never confused.
# Font size 36 gives the model a comfortable read area even on dense canvases.

ID_FONT_SIZE = 30


MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0

# MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
# MONGO_DB  = "vlm_validation"
# COLLECTION_NAME = "interactive_validations"


# 1. Simplified Pydantic Schema (Dict[str, str])
class CanvasOCR(BaseModel):
    """Maps numeric crop identifiers to their extracted text."""
    extracted_text: Dict[str, str] = Field(
        description="A dictionary mapping string numeric identifiers to their extracted text."
    )

SYSTEM_PROMPT = f"""
You are an expert Vision-OCR assistant specializing in extracting text from dense, multi-crop composite images.
Your task is to scan the canvas systematically and map every single flagged data region to its correct identifier.

Follow these strict structural rules:
1. IDENTIFY CELLS: The canvas contains multiple distinct data rows/cells. Each individual cell is completely enclosed inside a prominent {CELL_BORDER_COLOR.upper()} rectangular border.
2. LOCATE IDENTIFIERS: Look at the top-left corner of every {CELL_BORDER_COLOR.upper()} cell. You will see an administrative ID tag box: a small rectangle with a {ID_BORDER_COLOR.upper()} border, filled with a solid {BG_COLOR.upper()} background. Inside this box is a numeric identifier printed in {TEXT_COLOR.upper()}.
3. ID CHARACTERISTICS: These identifiers are random, non-sequential, and unsorted (e.g., 23, 4, 0, 11). Do not guess or assume a sequence. Transcribe only the exact numbers physically rendered in the image.
4. TEXT EXTRACTION ZONE: For each cell, read the identifier number from the {ID_BORDER_COLOR.upper()} tag box, then transcribe the text crop located directly below that tag box, safely within the boundaries of the main {CELL_BORDER_COLOR.upper()} cell frame.
5. TRANSCRIPTION FIDELITY: Capture the text exactly as it appears. Preserve spelling, dates, symbols, and formatting.
6. EMPTY CELLS (EDGE CASE): If a {CELL_BORDER_COLOR.upper()} cell contains an ID tag box but has absolutely no image crop or data text below it, map that identifier to an empty string ("").
7. ISOLATION: Treat each {CELL_BORDER_COLOR.upper()} box as completely independent. Never merge text strings or switch identifiers between cells.
8. OUTPUT FORMAT: Output *only* the raw JSON object matching the requested schema. Do not output conversational text, preambles, explanations, or chain-of-thought markdown blocks.
"""

USER_PROMPT = f"""
Systematically scan the canvas from top-to-bottom and left-to-right.
Locate every cell enclosed in a {CELL_BORDER_COLOR.upper()} border. For every cell found, read its numeric ID from the top-left {ID_BORDER_COLOR.upper()}-bordered tag box, extract the text beneath it, and format it exactly according to the requested schema.
"""
# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────
def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = str(text)
    # text = text.lower()
    text = re.sub(r'[\n\r\t]', ' ', text)
    text = re.sub(r'\s*([.,;:!?\-\(\)\[\]{}""\'])\s*', r'\1', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def _pil_to_base64(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode('utf-8')

# ─────────────────────────────────────────────────────────────────────────────
# ASYNC PIPELINE
# ─────────────────────────────────────────────────────────────────────────────
async def acall_canvas_with_retry(structured_llm, canvas: Image.Image) -> dict:
    """Calls the LLM using Structured Output and returns a dict mapping cell_id -> text."""
    image_b64 = await asyncio.get_running_loop().run_in_executor(None, _pil_to_base64, canvas)

    # Change data:image/jpeg to data:image/png
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=[
            {"type": "text", "text": USER_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
        ]),
    ]

    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            response: CanvasOCR = await structured_llm.ainvoke(messages)
            return response.extracted_text

        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(RETRY_BACKOFF_BASE ** attempt)

    raise last_exc

async def process_canvas(bin_idx, canvas, cell_ids, gt_map, structured_llm, semaphore):
    """Processes a single packed bin within a concurrency semaphore."""
    async with semaphore:
        try:
            extracted_text_map = await acall_canvas_with_retry(structured_llm, canvas)
        except Exception as e:
            print(f"Canvas {bin_idx} failed: {e}")
            return {
                cid: {
                    "match": False,
                    "lltext": ""
                }
                for cid in cell_ids
            }

        results = {}
        for cid in cell_ids:
            gt_norm = normalize_text(gt_map.get(cid, {}).get("text", ""))
            llm_norm = normalize_text(extracted_text_map.get(cid, ""))
            results[cid] = {
                "match" : (gt_norm == llm_norm),
                "lltext": (llm_norm)
            }

        return results

async def run_validation_pipeline(b64_image: str, raw_rects: dict, obj_bbox: list) -> dict:
    """Main execution flow per request."""
    start_time = time.time()

    # 1. Instantiate per-request DB and Controls
    # mongo_client = AsyncIOMotorClient(MONGO_URI)
    # db = mongo_client[MONGO_DB]
    # collection = db[COLLECTION_NAME]

    # Concurrency and Rate Limiting
    semaphore = asyncio.Semaphore(30)  # Max 10 active memory-heavy threads
    rate_limiter = InMemoryRateLimiter(
        requests_per_second=15,
        check_every_n_seconds=0.1,
        max_bucket_size=30,
    )

    base_llm = ChatOpenAI(
        model="google/gemma-4-26B-A4B-it",
        base_url="https://openrouter.ai/api/v1",
        rate_limiter=rate_limiter,
        temperature=0,
        extra_body={
            "provider": {
                "require_parameters": True,
                "only": ["google-vertex"],
                "allow_fallbacks": False,
            }
        }
    )

    # base_llm = ChatOpenAI(
    #     model="google/gemini-3.1-flash-lite",
    #     base_url="https://openrouter.ai/api/v1",
    #     extra_body={"service_tier": "flex"},
    #     rate_limiter=rate_limiter,
    #     temperature=0
    # )
    structured_llm = base_llm.with_structured_output(CanvasOCR)

    try:
        # 2. Decode original image
        if b64_image.startswith("data:image"):
            b64_image = b64_image.split(",")[1]

        image_bytes = base64.b64decode(b64_image)
        with Image.open(BytesIO(image_bytes)) as src_img:
            src_img.load()

            # 3. Prepare cells and track maximum required crop height
            gt_map = {}
            packer = newPacker(rotation=False, mode=PackingMode.Offline, bin_algo=PackingBin.Global)

            total_area = 0
            max_crop_height = 0

            for original_cell_id, data in raw_rects.items():
                cid_str = str(original_cell_id)
                bbox = data.get("rects") or data.get("bbox", [])

                gt_map[cid_str] = {
                    "text": data.get("text", ""),
                    "bbox": bbox
                }

                if not bbox:
                    continue

                xtl, ytl, xbr, ybr = [float(v) for v in bbox]

                # Estimate badge height: border + text_height(≈ ID_FONT_SIZE) + 2×pad + border + gap
                # Using ID_FONT_SIZE as a safe upper bound for text height before rendering.
                estimated_id_box_height = BORDER_WIDTH + (BORDER_WIDTH * 2) + ID_FONT_SIZE + (10 * 2) + 6
                crop_w = int(xbr - xtl) + (PADDING_X * 2)
                crop_h = int(ybr - ytl) + estimated_id_box_height + PADDING_BOTTOM

                packer.add_rect(crop_w, crop_h, cid_str)
                total_area += (crop_w * crop_h)


            dynamic_bin_height = ABSOLUTE_MAX_BIN_HEIGHT

            bin_area = MAX_BIN_WIDTH * dynamic_bin_height
            # num_bins = math.ceil(total_area / bin_area) + 5 if total_area > 0 else 0
            num_bins = len(raw_rects)
            print(f"RAW : {num_bins}")

            for _ in range(num_bins):
                packer.add_bin(MAX_BIN_WIDTH, dynamic_bin_height)

            packer.pack()
            all_rects = packer.rect_list()

            # 5. Build Canvases dynamically
            id_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", ID_FONT_SIZE)

            # ── Shared layout constants for the identifier badge
            ID_PAD_X = 10   # horizontal padding inside badge
            ID_PAD_Y = 8   # vertical padding inside badge
            ID_BORDER_W = 3  # badge border thickness (distinct from cell border)
            ID_CROP_GAP = 6  # gap between bottom of badge and top of crop image

            canvases = {}
            l=[]
            for b, x, y, w, h, rid in all_rects:
                if b not in canvases:
                    canvases[b] = {"canvas": Image.new('RGB', (MAX_BIN_WIDTH, dynamic_bin_height), 'white'), "cell_ids": []}


                l.append(rid)
                orig_bbox = gt_map[rid]["bbox"]
                crop = src_img.crop((float(orig_bbox[0]), float(orig_bbox[1]), float(orig_bbox[2]), float(orig_bbox[3])+5))

                cell_tile = Image.new('RGB', (w, h), 'white')
                draw = ImageDraw.Draw(cell_tile)

                # 1. Measure the identifier text precisely using textbbox (Pillow ≥ 8.0).
                #    textbbox returns (left, top, right, bottom) relative to the anchor;
                #    using anchor (0,0) keeps offsets predictable.
                if hasattr(draw, 'textbbox'):
                    tb = draw.textbbox((0, 0), rid, font=id_font)
                    text_w = tb[2] - tb[0]
                    text_h = tb[3] - tb[1]
                    text_offset_x = -tb[0]   # shift so leftmost pixel starts at x=0
                    text_offset_y = -tb[1]   # shift so topmost pixel starts at y=0
                else:
                    # Older Pillow: textsize gives (w, h) but no baseline offset info
                    text_w, text_h = draw.textsize(rid, font=id_font)
                    text_offset_x = 0
                    text_offset_y = 0

                # 2. Compute badge (identifier box) dimensions from measured text.
                badge_inner_w = text_w + ID_PAD_X * 2
                badge_inner_h = text_h + ID_PAD_Y * 2
                # Badge starts at cell border's inner edge
                badge_x0 = BORDER_WIDTH
                badge_y0 = BORDER_WIDTH
                badge_x1 = badge_x0 + ID_BORDER_W + badge_inner_w + ID_BORDER_W
                badge_y1 = badge_y0 + ID_BORDER_W + badge_inner_h + ID_BORDER_W

                # 3. Paste the crop image safely below the badge + gap.
                crop_paste_y = badge_y1 + ID_CROP_GAP
                crop_paste_x = BORDER_WIDTH + PADDING_X
                cell_tile.paste(crop, (crop_paste_x, crop_paste_y))

                # 4. Draw outer cell border (red, clearly visible).
                draw.rectangle(
                    [0, 0, w - 1, h - 1],
                    outline=CELL_BORDER_COLOR,
                    width=BORDER_WIDTH
                )

                # 5. Draw the identifier badge: yellow fill, black border.
                draw.rectangle(
                    [badge_x0, badge_y0, badge_x1, badge_y1],
                    fill=BG_COLOR,
                    outline=ID_BORDER_COLOR,
                    width=ID_BORDER_W
                )

                # 6. Draw identifier text perfectly centred inside the badge.
                #    Derive top-left so text is optically centred after accounting
                #    for textbbox offset (some fonts have descenders shifting bbox).
                text_x = badge_x0 + ID_BORDER_W + ID_PAD_X + text_offset_x
                text_y = badge_y0 + ID_BORDER_W + ID_PAD_Y + text_offset_y
                draw.text(
                    (text_x, text_y),
                    rid,
                    fill=TEXT_COLOR,
                    font=id_font,
                )

                canvases[b]["canvas"].paste(cell_tile, (x, y))
                canvases[b]["cell_ids"].append(rid)
            print(f"NO OF RECTS : {len(l)}")

        for b_idx, data in canvases.items():
            debug_path = f"{debug_dir}/canvas_{b_idx}.png"
            data["canvas"].save(debug_path)

        # 6. Fire Async Canvas Tasks with Semaphore
        tasks = [
            process_canvas(b_idx, data["canvas"], data["cell_ids"], gt_map, structured_llm, semaphore)
            for b_idx, data in canvases.items()
        ]

        bin_results = await asyncio.gather(*tasks)

        # 7. Aggregate results
        final_results = {}
        for r in bin_results:
            final_results.update(r)

        for cid in gt_map.keys():
            if cid not in final_results:
                final_results[cid] = {
                    "match": False,
                    "lltext": ""  # Explicitly pass empty string to frontend
                }

        # 8. Store metrics in MongoDB (Commented Out)
        end_time = time.time()
        # await collection.insert_one({
        #     "timestamp": datetime.now(timezone.utc).isoformat(),
        #     "total_time_s": round(end_time - start_time, 2),
        #     "num_bins": len(canvases),
        #     "dynamic_bin_height": dynamic_bin_height,
        #     "status": "completed",
        #     "total_cells": len(raw_rects),
        #     "results": final_results
        # })

        return final_results

    finally:
        pass
        # mongo_client.close()

# ─────────────────────────────────────────────────────────────────────────────
# NUCLIO HANDLER ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────
# Change this signature to async
async def handler(context, event):
    try:

        # # 1. Parse Payload
        body = event.body
        if isinstance(body, dict):
            payload = body
        elif isinstance(body, (bytes, str)):
            payload = json.loads(body)
        else:
            payload = json.loads(str(body))

        b64_image = payload.get("image", "")
        x_data = payload.get("x-data", {})
        raw_rects = x_data.get("rects", {})
        obj_bbox = x_data.get("obj_bbox") or payload.get("obj_bbox")

        if not b64_image or not raw_rects:
            return context.Response(body=json.dumps({"error": "Missing data"}), status_code=400)

        final_results = await run_validation_pipeline(b64_image, raw_rects, obj_bbox)

        # 3. Return response
        return context.Response(
            body=json.dumps(final_results),
            headers={"Content-Type": "application/json"},
            status_code=200
        )

    except Exception as e:
        context.logger.error(f"Function error: {str(e)}")
        return context.Response(body=json.dumps({"error": str(e)}), status_code=500)