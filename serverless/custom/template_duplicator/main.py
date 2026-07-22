import json
import re
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build

SERVICE_ACCOUNT_FILE = '/opt/nuclio/creds/cvat-sheets-integration.json'
SCOPES = ['https://www.googleapis.com/auth/drive']

def extract_google_id(url):
    """
    Extracts the file/folder ID from a
    standard Google URL.
    """
    match = re.search(r"/d/([a-zA-Z0-9-_]+)", url)
    if match:
         return match.group(1)
    match = re.search(r"folders/([a-zA-Z0-9-_]+)", url)
    if match:
        return match.group(1)
    return url

def init_context(context):
    """
    Runs once when the container starts.
    Initializes the Google Drive API client so it can be reused across requests.
    """
    context.logger.info("Initializing Google Drive service...")

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        error_msg = f"Credentials file not found at {SERVICE_ACCOUNT_FILE}"
        context.logger.error(error_msg)
        raise FileNotFoundError(error_msg)

def handler(context, event):
    """
    Triggered every time CVAT sends an HTTP request to duplicate the sheet.
    """
    try:

        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        # Store the authenticated service in context.user_data for reuse
        drive = build('drive', 'v3', credentials=creds)
        context.drive = drive
        context.logger.info(" Google Drive service initialized successfully.")

        # 1. Parse the incoming JSON payload from the CVAT frontend
        body = event.body

        template_url = body['x-data']['template_url']
        folder_url = body['x-data']['folder_url']
        new_file_name = body['x-data']['new_file_name']

        if not template_url or not folder_url:
            return context.Response(
                body=json.dumps({"error": "Missing template_url or folder_url in request"}),
                headers={"Content-Type": "application/json"},
                status_code=400
            )

        # 2. Extract IDs and retrieve the pre-authenticated service
        template_id = extract_google_id(template_url)
        folder_id = extract_google_id(folder_url)
        drive = context.drive

        context.logger.info(f"{template_id} and {folder_id} and {new_file_name}")

        file_metadata = {
            'name': new_file_name,
            'parents': [folder_id]
        }

        copied_file = drive.files().copy(
            fileId=template_id,
            body=file_metadata,
            fields='webViewLink',
	        supportsAllDrives=True,


        ).execute()

        new_file_link = copied_file.get('webViewLink')

        context.logger.info(f" Successfully generated new sheet: {new_file_link}")

        # 5. Return success payload
        return context.Response(
            body=json.dumps({
                "status": "success",
                "url": new_file_link
            }),
            headers={"Content-Type": "application/json"},
            status_code=200
        )

    except Exception as e:
        context.logger.error(f" Error during duplication: {str(e)}")
        return context.Response(
            body=json.dumps({
                "status": "error",
                "message": str(e)
            }),
            headers={"Content-Type": "application/json"},
            status_code=500
        )
