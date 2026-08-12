# AWS Table Annotation Backfill Script

This script (`table_populate.py`) processes table annotations in CVAT projects, creates/updates table polygons, and populates Google Sheets from Textract JSON files.

## Setup Requirements

Before running the script, make sure to set up the environment and credentials:

1. **Environment Variables (`.env`)**:
   - Refer to [.env.example](#)
   - Create the `.env` file in the directory where `main.py` is located
     ```env
     CVAT_URL="https://cvat.quantrium.com"
     CVAT_TOKEN="your_cvat_personal_access_token"
     TEMPLATE_SHEET_ID="your_google_sheet_template_id"
     TEXTRACT_JSON_FOLDER_ID="your_google_drive_folder_id_for_textract_json"
     OUTPUT_SHEETS_FOLDER_ID="your_google_drive_folder_id_for_outputs"
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

The script is executed using `python table_populate.py`. Here are all the possibilities of calling the script with its arguments:

### 1. Basic Execution (Required Arguments)
Process tasks in the range `[LOW, HIGH]` (inclusive) for a specific project:
```bash
python table_populate.py --project-id <PROJECT_ID> --low <LOW_TASK_ID> --high <HIGH_TASK_ID>
```
*Example:*
```bash
python table_populate.py --project-id 2 --low 10 --high 15
```

---

## Progress Tracking & Resuming
- The script logs its progress in a file called `progress.txt` next to the script in the format `projectid:taskid:jobid:frameid`.
- On rerun, the script automatically reads `progress.txt` and skips frames that have already been processed, providing resume support after crashes or manual interrupts (Ctrl+C).
