// Called from Code.gs onOpen — do not name this onOpen (simple-trigger clash).
function addMergeToolsMenu_() {
  SpreadsheetApp.getUi()
    .createMenu('Merge Tools')
    .addItem('Run Merge Process', 'mergeFrameFiles')
    .addToUi();
}

function mergeFrameFiles() {
  const activeSheet = SpreadsheetApp.getActiveSpreadsheet();
  const currentFile = DriveApp.getFileById(activeSheet.getId());

  // 1. Get the current sub-directory
  const parents = currentFile.getParents();
  if (!parents.hasNext()) {
    SpreadsheetApp.getUi().alert("Could not locate the parent folder.");
    return;
  }
  const folder = parents.next();

  // 2. Fetch all Google Sheets in this folder
  const files = folder.getFilesByType(MimeType.GOOGLE_SHEETS);
  const tableFiles = [];
  const blockFiles = [];

  let projName = "";
  let taskId = "";

  // Regex: projectname_taskno_jobno_objectname{no}_frame{no}
  const regex = /^(.*?)_(\d+)_\d+_([a-zA-Z]+)\d*_frame(\d+)/i;

  while (files.hasNext()) {
    const file = files.next();
    const fileName = file.getName();
    const match = fileName.match(regex);

    if (match) {
      if (!projName) {
        projName = match[1];
        taskId = match[2];
      }

      const objType = match[3].toLowerCase();
      const fileData = {
        file: file,
        frameNo: parseInt(match[4], 10)
      };

      if (objType === 'table') {
        tableFiles.push(fileData);
      } else if (objType === 'block') {
        blockFiles.push(fileData);
      }
    }
  }

  if (tableFiles.length === 0 && blockFiles.length === 0) {
    SpreadsheetApp.getUi().alert("No valid 'Table' or 'Block' frame files found in this directory.");
    return;
  }

  // 3. Sort files numerically by frame number
  tableFiles.sort((a, b) => a.frameNo - b.frameNo);
  blockFiles.sort((a, b) => a.frameNo - b.frameNo);

  // 4. Check if the consolidated file already exists
  const targetFileName = `${projName}_${taskId}`;
  const existingFiles = folder.getFilesByName(targetFileName);
  let targetSpreadsheet;

  if (existingFiles.hasNext()) {
    targetSpreadsheet = SpreadsheetApp.openById(existingFiles.next().getId());
  } else {
    targetSpreadsheet = SpreadsheetApp.create(targetFileName);
    const newDriveFile = DriveApp.getFileById(targetSpreadsheet.getId());
    newDriveFile.moveTo(folder);
  }

  // 5. Process the tabs
  processTab(targetSpreadsheet, "Table Data", tableFiles);
  processTab(targetSpreadsheet, "Block Data", blockFiles);

  // 6. Cleanup default "Sheet1" if a new spreadsheet was created
  const defaultSheet = targetSpreadsheet.getSheetByName("Sheet1");
  if (defaultSheet && targetSpreadsheet.getSheets().length > 1) {
    targetSpreadsheet.deleteSheet(defaultSheet);
  }

  SpreadsheetApp.getUi().alert(`Success! Merged ${tableFiles.length} tables and ${blockFiles.length} blocks into "${targetFileName}".`);
}

// Helper function to process and merge data targeting SheetId 0
function processTab(spreadsheet, tabName, fileArray) {
  if (fileArray.length === 0) return;

  let sheet = spreadsheet.getSheetByName(tabName);

  if (!sheet) {
    sheet = spreadsheet.insertSheet(tabName);
  } else {
    sheet.clear();
  }

  let currentRow = 1;

  for (let i = 0; i < fileArray.length; i++) {
    const sourceFile = SpreadsheetApp.openById(fileArray[i].file.getId());
    const sheets = sourceFile.getSheets();

    // Explicitly target the tab with sheetId === 0 (fallback to index 0 if 0 doesn't exist)
    const targetSourceSheet = sheets.find(s => s.getSheetId() === 0) || sheets[0];
    const sourceData = targetSourceSheet.getDataRange().getValues();

    if (sourceData.length > 0 && sourceData[0].length > 0) {
      sheet.getRange(currentRow, 1, sourceData.length, sourceData[0].length).setValues(sourceData);
      currentRow += sourceData.length;

      // Add a 1-line empty gap between file contents
      if (i < fileArray.length - 1) {
        currentRow += 1;
      }
    }
  }
}