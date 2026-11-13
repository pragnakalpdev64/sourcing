/**
 * SourcingAI Sheets add-on (Editor add-on, installed for Skylink by the admin).
 *
 * Each person turns it on in their own request sheet (Extensions > SourcingAI >
 * Turn on for this sheet). Then a row added to the Requests tab starts a search
 * for that sheet, and the offers are written back into the same sheet.
 *
 * Differences from the per-sheet script (../per_sheet/Code.gs), forced by how
 * add-ons work:
 *   - no setup() run from the editor: users never see it; the menu turns it on;
 *   - no 90 s timer here: an add-on may not run a time trigger more than once an
 *     hour, so every edit calls /trigger at once ("edited": true) and the service
 *     waits until the sheet has been quiet for 90 s (job_main.wait_until_quiet);
 *   - no shared secret: script properties belong to the whole add-on, so the call
 *     is signed with the user's own Google token (ScriptApp.getOAuthToken), which
 *     the service checks is a Skylink account using this add-on.
 * The worker decides which rows to search (Status empty or NEW): this script never
 * writes a Status. Rows it misses are found by the Cloud Scheduler sweep.
 *
 * Script properties (set once by the developer in the add-on's script project;
 * users can't see or change them):
 *   TRIGGER_URL            https://<cloud run host>/trigger   (required)
 *   SERVICE_ACCOUNT_EMAIL  the worker's service account, shown when turning on
 *                          (each sheet has to be shared with it as Editor)
 *   REQUESTS_TAB           tab name, default "Requests"
 */

var DEFAULT_TAB = 'Requests';
var HANDLER_EDIT = 'onRequestsEdit';

// Header labels, lowercased, as the worker accepts them (sourcing/sheets.py).
var PART_HEADERS = ['part number', 'part', 'part no', 'pn', 'part_number'];
var STATUS_HEADERS = ['status'];
var REQUESTER_HEADERS = ['requested by', 'requester'];

function setting_(name, fallback) {
  var value = PropertiesService.getScriptProperties().getProperty(name);
  return value === null || value === '' ? fallback : value.trim();
}

/** Runs before the user has allowed anything (AuthMode.NONE): menu only. */
function onOpen(e) {
  SpreadsheetApp.getUi().createAddonMenu()
    .addItem('Turn on for this sheet', 'turnOn')
    .addItem('Search new rows now', 'searchNow')
    .addItem('Turn off for this sheet', 'turnOff')
    .addToUi();
}

function onInstall(e) {
  onOpen(e);
}

/** This user's edit trigger on this sheet (add-ons: at most one per type). */
function editTriggers_(ss) {
  return ScriptApp.getUserTriggers(ss).filter(function (t) {
    return t.getHandlerFunction() === HANDLER_EDIT;
  });
}

function turnOn() {
  var ss = SpreadsheetApp.getActive();
  var ui = SpreadsheetApp.getUi();
  var tab = setting_('REQUESTS_TAB', DEFAULT_TAB);
  var sheet = ss.getSheetByName(tab);
  if (!sheet || !partColumn_(headerOf_(sheet))) {
    ui.alert('SourcingAI', 'This sheet needs a "' + tab + '" tab with a "Part Number" ' +
             'column in row 1. Add it, then turn SourcingAI on again.', ui.ButtonSet.OK);
    return;
  }
  editTriggers_(ss).forEach(function (t) { ScriptApp.deleteTrigger(t); });
  // Google doesn't run installable triggers in an add-on test deployment
  // (2026-11-13: menu "Search now" worked, adding a row started nothing), so say
  // so instead of promising an automatic start.
  var made = '';
  try {
    ScriptApp.newTrigger(HANDLER_EDIT).forSpreadsheet(ss).onEdit().create();
  } catch (err) {
    made = String(err);
  }
  if (made || !editTriggers_(ss).length) {
    console.error('SourcingAI: edit trigger not installed: %s', made || 'none listed');
    ui.alert('SourcingAI', 'Automatic search could not be turned on here' +
             (made ? ' (' + made + ')' : '') + '. In a test deployment Google ' +
             'does not run edit triggers: use Extensions > SourcingAI > Search new rows ' +
             'now. It works automatically once the add-on is installed.', ui.ButtonSet.OK);
    return;
  }
  var share = setting_('SERVICE_ACCOUNT_EMAIL', '');
  ui.alert('SourcingAI is on for this sheet',
           'New rows in "' + tab + '" are searched about 90 seconds after the last edit. ' +
           'The offers are written to this sheet.' +
           (share ? '\n\nOnce per sheet: share it with ' + share + ' as Editor ' +
                    '(Share button), so SourcingAI can write the offers.' : ''),
           ui.ButtonSet.OK);
}

function turnOff() {
  var ss = SpreadsheetApp.getActive();
  editTriggers_(ss).forEach(function (t) { ScriptApp.deleteTrigger(t); });
  SpreadsheetApp.getUi().alert('SourcingAI', 'Turned off for this sheet. New rows are ' +
    'still picked up by the scheduled check while the sheet stays registered.',
    SpreadsheetApp.getUi().ButtonSet.OK);
}

/** Installable onEdit (simple triggers may not call other services). */
function onRequestsEdit(e) {
  if (!e || !e.range) return;
  var sheet = e.range.getSheet();
  if (sheet.getName() !== setting_('REQUESTS_TAB', DEFAULT_TAB)) return;
  var first = Math.max(e.range.getRow(), 2);   // row 1 is the header
  var last = e.range.getLastRow();
  if (last < first) return;

  var header = headerOf_(sheet);
  var partCol = partColumn_(header);
  if (!partCol) return;
  var statusCol = column_(header, STATUS_HEADERS);
  var requesterCol = column_(header, REQUESTER_HEADERS);

  var rows = sheet.getRange(first, 1, last - first + 1, header.length).getValues();
  // The trigger runs as the person who turned SourcingAI on; e.user is whoever
  // made this edit (same Workspace only, else empty: left for them to type).
  var email = '';
  try {
    email = (e.user && e.user.getEmail()) || Session.getActiveUser().getEmail();
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
  if (waiting) callTrigger_(true);
}

/** Menu item: start now, without the wait. */
function searchNow() {
  SpreadsheetApp.getActive().toast(callTrigger_(false), 'SourcingAI', 8);
}

function callTrigger_(edited) {
  var ss = SpreadsheetApp.getActive();
  var url = setting_('TRIGGER_URL', '');
  if (!url) return 'SourcingAI is not set up yet (TRIGGER_URL).';
  var response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({
      sheet_id: ss.getId(),
      label: ss.getName(),
      requests_tab: setting_('REQUESTS_TAB', DEFAULT_TAB),
      edited: edited
    }),
    headers: {
      // The user's own token: the service asks Google whose it is and checks it
      // was made for this add-on (service/api.py, _addon_refusal).
      'Authorization': 'Bearer ' + ScriptApp.getOAuthToken(),
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
  if (body.started) return edited ? 'Search starts once the sheet is quiet for 90 seconds.'
                                  : 'Search started.';
  if (body.waiting) return 'Search starts once the sheet is quiet for 90 seconds.';
  if (body.rerun) return 'A search is running; it picks up the new rows when it finishes.';
  return 'Not started: ' + (body.reason || text);
}

function headerOf_(sheet) {
  var width = Math.max(sheet.getLastColumn(), 1);
  return sheet.getRange(1, 1, 1, width).getValues()[0]
    .map(function (h) { return String(h).trim().toLowerCase(); });
}

function partColumn_(header) {
  return column_(header, PART_HEADERS);
}

function column_(header, labels) {
  for (var i = 0; i < labels.length; i++) {
    var at = header.indexOf(labels[i]);
    if (at >= 0) return at + 1;
  }
  return 0;
}
