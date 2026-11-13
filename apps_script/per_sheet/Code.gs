/**
 * SourcingAI: start a search when a team member adds a row to the Requests tab.
 *
 * Installed in one request sheet (Extensions > Apps Script), set up once with
 * setup() (see apps_script/README.md). What it does:
 *   - an edit on the Requests tab (typing or pasting rows) is noted;
 *   - once nobody has edited the tab for DELAY_SECONDS, it calls SourcingAI's
 *     /trigger once, so a row isn't searched while its quantity or vendor is
 *     still being typed, and pasting 20 rows gives one run, not 20;
 *   - Requested By is filled in with the editor's email when it's empty (works
 *     for people in Skylink's Google Workspace; otherwise left for them to type).
 * The worker itself decides which rows to search (Status empty or NEW), so this
 * script never writes a Status. Rows it misses (written by another program, or
 * a failed call) are found by the Cloud Scheduler sweep.
 *
 * Script properties (Project Settings > Script properties):
 *   TRIGGER_URL     https://<cloud run or ngrok host>/trigger   (required)
 *   TRIGGER_SECRET  same value as the service's TRIGGER_SECRET  (required)
 *   REQUESTS_TAB    tab name, default "Requests"
 *   DELAY_SECONDS   quiet time before the call, default 90
 */

var DEFAULT_TAB = 'Requests';
var DEFAULT_DELAY_SECONDS = 90;
var HANDLER_EDIT = 'onRequestsEdit';
var HANDLER_SEND = 'sendTrigger';

// Header labels, lowercased, as the worker accepts them (sourcing/sheets.py).
var PART_HEADERS = ['part number', 'part', 'part no', 'pn', 'part_number'];
var STATUS_HEADERS = ['status'];
var REQUESTER_HEADERS = ['requested by', 'requester'];

function props_() {
  return PropertiesService.getScriptProperties();
}

function setting_(name, fallback) {
  var value = props_().getProperty(name);
  return value === null || value === '' ? fallback : value.trim();
}

function delayMs_() {
  return Number(setting_('DELAY_SECONDS', DEFAULT_DELAY_SECONDS)) * 1000;
}

function onOpen() {
  SpreadsheetApp.getUi().createMenu('SourcingAI')
    .addItem('Search new rows now', 'searchNow')
    .addToUi();
}

/** Run once from the editor: installs the edit trigger (replacing old ones). */
function setup() {
  if (!setting_('TRIGGER_URL', '') || !setting_('TRIGGER_SECRET', '')) {
    throw new Error('Set the script properties TRIGGER_URL and TRIGGER_SECRET first.');
  }
  ScriptApp.getProjectTriggers().forEach(function (t) {
    var fn = t.getHandlerFunction();
    if (fn === HANDLER_EDIT || fn === HANDLER_SEND) ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger(HANDLER_EDIT)
    .forSpreadsheet(SpreadsheetApp.getActive())
    .onEdit()
    .create();
  console.log('SourcingAI trigger installed for tab "%s"', setting_('REQUESTS_TAB', DEFAULT_TAB));
}

/** Installable onEdit (simple triggers may not call other services). */
function onRequestsEdit(e) {
  if (!e || !e.range) return;
  var sheet = e.range.getSheet();
  if (sheet.getName() !== setting_('REQUESTS_TAB', DEFAULT_TAB)) return;
  var first = Math.max(e.range.getRow(), 2);   // row 1 is the header
  var last = e.range.getLastRow();
  if (last < first) return;

  var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0]
    .map(function (h) { return String(h).trim().toLowerCase(); });
  var partCol = column_(header, PART_HEADERS);
  if (!partCol) return;
  var statusCol = column_(header, STATUS_HEADERS);
  var requesterCol = column_(header, REQUESTER_HEADERS);

  var rows = sheet.getRange(first, 1, last - first + 1, header.length).getValues();
  var email = '';
  try {
    email = Session.getActiveUser().getEmail();
  } catch (err) {
    email = '';
  }
  var waiting = false;
  rows.forEach(function (row, i) {
    if (!String(row[partCol - 1]).trim()) return;
    var status = statusCol ? String(row[statusCol - 1]).trim().toUpperCase() : '';
    if (status !== '' && status !== 'NEW') return;
    waiting = true;
    if (requesterCol && email && !String(row[requesterCol - 1]).trim()) {
      sheet.getRange(first + i, requesterCol).setValue(email);
    }
  });
  if (waiting) schedule_();
}

/** Notes the edit and makes sure one delayed call is pending. */
function schedule_() {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    props_().setProperty('LAST_EDIT_MS', String(Date.now()));
    var pending = ScriptApp.getProjectTriggers().some(function (t) {
      return t.getHandlerFunction() === HANDLER_SEND;
    });
    if (!pending) {
      ScriptApp.newTrigger(HANDLER_SEND).timeBased().after(delayMs_()).create();
    }
  } finally {
    lock.releaseLock();
  }
}

/** Time trigger: calls /trigger once the tab has been quiet for DELAY_SECONDS. */
function sendTrigger() {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    ScriptApp.getProjectTriggers().forEach(function (t) {
      if (t.getHandlerFunction() === HANDLER_SEND) ScriptApp.deleteTrigger(t);
    });
    var quiet = Date.now() - Number(props_().getProperty('LAST_EDIT_MS') || 0);
    var wait = delayMs_() - quiet;
    if (wait > 5000) {
      // Someone is still typing: wait until the tab is quiet.
      ScriptApp.newTrigger(HANDLER_SEND).timeBased().after(wait).create();
      return;
    }
  } finally {
    lock.releaseLock();
  }
  callTrigger_();
}

/** Menu item: start now, without the wait. */
function searchNow() {
  var result = callTrigger_();
  SpreadsheetApp.getActive().toast(result, 'SourcingAI', 8);
}

function callTrigger_() {
  var ss = SpreadsheetApp.getActive();
  var response = UrlFetchApp.fetch(setting_('TRIGGER_URL', ''), {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({
      sheet_id: ss.getId(),
      label: ss.getName(),
      requests_tab: setting_('REQUESTS_TAB', DEFAULT_TAB)
    }),
    headers: {
      'X-Trigger-Secret': setting_('TRIGGER_SECRET', ''),
      // ngrok's free plan shows a warning page to unknown clients without it.
      'ngrok-skip-browser-warning': '1'
    },
    muteHttpExceptions: true
  });
  var code = response.getResponseCode();
  var text = response.getContentText().slice(0, 300);
  if (code !== 200) {
    // The Scheduler sweep still finds the rows; the log shows why this failed.
    console.error('SourcingAI /trigger answered %s: %s', code, text);
    return 'Could not start the search (' + code + '). The next scheduled check picks the rows up.';
  }
  console.log('SourcingAI /trigger: %s', text);
  var body = JSON.parse(text);
  if (body.started) return 'Search started.';
  if (body.rerun) return 'A search is running; it picks up the new rows when it finishes.';
  return 'Not started: ' + (body.reason || text);
}

function column_(header, labels) {
  for (var i = 0; i < labels.length; i++) {
    var at = header.indexOf(labels[i]);
    if (at >= 0) return at + 1;
  }
  return 0;
}
