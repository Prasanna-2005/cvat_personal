function clearActivityForTest() {
  var props = PropertiesService.getDocumentProperties();

  props.deleteProperty(PROP.LAST_ACTIVITY);
  props.deleteProperty(PROP.QUEUE);
  props.deleteProperty(PROP.LAST_SENT);
  props.deleteProperty(PROP.FLUSH_ARMED_ID);

  Logger.log('Activity state cleared');
}

var INACTIVITY_TIMEOUT_MS = 10 * 60 * 1000;
var ACTIVITY_INTERVAL_MS = 60 * 1000;
var QUEUE_MAX = 50;

var PROP = {
  QUEUE: 'cvat_event_queue',
  LAST_SENT: 'cvat_last_sent_event',
  LAST_ACTIVITY: 'cvat_last_activity_ms',
  IDS: 'cvat_parsed_ids',
  USER_EMAIL: 'cvat_annotator_email',

  FLUSH_ARMED_ID: 'cvat_flush_armed_ssid',
};

function setupCvatConfig_() {
  var props = PropertiesService.getScriptProperties();

  if (!props.getProperty('CVAT_BASE_URL')) {
    props.setProperty('CVAT_BASE_URL', 'https://cvat.quantrium.ai');
  }

  if (!props.getProperty('CVAT_PAT')) {
    props.setProperty('CVAT_PAT', '---);
  }

  if (!props.getProperty('ORG_SLUG')) {
    props.setProperty('ORG_SLUG', 'Quantrium');
  }
}

// ---- simple triggers (survive Make a copy; enqueue only) ----


function onOpen(e) {
  // Shared project: merge.gs menu (cannot define a second onOpen).
  try {
    addMergeToolsMenu_();
  } catch (err) {
    logWarn_('addMergeToolsMenu_ failed:', err);
  }

  log_('open (simple)');
  if (!isActivated_()) {
    showSetupTabOnly_();
    return;
  }
  enqueueActivity_({ force: true, reason: 'open' });
}

function onEdit(e) {
  if (!isActivated_()) {
    // Idle deactivation clears FLUSH_ARMED_ID while the file may stay open;
    // gate again so the annotator can re-enable logging.
    showSetupTabOnly_();
    return;
  }

  enqueueActivity_({
    force: false,
    reason: 'edit'
  });
}

function onSelectionChange(e) {
  if (!isActivated_()) {
    showSetupTabOnly_();
    return;
  }

  enqueueActivity_({
    force: false,
    reason: 'selection'
  });
}

/** Installable time-driven flush (does NOT survive Make a copy). */
function flushEventsToCvat() {
  return flushQueue_();
}

/**
 * Create/repair the installable flush trigger. Safe to re-run.
 * Called from the enable dialog (google.script.run) or the Apps Script editor.
 * @returns {{ok:boolean, detail:string}}
 */
function bindTriggersOnce() {
  // Script Properties do NOT survive Drive "Make a copy" — seed defaults
  // from code on first activate of each duplicated spreadsheet project.
  setupCvatConfig_();

  var cfg = getConfig_();
  if (!cfg.baseUrl || !cfg.pat) {
    var miss =
  'Missing CVAT_BASE_URL / CVAT_PAT in Script Properties.';
    logError_('bindTriggersOnce:', miss);
    return { ok: false, detail: miss };
  }

  var hasFlush = false;
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'flushEventsToCvat') hasFlush = true;
  });

  if (hasFlush) {
    log_('trigger ok: flushEventsToCvat');
  } else {
    ScriptApp.newTrigger('flushEventsToCvat').timeBased().everyMinutes(1).create();
    log_('trigger created: flushEventsToCvat (every 1 min)');
  }

  markFlushArmed_();
  // Reset inactivity clock so the next flush does not immediately tear down
  // after a return-from-idle re-activation.
  touchActivity_(PropertiesService.getDocumentProperties(), Date.now());
  log_('bindTriggersOnce done; baseUrl=', cfg.baseUrl);
  return { ok: true, detail: 'Automatic CVAT upload enabled (flush every 1 min)' };
}

function deleteFlushTrigger() {
  var triggers = ScriptApp.getProjectTriggers();

  triggers.forEach(function (trigger) {
    if (trigger.getHandlerFunction() === 'flushEventsToCvat') {
      ScriptApp.deleteTrigger(trigger);
      log_('deleted trigger: flushEventsToCvat');
    }
  });
}

function activateAndRevealSheets() {
  var ui = SpreadsheetApp.getUi();

  var answer = ui.alert(
    'CVAT time logging',
    'Start automatic CVAT time logging?',
    ui.ButtonSet.YES_NO
  );

  if (answer !== ui.Button.YES) {
    return;
  }

  var result = bindTriggersOnce();

  if (!result.ok) {
    ui.alert(
      'CVAT Logging Failed',
      result.detail || 'Unknown error',
      ui.ButtonSet.OK
    );
    return;
  }

  var ss = SpreadsheetApp.getActiveSpreadsheet();

  // Keep Setup last so CVAT/backends that use sheets[0] never hit it.
  ensureSetupLast_();

  // Reveal annotation/work sheets.
  ss.getSheets().forEach(function(sheet) {
    if (sheet.getName() !== 'Setup') {
      sheet.showSheet();
    }
  });

  // Hide Setup, but DO NOT delete it.
  var setupSheet = ss.getSheetByName('Setup');
  if (setupSheet) {
    setupSheet.hideSheet();
  }

  // Select first work sheet (now always sheets[0] if Setup is last).
  for (var i = 0; i < ss.getSheets().length; i++) {
    var sheet = ss.getSheets()[i];

    if (sheet.getName() !== 'Setup') {
      ss.setActiveSheet(sheet);
      break;
    }
  }

  ui.alert(
    'CVAT Logging Enabled',
    'Automatic logging is now active.',
    ui.ButtonSet.OK
  );
}

function clearFlushArmedId() {
  PropertiesService
    .getDocumentProperties()
    .deleteProperty(PROP.FLUSH_ARMED_ID);

  Logger.log('FLUSH_ARMED_ID cleared');
}

/**
 * Checks if the flush trigger is activated and armed for the current spreadsheet.
 * Purely property-based, avoids ScriptApp calls to prevent auth errors in simple triggers.
 * @returns {boolean}
 */
function isActivated_() {
  var ssId = SpreadsheetApp.getActiveSpreadsheet().getId();
  var props = PropertiesService.getDocumentProperties();
  return props.getProperty(PROP.FLUSH_ARMED_ID) === ssId;
}

function isInactive_(props, now) {
  var lastActivity = Number(
    props.getProperty(PROP.LAST_ACTIVITY) || 0
  );

  if (!lastActivity) {
    return false;
  }

  return now - lastActivity >= INACTIVITY_TIMEOUT_MS;
}

function markFlushArmed_() {
  var ssId = SpreadsheetApp.getActiveSpreadsheet().getId();
  PropertiesService.getDocumentProperties().setProperty(PROP.FLUSH_ARMED_ID, ssId);
}

/**
 * Move Setup to the last tab index. Downstream CVAT logic uses sheets[0]
 * (API order), so Setup must never sit at the front — even when hidden.
 * @returns {GoogleAppsScript.Spreadsheet.Sheet|null}
 */
function ensureSetupLast_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var setup = ss.getSheetByName('Setup');
  if (!setup) return null;

  var lastPos = ss.getNumSheets(); // 1-based
  if (setup.getIndex() !== lastPos) {
    var prev = ss.getActiveSheet();
    ss.setActiveSheet(setup);
    ss.moveActiveSheet(lastPos);
    if (prev && prev.getName() !== 'Setup') {
      ss.setActiveSheet(prev);
    }
  }
  return setup;
}

/**
 * Ensures the 'Setup' tab is visible and hides all other sheets to enforce
 * the CVAT logging activation gate. Setup stays last so sheets[0] remains a
 * work sheet for CVAT even while only Setup is visible to the annotator.
 */
function showSetupTabOnly_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var setupSheet = ensureSetupLast_();
  if (!setupSheet) return;

  setupSheet.showSheet();

  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var s = sheets[i];
    if (s.getName() !== 'Setup') {
      s.hideSheet();
    }
  }

  ss.setActiveSheet(setupSheet);
}

/**
 * Administrative function to configure/initialize the Setup Tab Gate in a spreadsheet template.
 * Automatically creates the 'Setup' tab, populates instructions, and hides the annotation sheets.
 */
function setupTemplate() {

  setupCvatConfig_();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var ui = SpreadsheetApp.getUi();

  // 1. Ensure Setup sheet exists at the END (never sheets[0] for CVAT).
  var setupSheet = ss.getSheetByName('Setup');
  if (!setupSheet) {
    setupSheet = ss.insertSheet('Setup'); // appends last
  }
  ensureSetupLast_();
  setupSheet = ss.getSheetByName('Setup');

  // 2. Add styled instructions
  setupSheet.clear();

  setupSheet.getRange('A2').setValue('CVAT TIME LOGGER SETUP').setFontWeight('bold').setFontSize(14);
  setupSheet.getRange('A4').setValue('Instructions for Annotators:').setFontWeight('bold');
  setupSheet.getRange('A5').setValue('1. Click the button below to activate logging.');
  setupSheet.getRange('A6').setValue('2. You will be prompted to authorize the script. Please approve all steps.');
  setupSheet.getRange('A7').setValue('3. Once authorized, the annotation sheets will automatically be revealed.');

  setupSheet.autoResizeColumns(1, 1);

  // 3. Gate: only Setup visible; work sheets stay at lower indexes for CVAT.
  showSetupTabOnly_();

  ui.alert(
    'Template Initialized',
    'Setup sheet created/updated. Now, please:\n' +
    '1. Insert a Drawing Button (Insert -> Drawing) on the Setup sheet.\n' +
    '2. Assign the script "activateAndRevealSheets" (no quotes) to the drawing.\n' +
    '3. Save and share this spreadsheet as the template.',
    ui.ButtonSet.OK
  );
}


/**
 * @param {{force:boolean, reason:string}} opts
 * @returns {boolean}
 */
function enqueueActivity_(opts) {
  var force = !!(opts && opts.force);
  var reason = (opts && opts.reason) || 'activity';

  var cfg = getConfig_();
  if (!cfg.baseUrl || !cfg.pat) {
    logError_('enqueue skip: missing CVAT_BASE_URL');
    return false;
  }

  var ids = getSheetIds_();
  if (!ids) {
    logWarn_('enqueue skip: title not parseable:', sheetTitle_());
    return false;
  }

  var username = resolveAnnotatorUsername_();
  if (!username) {
    logWarn_('enqueue skip: no annotator email yet');
    return false;
  }

  var props = PropertiesService.getDocumentProperties();
  var now = Date.now();
  if (!force && isThrottled_(props, now)) {
    return false;
  }
  touchActivity_(props, now);

  var event = buildEvent_(ids, username, new Date().toISOString(), reason);
  var queue = readJsonProp_(props, PROP.QUEUE, []);
  queue.push(event);
  if (queue.length > QUEUE_MAX) queue = queue.slice(-QUEUE_MAX);
  props.setProperty(PROP.QUEUE, JSON.stringify(queue));

  log_(
    'enqueue',
    reason,
    'user=',
    username,
    'job=',
    ids.jobId,
    'frame=',
    ids.frame,
    'queue=',
    queue.length
  );
  return true;
}

function isThrottled_(props, now) {
  var last = Number(
    props.getProperty(PROP.LAST_ACTIVITY) || 0
  );

  return now - last < ACTIVITY_INTERVAL_MS;
}

function touchActivity_(props, now) {
  props.setProperty(PROP.LAST_ACTIVITY, String(now));
}


function buildEvent_(ids, username, isoTimestamp, reason) {
  var payload = {
    client_id: 'sheets-' + SpreadsheetApp.getActiveSpreadsheet().getId(),
    is_active: true,
    frame: ids.frame,
  };
  if (reason) payload.reason = reason;

  return {
    scope: 'user:activity',
    source: 'google_sheets',
    timestamp: isoTimestamp,
    duration: 0,
    job_id: ids.jobId,
    task_id: ids.taskId,
    user_name: username,
    payload: JSON.stringify(payload),
  };
}

function flushQueue_() {
  var props = PropertiesService.getDocumentProperties();
  var ids = getSheetIds_();

  if (!ids) {
    return fail_(
      'flush skip: title not parseable: ' + sheetTitle_()
    );
  }

  var queue = readJsonProp_(props, PROP.QUEUE, []);
  var previous = readJsonProp_(props, PROP.LAST_SENT, null);

  // Check inactivity BEFORE doing anything else.
  if (isInactive_(props, Date.now())) {
    log_('flush: inactive for 10 minutes');

    // There may still be events waiting in the queue.
    // Send them before shutting down the trigger.
    if (queue.length) {
      log_(
        'flush: posting final',
        queue.length,
        'events before deactivation'
      );

      var result = postEvents_(queue, previous);

      if (!result.ok) {
        // IMPORTANT:
        // Do not delete the trigger if the final POST failed.
        return result;
      }

      props.setProperty(
        PROP.LAST_SENT,
        JSON.stringify(queue[queue.length - 1])
      );

      props.setProperty(PROP.QUEUE, '[]');
    }

    // No more activity for 10 minutes and
    // everything pending has been successfully sent.
    deleteFlushTrigger();

    props.deleteProperty(PROP.FLUSH_ARMED_ID);
    props.deleteProperty(PROP.LAST_ACTIVITY);

    // File may still be open in the browser — bring Setup back so the
    // annotator must re-enable before continuing work.
    showSetupTabOnly_();

    log_('flush: deactivated after 10 minutes of inactivity');

    return {
      ok: true,
      detail: 'Inactive for 10 minutes; flush trigger removed'
    };
  }

  // Normal active operation.
  if (!queue.length) {
    log_('flush: queue empty');
    return {
      ok: true,
      detail: 'queue empty'
    };
  }

  log_(
    'flush: posting',
    queue.length,
    'events; has_previous=',
    !!previous,
    'job=',
    ids.jobId,
    'frame=',
    ids.frame
  );

  var result = postEvents_(queue, previous);

  if (!result.ok) {
    return result;
  }

  props.setProperty(
    PROP.LAST_SENT,
    JSON.stringify(queue[queue.length - 1])
  );

  props.setProperty(PROP.QUEUE, '[]');

  log_('flush: OK', result.detail);

  return result;
}

/**
 * @param {Object[]} events
 * @param {Object|null} previous
 */
function postEvents_(events, previous) {
  var cfg = getConfig_();
  if (!cfg.baseUrl || !cfg.pat) return fail_('Missing CVAT_BASE_URL / CVAT_PAT');

  var body = {
    events: events,
    timestamp: new Date().toISOString(),
  };
  if (previous) body.previous_event = previous;

  var url = cfg.baseUrl + '/api/events';
  if (cfg.orgSlug) url += '?org=' + encodeURIComponent(cfg.orgSlug);

  var resp = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(body),
    headers: {
      Authorization: 'Bearer ' + cfg.pat,
      'ngrok-skip-browser-warning': '1',
    },
    muteHttpExceptions: true,
  });

  var code = resp.getResponseCode();
  var text = resp.getContentText() || '';
  if (code < 200 || code >= 300) {
    return fail_('HTTP ' + code + ' ' + text.slice(0, 400), code);
  }
  if (text && text.charAt(0) === '<') {
    return fail_('Non-JSON body (ngrok HTML?). Check base URL / skip header.', code);
  }

  return { ok: true, code: code, detail: 'Posted ' + events.length + ' events (HTTP ' + code + ')' };
}

function resolveAnnotatorUsername_() {
  var email = '';
  try {
    email = Session.getActiveUser().getEmail() || '';
  } catch (e) {}
  if (!email) {
    try {
      email = Session.getEffectiveUser().getEmail() || '';
    } catch (e2) {}
  }

  var props = PropertiesService.getDocumentProperties();
  if (email) {
    props.setProperty(PROP.USER_EMAIL, email);
    return email;
  }
  return props.getProperty(PROP.USER_EMAIL) || '';
}

function getSheetIds_() {
  var title = sheetTitle_();
  var props = PropertiesService.getDocumentProperties();
  var cached = readJsonProp_(props, PROP.IDS, null);
  if (cached && cached.title === title && cached.jobId != null) return cached;

  var m = title.match(/_(\d+)_(\d+)_[A-Za-z]+\d+_frame(\d+)$/);
  if (!m) return null;

  var ids = {
    title: title,
    taskId: Number(m[1]),
    jobId: Number(m[2]),
    frame: Number(m[3]),
  };
  props.setProperty(PROP.IDS, JSON.stringify(ids));
  return ids;
}

function getConfig_() {
  var props = PropertiesService.getScriptProperties();

  return {
    baseUrl: (props.getProperty('CVAT_BASE_URL') || '')
      .replace(/\/$/, '')
      .trim(),

    pat: (props.getProperty('CVAT_PAT') || '').trim(),

    orgSlug: (props.getProperty('ORG_SLUG') || '').trim(),
  };
}

function sheetTitle_() {
  return SpreadsheetApp.getActiveSpreadsheet().getName();
}

function readJsonProp_(props, key, fallback) {
  var raw = props.getProperty(key);
  if (!raw) return fallback;
  try {
    return JSON.parse(raw);
  } catch (e) {
    return fallback;
  }
}

function fail_(detail, code) {
  logError_(detail);
  var out = { ok: false, detail: String(detail) };
  if (code != null) out.code = code;
  return out;
}

function log_() {
  var parts = ['[cvat]'];
  for (var i = 0; i < arguments.length; i++) parts.push(String(arguments[i]));
  console.log(parts.join(' '));
}

function logWarn_() {
  var parts = ['[cvat]'];
  for (var i = 0; i < arguments.length; i++) parts.push(String(arguments[i]));
  console.warn(parts.join(' '));
}

function logError_() {
  var parts = ['[cvat]'];
  for (var i = 0; i < arguments.length; i++) parts.push(String(arguments[i]));
  console.error(parts.join(' '));
}
