# CVAT Table Quality Control (QC) Backfill Script

This script (`main.py`) performs a one-off CVAT table QC backfill. It reads spreadsheet tables, runs Mistral OCR on the table image crops, updates the Google Sheet with QC highlights, and creates a `QC Diff` sheet.

## Setup Requirements

Before running the script, make sure to set up the environment and credentials:

1. **Environment Variables (`.env`)**:
   - Refer to [.env.example]
   - Create a new `.env` file containing:
     ```env
     CVAT_URL="https://cvat.yourdomain.com"
     CVAT_TOKEN="your_cvat_personal_access_token"
     MISTRAL_API_KEY="your_mistral_api_key"
     ```

2. **GCP Service Account Key (`cvat-sheets-integration.json`)**:
   - The script expects this file to be named exactly **`cvat-sheets-integration.json`** and to sit next to `table_populate.py`.

3. **Install Dependencies**:
   ```bash
   uv init my_new_project
   cd my_new_project
   uv add cvat-sdk httpx google-auth tenacity python-dotenv pillow
   ```

---

## Usage Guide & Calling Possibilities

The script is executed using `python main.py`. It requires `--project-id` and exactly one of the task selection arguments (`--all-tasks`, `--task-range`, or `--task-ids`).

### 1. Task Selection Options (Mutually Exclusive, One Required)

- **Process All Tasks in Project:**
  ```bash
  python main.py --project-id <PROJECT_ID> --all-tasks
  ```

- **Process a Range of Tasks (Inclusive bounds):**
  ```bash
  python main.py --project-id <PROJECT_ID> --task-range <LO_TASK_ID> <HI_TASK_ID>
  ```

- **Process Specific Task IDs:**
  ```bash
  python main.py --project-id <PROJECT_ID> --task-ids <ID1> <ID2> <ID3>
  ```

### 2. QC Mode Options
- **DEFAULT: Non batch api calls** - It uses synchronous calls to the Mistral API.

- **Mistral Batch API Mode (Not Recommended right now : not tested):**
  Use the `--batch` flag to use the Mistral Batch API (JSONL upload) instead of synchronous OCR calls. This method is 50% cheaper and processes frames in chunks of 4.
  ```bash
  python main.py --project-id <PROJECT_ID> --all-tasks --batch
  ```

- **Dry Run:**
  Use the `--dry-run` flag to count and display the pending tables/frames to process without executing QC API calls or modifying Google Sheets.
  ```bash
  python main.py --project-id <PROJECT_ID> --all-tasks --dry-run
  ```

### 3. Execution Control & Filtering Options

- **Filter by Specific Job IDs:**
  Use `--filter-jobs` to restrict processing to the list of Job IDs specified in a text file (one ID per line).
  ```bash
  # Uses default jobs file "jobs.txt" in the current directory:
  python main.py --project-id <PROJECT_ID> --all-tasks --filter-jobs

  Example:
  job.txt
  4251
  1341
  5452
  ```
## Progress Tracking & Resuming
- The script logs its progress in a file called ` qc_progress.txt` next to the script in the format `projectid:taskid:jobid:frameid`.
- On rerun, the script automatically reads `qc_progress.txt` and skips frames that have already been processed, providing resume support after crashes or manual interrupts (Ctrl+C).
