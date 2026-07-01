import json
import math
import asyncio
import base64
import time
import os
import re
from io import BytesIO
from datetime import datetime, timezone
from typing import Dict, Any, TypedDict

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from rectpack import newPacker, PackingMode, PackingBin
from shapely.geometry import Polygon

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langchain_core.rate_limiters import InMemoryRateLimiter
from pydantic import BaseModel, Field


import mlflow.langchain
import mlflow
mlflow.set_experiment("cvat_qc_automation")
mlflow.langchain.autolog()

DEFAULT_BIN_WIDTH = 1200
DEFAULT_BIN_HEIGHT = 500
ID_BOX_HEIGHT = 40
PADDING_TOP = 20
PADDING = 15
PADDING_BOTTOM = 5
BORDER_WIDTH = 3


TEXT_COLOR = "black"
BG_COLOR = "yellow"
CELL_BORDER_COLOR = "red"
ID_BORDER_COLOR = (127,127,127,60)
t_ID_BORDER_COLOR = "gray"

ID_FONT_SIZE = 30
id_font = ImageFont.truetype(
    "./opt/nuclio/font.ttf", ID_FONT_SIZE
)

ID_PAD_X = 10  # horizontal padding inside badge
ID_PAD_Y = 8  # vertical padding inside badge
ID_BORDER_WIDTH = 3  # badge border thickness (distinct from cell border)

temp_image = Image.new("RGBA", (1000, 1000), "white")
draw_image = ImageDraw.Draw(temp_image)


class XDataItem(TypedDict):
    text: str
    rects: list[float]


class CanvasOCR(BaseModel):
    extracted_text: Dict[str, str] = Field(
        description="A dictionary mapping string numeric identifiers to their extracted text."
    )


SYSTEM_PROMPT = f"""
You are an expert Vision-OCR assistant specializing in extracting text from dense, multi-crop composite images.
Your task is to scan the canvas systematically and map every single flagged data region to its correct identifier.

Follow these strict structural rules:
1. IDENTIFY CELLS: The canvas contains multiple distinct data rows/cells. Each individual cell is completely enclosed inside a prominent {CELL_BORDER_COLOR.upper()} rectangular border.
2. LOCATE IDENTIFIERS: Look at the top-left corner of every {CELL_BORDER_COLOR.upper()} cell. You will see an administrative ID tag box: a small rectangle with a {t_ID_BORDER_COLOR.upper()} border, filled with a solid {BG_COLOR.upper()} background. Inside this box is a numeric identifier printed in {TEXT_COLOR.upper()}.
3. ID CHARACTERISTICS: These identifiers are random, non-sequential, and unsorted (e.g., 23, 4, 0, 11). Do not guess or assume a sequence. Transcribe only the exact numbers physically rendered in the image.
4. TEXT EXTRACTION ZONE: For each cell, read the identifier number from the {t_ID_BORDER_COLOR.upper()} tag box, then transcribe the text crop located directly below that tag box, safely within the boundaries of the main {CELL_BORDER_COLOR.upper()} cell frame.
5. TRANSCRIPTION FIDELITY: Capture the text exactly as it appears. Preserve spelling, dates, symbols, and formatting.
6. EMPTY CELLS (EDGE CASE): If a {CELL_BORDER_COLOR.upper()} cell contains an ID tag box but has absolutely no image crop or data text below it, map that identifier to an empty string ("").
7. ISOLATION: Treat each {CELL_BORDER_COLOR.upper()} box as completely independent. Never merge text strings or switch identifiers between cells.
8. OUTPUT FORMAT: Output *only* the raw JSON object matching the requested schema. Do not output conversational text, preambles, explanations, or chain-of-thought markdown blocks.
"""

USER_PROMPT = f"""
Systematically scan the canvas from top-to-bottom and left-to-right.
Locate every cell enclosed in a {CELL_BORDER_COLOR.upper()} border. For every cell found, read its numeric ID from the top-left {t_ID_BORDER_COLOR.upper()}-bordered tag box, extract the text beneath it, and format it exactly according to the requested schema.
"""


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = str(text)
    text = re.sub(r"[^\x00-\x7F]+", "", text)
    text = re.sub(r"[\n\r\t]", " ", text)
    text = re.sub(r'\s*([.,;:!?\-\(\)\[\]{}""\'])\s*', r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def resolve_crop_region(img: Image.Image, coords: list[float]):
    if len(coords) == 4:
        # Plain rectangle: [x1, y1, x2, y2]
        x1, y1, x2, y2 = coords
        x1, y1 = math.floor(x1), math.floor(y1)
        x2, y2 = math.ceil(x2), math.ceil(y2)
        crop = img.crop((x1, y1, x2, y2))
        return x1, y1, x2, y2, crop

    # Polygon case: flat list of (x, y) pairs, e.g. [x1,y1,x2,y2,x3,y3,...]
    if len(coords) < 6 or len(coords) % 2 != 0:
        raise ValueError(
            f"Invalid coordinate list of length {len(coords)}; expected 4 "
            f"values for a rectangle or an even number >= 6 for a polygon."
        )

    pairs = [(coords[i], coords[i + 1]) for i in range(0, len(coords), 2)]
    poly = Polygon(pairs)
    minx, miny, maxx, maxy = poly.bounds

    x1, y1 = math.floor(minx), math.floor(miny)
    x2, y2 = math.ceil(maxx), math.ceil(maxy)

    # Step A: Crop to bounding box using PIL first to save memory
    region_pil = img.crop((x1, y1, x2, y2)).convert("RGBA")
    region_np = np.array(region_pil)

    # Step B: Create a binary mask using PIL Draw
    local_coords = [(x - x1, y - y1) for x, y in pairs]
    mask_img = Image.new("L", (region_pil.width, region_pil.height), 0)
    ImageDraw.Draw(mask_img).polygon(local_coords, fill=255)
    mask = np.array(mask_img)

    # Step C: Apply mask via NumPy vectorization (white background outside polygon)
    crop_np = np.where(mask[:, :, None] > 0, region_np, 255)
    crop_pil = Image.fromarray(crop_np.astype(np.uint8))

    return x1, y1, x2, y2, crop_pil


def pil_to_base64(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


async def acall_canvas_with_retry(structured_llm, canvas: Image.Image) -> dict:
    image_b64 = await asyncio.to_thread(pil_to_base64, canvas)

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(
            content=[
                {"type": "text", "text": USER_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                },
            ]
        ),
    ]

    response: CanvasOCR = await structured_llm.ainvoke(messages)
    return response.extracted_text


async def process_canvas(
    bin_idx: int,
    canvas: Image.Image,
    cell_ids: dict[str, str],
    gt_map: dict[str, Any],
    structured_llm,
    semaphore,
):
    async with semaphore:
        try:
            extracted_text_map = await acall_canvas_with_retry(structured_llm, canvas)
        except Exception as e:
            print(f"Canvas {bin_idx} failed: {e}")
            return {cid: {"match": False, "lltext": ""} for cid in cell_ids}

        results = {}
        for cid in cell_ids:
            gt_norm = normalize_text(gt_map.get(cid, {}).get("text", ""))
            llm_norm = normalize_text(extracted_text_map.get(cid, ""))
            results[cid] = {"match": (gt_norm == llm_norm), "lltext": (llm_norm)}

        return results


async def run_validation_pipeline(
    b64_image: str,
    raw_rects: dict[str, XDataItem],
) -> dict:
    start_time = time.time()

    semaphore = asyncio.Semaphore(20)
    rate_limiter = InMemoryRateLimiter(
        requests_per_second=15,
        check_every_n_seconds=0.1,
        max_bucket_size=20,
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
        },
    )

    structured_llm = base_llm.with_structured_output(CanvasOCR)
    image_bytes = base64.b64decode(b64_image)

    with Image.open(BytesIO(image_bytes)) as img:
        _ = img.load()

        gt_map: dict[str, Any] = {}
        packer = newPacker(
            rotation=False,
            mode=PackingMode.Offline,
            bin_algo=PackingBin.Global,
        )

        cell_images: dict[str, Image.Image] = {}  # cell_id -> image

        for cell_id, data in raw_rects.items():
            cell_id_str = str(cell_id)
            bbox = data["rects"]
            gt_map[cell_id_str] = {"text": data["text"], "bbox": bbox}

            x1, y1, x2, y2, masked_crop = resolve_crop_region(img, bbox)

            tx1, ty1, tx2, ty2 = draw_image.textbbox(
                (0, 0), cell_id_str, font=id_font, align="left"
            )

            y2 = y2 + 3 #extend down a bit

            tx1 = tx1 - ID_PAD_X
            tx2 = tx2 + ID_PAD_X
            ty1 = ty1 - ID_PAD_Y
            ty2 = ty2 + ID_PAD_Y

            gap_up = 5
            pup = max(ty2 - ty1, PADDING_TOP) + gap_up
            pright = max(tx2 - tx1, PADDING)
            pdown = PADDING_BOTTOM
            pleft = PADDING

            tile_height = math.ceil(pup + pdown + y2 - y1) + (2 * (BORDER_WIDTH + ID_BORDER_WIDTH ))
            tile_width = math.ceil(pleft + pright + x2 - x1) + (2 * (BORDER_WIDTH + ID_BORDER_WIDTH))
            tile_image = Image.new("RGBA", (tile_width, tile_height), "white")
            draw_tile = ImageDraw.Draw(tile_image)
            _ =  packer.add_rect(tile_width, tile_height, cell_id_str)
            cell_images[cell_id_str] = tile_image

            # outer red border
            draw_tile.rectangle(
                [0, 0, tile_width - 1, tile_height - 1],
                outline=CELL_BORDER_COLOR,
                width=BORDER_WIDTH,
            )

            # outer fill
            draw_tile.rectangle(
                [
                    BORDER_WIDTH,
                    BORDER_WIDTH,
                    BORDER_WIDTH + (tx2 - tx1) + (2*ID_BORDER_WIDTH) -1,
                    BORDER_WIDTH + (ty2 - ty1) + (2*ID_BORDER_WIDTH) -1
                ],
                fill=BG_COLOR,
                outline=ID_BORDER_COLOR,
                width=ID_BORDER_WIDTH
            )

            draw_tile.text(
                (BORDER_WIDTH + ID_PAD_X, BORDER_WIDTH + ID_PAD_Y),
                cell_id_str,
                fill=TEXT_COLOR,
                font=id_font,
            )

            # place the image
            crop = masked_crop
            tile_image.paste(crop, (BORDER_WIDTH + pleft, math.ceil(BORDER_WIDTH + ty2 + gap_up)))

        num_bins = len(cell_images)
        min_bin_width = max([im.width for im in cell_images.values()])
        min_bin_height = max([im.height for im in cell_images.values()])

        bin_width = max(min_bin_width, DEFAULT_BIN_WIDTH)
        bin_height = max(min_bin_height, DEFAULT_BIN_HEIGHT)

        canvases: dict[int, Image.Image] = {} # bin_id -> Image

        for _ in range(num_bins):
            packer.add_bin(bin_width, bin_height)

        packer.pack()
        rect_list = packer.rect_list()


        all_rids = set(cell_images.keys())
        for bin, x, y, _, _, rid in rect_list:
            if bin not in canvases:
                canvases[bin] = {
                    "canvas": Image.new("RGBA", (bin_width, bin_height), "white"),
                    "cell_ids": [],
                }

            canvases[bin]["canvas"].paste(cell_images[rid], (x, y))
            canvases[bin]["cell_ids"].append(rid)
            all_rids.discard(rid)


        if all_rids:
            # PANIC : Cells that didn't fit :: get single-cell canvases
            for rid in all_rids:
                tile = cell_images[rid]
                solo_canvas = Image.new("RGBA", (tile.width, tile.height), "white")
                solo_canvas.paste(tile, (0, 0))
                bin_id = len(canvases)
                canvases[bin_id] = {"canvas": solo_canvas, "cell_ids": [rid]}

        tasks = [
            process_canvas(b_idx, data["canvas"], data["cell_ids"], gt_map, structured_llm, semaphore)
            for b_idx, data in canvases.items()
        ]

        bin_results = await asyncio.gather(*tasks)

         # Aggregate results
        final_results: dict[str, Any] = {}
        for r in bin_results:
            final_results.update(r)

        # Fill in any cell that was never processed (e.g. empty gt_map entries)
        for cid in gt_map:
            if cid not in final_results:
                final_results[cid] = {"match": False, "lltext": ""}

        return final_results


async def handler(context, event):
    try:
        body = event.body
        if isinstance(body, dict):
            payload = body
        elif isinstance(body, (bytes, str)):
            payload = json.loads(body)
        else:
            payload = json.loads(str(body))

        b64_image = payload.get("image", "")
        raw_rects: dict[str, XDataItem] = payload["x-data"]["rects"]

        if not b64_image or not raw_rects:
            return context.Response(
                body=json.dumps({"error": "Missing data"}), status_code=400
            )
        with mlflow.start_run():
            final_results = await run_validation_pipeline(b64_image, raw_rects)

        return context.Response(
            body=json.dumps(final_results),
            headers={"Content-Type": "application/json"},
            status_code=200,
        )

    except Exception as e:
        context.logger.error(f"Function error: {str(e)}")
        return context.Response(body=json.dumps({"error": str(e)}), status_code=500)