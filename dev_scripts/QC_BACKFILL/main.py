#!/usr/bin/env python3
"""One-off CVAT table QC backfill. Env: CVAT_URL, CVAT_TOKEN, MISTRAL_API_KEY.

Only jobs listed in jobs.txt (one ID per line) are processed.

uv run cvat_api_sdk/QC/main.py --project-id 2 --task-ids 10 --dry-run
uv run cvat_api_sdk/QC/main.py --project-id 2 --task-range 4 12
uv run cvat_api_sdk/QC/main.py --project-id 2 --all-tasks
uv run cvat_api_sdk/QC/main.py --project-id 2 --all-tasks --filter-jobs --dry-run
uv run cvat_api_sdk/QC/main.py --project-id 2 --all-tasks --filter-jobs --batch
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import threading
from io import BytesIO
from pathlib import Path

from cvat_sdk.core.client import AccessTokenCredentials, Client
from cvat_sdk.core.helpers import get_paginated_collection
from dotenv import load_dotenv
from PIL import Image

from mistral_qc import default_credentials_path, run_table_qc
from mistral_qc_batch import BATCH_SIZE, run_table_qc_batch

load_dotenv()

LABEL, ATTR = "table", "excel_link"
SHEET_RE = re.compile(r"(?:docs\.google\.com/spreadsheets/d/)?([a-zA-Z0-9_-]{20,})")

PARENT = Path(__file__).resolve()
LOG = PARENT / "qc_progress.txt"
JOBS_FILE = PARENT / "jobs.txt"
CONCURRENCY = 5
_log_lock = threading.Lock()
_cvat_lock = threading.Lock()  # cvat_sdk client is not thread-safe


def load_job_ids(path: Path) -> set[int]:
    """Load job IDs from a text file (one integer per line)."""
    if not path.is_file():
        sys.exit(f"jobs file not found: {path}")
    ids: set[int] = set()
    for ln in path.read_text().splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        ids.add(int(s))
    if not ids:
        sys.exit(f"no job ids in {path}")
    return ids


def make_cvat_client(url: str, token: str) -> Client:
    """PAT client without the noisy server/SDK version warning."""
    client = Client(url=url, check_server_version=False)
    client.login(AccessTokenCredentials(token))
    return client


def env(*keys: str) -> dict[str, str]:
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env: {', '.join(missing)}")
    return {k: os.environ[k].strip() for k in keys}


def load_done(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {ln.strip() for ln in path.read_text().splitlines() if ln.strip()}


def mark_done(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(key + "\n")


def sheet_id(link: str) -> str:
    m = SHEET_RE.search(link.strip())
    if not m:
        raise ValueError(f"bad excel_link: {link!r}")
    return m.group(1)


def attr_val(attrs, spec_id: int) -> str | None:
    for attr in attrs or []:
        attr_dict = attr.to_dict() if hasattr(attr, "to_dict") else dict(attr)
        if int(attr_dict.get("spec_id", -1)) == spec_id:
            value = attr_dict.get("value")
            return None if value is None else str(value)
    return None


def table_spec(client, project_id: int) -> tuple[int, int]:
    """Project-level label map: table label_id + excel_link attr spec_id."""
    labels = get_paginated_collection(
        client.api_client.labels_api.list_endpoint, project_id=project_id
    )
    for label in labels:
        label_dict = label.to_dict() if hasattr(label, "to_dict") else dict(label)
        if str(label_dict.get("name", "")).lower() != LABEL:
            continue
        for attr in label_dict.get("attributes") or []:
            attr_dict = (
                attr
                if isinstance(attr, dict)
                else (attr.to_dict() if hasattr(attr, "to_dict") else dict(attr))
            )
            if str(attr_dict.get("name", "")).lower() == ATTR:
                return int(label_dict["id"]), int(attr_dict["id"])
        sys.exit(f"project {project_id}: label '{LABEL}' has no '{ATTR}'")
    sys.exit(f"project {project_id}: no label '{LABEL}'")


def job_frames(job) -> list[int]:
    meta = job.get_meta()
    if meta.included_frames:
        return list(meta.included_frames)
    return list(range(int(meta.start_frame), int(meta.stop_frame) + 1))


def table_object_frame_id_mapping(
    anns, label_id: int, spec_id: int
) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for shape in anns.shapes or []:
        shape_dict = shape.to_dict() if hasattr(shape, "to_dict") else dict(shape)
        if int(shape_dict.get("label_id", -1)) != label_id:
            continue
        points = shape_dict.get("points") or []
        link = attr_val(shape_dict.get("attributes") or [], spec_id)
        if not link or len(points) < 4:
            continue
        try:
            sid = sheet_id(link)
        except ValueError:
            continue
        out.setdefault(int(shape_dict["frame"]), []).append(
            {
                "id": int(shape_dict.get("id", -1)),
                "type": str(shape_dict.get("type", "shape")),
                "points": [float(p) for p in points],
                "link": link,
                "sheet_id": sid,
            }
        )
    return out


def crop_to_png_bytes(frame_image: Image.Image, points: list[float]) -> bytes:
    """Crop table bbox from an in-memory frame; return PNG bytes (no disk)."""
    if len(points) == 4:
        xs, ys = [points[0], points[2]], [points[1], points[3]]
    else:
        xs, ys = points[0::2], points[1::2]
    w, h = frame_image.size
    left = max(0, int(min(xs)))
    top = max(0, int(min(ys)))
    right = min(w, int(max(xs)))
    bottom = min(h, int(max(ys)))
    crop = frame_image.crop((left, top, max(left + 1, right), max(top + 1, bottom)))
    buf = BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def set_org_from_project(client: Client, project_id: int) -> None:
    """Org projects need an org context; personal-workspace list filters return 0 tasks."""
    project = client.projects.retrieve(project_id)
    org_id = getattr(project, "organization", None)
    if org_id is None:
        return
    for org in get_paginated_collection(
        client.api_client.organizations_api.list_endpoint
    ):
        if int(getattr(org, "id", -1)) == int(org_id):
            client.organization_slug = str(getattr(org, "slug"))
            print(f"organization={client.organization_slug} (id={org_id})", flush=True)
            return


def resolve_tasks(client, project_id: int, args) -> list[int]:
    # Use project.get_tasks() so org_id is passed (bare /api/tasks?project_id= hides org tasks).
    project = client.projects.retrieve(project_id)
    all_ids = sorted(int(t.id) for t in project.get_tasks())
    known = set(all_ids)
    if args.all_tasks:
        return all_ids
    if args.task_range:
        lo, hi = args.task_range
        return [i for i in range(lo, hi + 1) if i in known]
    unknown = [i for i in args.task_ids if i not in known]
    if unknown:
        sys.exit(f"task ids not in project: {unknown}")
    return sorted(set(args.task_ids))


def process_frame(
    job,
    project_id: int,
    tid: int,
    jid: int,
    frame: int,
    table_objs: list[dict],
    creds: Path,
    log_path: Path,
) -> bool:
    """Fetch frame once, crop in RAM, QC, then drop all image buffers."""
    key = f"{project_id}:{tid}:{jid}:{frame}"
    try:
        with _cvat_lock:
            frame_stream = job.get_frame(frame, quality="original")
        with Image.open(frame_stream) as frame_image:
            # Load into memory so the CVAT stream can be closed immediately.
            frame_image.load()
            for table_obj in table_objs:
                crop_bytes = crop_to_png_bytes(frame_image, table_obj["points"])
                run_table_qc(
                    table_obj["sheet_id"],
                    crop_bytes,
                    creds,
                    task_id=tid,
                    job_id=jid,
                    frame=frame,
                    object_id=table_obj["id"],
                )
                del crop_bytes
        with _log_lock:
            mark_done(log_path, key)
        return True
    except Exception as exc:
        print(
            f"ERROR  task={tid} job={jid} frame={frame}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return False


async def run_frames(
    work: list[tuple], concurrency: int = CONCURRENCY
) -> tuple[int, int]:
    sem = asyncio.Semaphore(concurrency)

    async def one(item: tuple) -> bool:
        async with sem:
            return await asyncio.to_thread(process_frame, *item)

    results = await asyncio.gather(*(one(item) for item in work))
    ok = sum(1 for r in results if r)
    return ok, len(results) - ok


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--project-id", type=int, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--all-tasks", action="store_true")
    g.add_argument("--task-range", nargs=2, type=int, metavar=("LO", "HI"))
    g.add_argument("--task-ids", nargs="+", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--batch",
        action="store_true",
        help="Use Mistral Batch API (JSONL upload) instead of sync OCR. "
        "50 %% cheaper; processes frames in chunks of 4.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=CONCURRENCY,
        help="frames in flight (default 5)",
    )
    p.add_argument("--log-path", type=Path, default=LOG)
    p.add_argument(
        "--jobs-file",
        type=Path,
        default=JOBS_FILE,
        help="text file of job IDs to process (default: jobs.txt)",
    )
    p.add_argument(
        "--filter-jobs",
        action="store_true",
        help="Filter jobs based on the jobs file (default: False)",
    )
    args = p.parse_args()

    e = env("CVAT_URL", "CVAT_TOKEN")
    if not args.dry_run:
        env("MISTRAL_API_KEY")

    allowed_jobs = load_job_ids(args.jobs_file) if args.filter_jobs else None
    done = load_done(args.log_path)
    creds = default_credentials_path()
    work: list[tuple] = []
    dry_n = 0

    with make_cvat_client(e["CVAT_URL"], e["CVAT_TOKEN"]) as client:
        set_org_from_project(client, args.project_id)
        tasks = resolve_tasks(client, args.project_id, args)
        lid, sid = table_spec(client, args.project_id)
        print(
            f"project id ={args.project_id}  no of tasks={len(tasks)}  "
            f"jobs_file={args.jobs_file if args.filter_jobs else 'None'}  "
            f"allowed_jobs={len(allowed_jobs) if allowed_jobs is not None else 'All'}  "
            f"label_id={lid}  excel_link spec={sid}",
            flush=True,
        )

        table_count = 0
        matched_jobs = 0
        for tid in tasks:
            task = client.tasks.retrieve(tid)
            for job in sorted(task.get_jobs(), key=lambda j: j.id):
                jid = int(job.id)
                if allowed_jobs is not None and jid not in allowed_jobs:
                    continue
                matched_jobs += 1
                tables_in_frame = table_object_frame_id_mapping(
                    job.get_annotations(), lid, sid
                )
                frames = [f for f in sorted(job_frames(job)) if f in tables_in_frame]
                pending = [
                    f
                    for f in frames
                    if f"{args.project_id}:{tid}:{jid}:{f}" not in done
                ]

                for frame in pending:
                    table_objs = tables_in_frame[frame]
                    if args.dry_run:
                        dry_n += len(table_objs)
                        continue
                    table_count += len(table_objs)
                    if args.batch:
                        # For batch mode: collect (job, metadata) tuples;
                        # images are fetched later just before submission.
                        for table_obj in table_objs:
                            work.append(
                                {
                                    "_job": job,
                                    "project_id": args.project_id,
                                    "task_id": tid,
                                    "job_id": jid,
                                    "frame": frame,
                                    "table_obj": table_obj,
                                    "log_path": args.log_path,
                                }
                            )
                    else:
                        work.append(
                            (
                                job,
                                args.project_id,
                                tid,
                                jid,
                                frame,
                                table_objs,
                                creds,
                                args.log_path,
                            )
                        )

        if allowed_jobs is not None:
            print(f"matched_jobs={matched_jobs} / {len(allowed_jobs)} in jobs file", flush=True)
        else:
            print(f"processed_jobs={matched_jobs} (no jobs file filter)", flush=True)

        if args.dry_run:
            print(f"dry-run  pending_tables={dry_n}", flush=True)
            return 0

        if not work:
            print(f"nothing pending  log={args.log_path}", flush=True)
            return 0

        if args.batch:
            # ---- Batch mode: process in chunks of BATCH_SIZE ----
            # We download + crop only BATCH_SIZE frames at a time so that
            # at most BATCH_SIZE crop images live in RAM simultaneously.
            # Each chunk is uploaded, batch-submitted, polled, and QC'd
            # before the next chunk is even downloaded.
            n_chunks = (len(work) + BATCH_SIZE - 1) // BATCH_SIZE
            print(
                f"queued={len(work)} tables (batch mode)  "
                f"chunks={n_chunks}  chunk_size={BATCH_SIZE}",
                flush=True,
            )
            ok = failed = 0
            for chunk_idx in range(0, len(work), BATCH_SIZE):
                chunk = work[chunk_idx : chunk_idx + BATCH_SIZE]
                chunk_label = f"{chunk_idx // BATCH_SIZE + 1}/{n_chunks}"
                print(
                    f"  downloading chunk {chunk_label}  ({len(chunk)} tables)",
                    flush=True,
                )

                # --- Download + crop only this chunk's images ---
                batch_items: list[dict] = []
                for w in chunk:
                    job_obj = w["_job"]
                    frame = w["frame"]
                    table_obj = w["table_obj"]
                    with _cvat_lock:
                        frame_stream = job_obj.get_frame(frame, quality="original")
                    with Image.open(frame_stream) as frame_image:
                        frame_image.load()
                        crop_bytes = crop_to_png_bytes(
                            frame_image, table_obj["points"]
                        )
                    cid = (
                        f"{w['project_id']}:{w['task_id']}:{w['job_id']}"
                        f":{frame}:{table_obj['id']}"
                    )
                    batch_items.append(
                        {
                            "custom_id": cid,
                            "image": crop_bytes,  # only BATCH_SIZE of these exist
                            "sheet_id": table_obj["sheet_id"],
                            "task_id": w["task_id"],
                            "job_id": w["job_id"],
                            "frame": frame,
                            "object_id": table_obj["id"],
                        }
                    )
                    del crop_bytes  # variable ref gone; list holds the only ref

                # --- Submit this chunk as a batch job and wait for results ---
                chunk_results = run_table_qc_batch(batch_items)
                del batch_items  # all crop bytes freed here

                # --- Tally + mark done ---
                for r in chunk_results:
                    if r.get("status") == "success":
                        ok += 1
                        parts = r["custom_id"].split(":")
                        # custom_id = project:task:job:frame:obj
                        key = ":".join(parts[:4])  # project:task:job:frame
                        with _log_lock:
                            mark_done(args.log_path, key)
                    else:
                        failed += 1

            print(
                f"finished (batch)  ok={ok}  failed={failed}  "
                f"log={args.log_path}",
                flush=True,
            )
        else:
            # ---- Sync Mistral mode (original) ----
            print(
                f"queued={len(work)} frames / {table_count} tables  "
                f"concurrency={args.concurrency}",
                flush=True,
            )
            ok, failed = asyncio.run(
                run_frames(work, concurrency=args.concurrency)
            )
            print(
                f"finished  ok={ok}  failed={failed}  log={args.log_path}",
                flush=True,
            )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
