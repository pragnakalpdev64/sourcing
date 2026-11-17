/**
 * SourcingAI Sheets add-on (Editor add-on, installed for Skylink by the admin).
 *
 * Each person turns it on in their own request sheet (Extensions > SourcingAI >
 * Turn on for this sheet). Then a row added to the Requests tab starts a search
 * for that sheet, and the offers are written back into the same sheet.
 *
 * The add-on does every read and write of the sheet; the service never opens it
 * (client, 2026-11-13: Skylink's org blocks the service account from their Sheets):
 *   1. with each call to /trigger it sends a copy of the Requests tab (and the
 *      RFQ / Results header rows, so the columns line up);
 *   2. the search's writes (Status, Notes, Run ID, RFQ lines) wait in the service;
 *   3. pullResults_ collects them (/sheet/pull), writes them into the sheet and
 *      confirms them (/sheet/ack). It keeps checking while a search runs, for up
 *      to about 5 minutes (Apps Script stops a run at 6). Anything still waiting
 *      is written on the next edit, when the sheet is opened, or from the menu
 *      (Get results now).
 *
 * Forced by how add-ons work:
 *   - no setup() run from the editor: users never see it; the menu turns it on;
 *   - no 90 s timer here: an add-on may not run a time trigger more than once an
 *     hour, so every edit calls /trigger at once ("edited": true) and the service
 *     waits until the sheet has been quiet for 90 s (job_main.wait_until_quiet);
 *   - no shared secret: script properties belong to the whole add-on, so the call
 *     is signed with the user's own Google token (ScriptApp.getOAuthToken), which
 *     the service checks is a Skylink account using this add-on;
 *   - Google sign-in (IAP) in front of the service: the user's ID token goes in
 *     Proxy-Authorization, which IAP checks (scope "openid"; the add-on's OAuth
 *     client must be on IAP's programmatic-access list);
 *   - installable triggers (on edit, on open) don't run in a test deployment:
 *     there, use Search new rows now / Get results now.
 *
 * Script properties (set once by the developer in the add-on's script project;
 * users can't see or change them):
 *   TRIGGER_URL   https://<host>/trigger   (required; /sheet/pull and /sheet/ack
 *                 are next to it)
 *   REQUESTS_TAB  tab name, default "Requests"
 */

var DEFAULT_TAB = 'Requests';
var RFQ_TAB = 'RFQ';
var RESULTS_TAB = 'Results';
var HANDLER_EDIT = 'onRequestsEdit';
var HANDLER_OPEN = 'onSheetOpen';
var PULL_PAUSE_MS = 8000;
var PULL_MAX_MS = 5 * 60 * 1000;

// Header labels, lowercased, as the worker accepts them (sourcing/sheets.py).
var PART_HEADERS = ['part number', 'part', 'part no', 'pn', 'part_number'];
var STATUS_HEADERS = ['status'];
var REQUESTER_HEADERS = ['requested by', 'requester'];

function setting_(name, fallback) {
  var value = PropertiesService.getScriptProperties().getProperty(name);
  return value === null || value === '' ? fallback : value.trim();
}

/** https://host/trigger -> https://host/<path> */
function endpoint_(path) {
  return setting_('TRIGGER_URL', '').replace(/\/trigger\/?$/, '') + path;
}

/** Runs before the user has allowed anything (AuthMode.NONE): menu only. */
function onOpen(e) {
  SpreadsheetApp.getUi().createAddonMenu()
    .addItem('Turn on for this sheet', 'turnOn')
    .addItem('Search new rows now', 'searchNow')
    .addItem('Get results now', 'getResultsNow')
    .addItem('Turn off for this sheet', 'turnOff')
    .addToUi();
}

function onInstall(e) {
  onOpen(e);
}

/** This user's SourcingAI triggers on this sheet (add-ons: at most one per type). */
function ourTriggers_(ss) {
  return ScriptApp.getUserTriggers(ss).filter(function (t) {
    var fn = t.getHandlerFunction();
    return fn === HANDLER_EDIT || fn === HANDLER_OPEN;
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
  ourTriggers_(ss).forEach(function (t) { ScriptApp.deleteTrigger(t); });
  // Google doesn't run installable triggers in an add-on test deployment
  // (2026-11-13: menu "Search now" worked, adding a row started nothing), so say
  // so instead of promising an automatic start.
  var made = '';
  try {
    ScriptApp.newTrigger(HANDLER_EDIT).forSpreadsheet(ss).onEdit().create();
    // Results that came in while nobody had the sheet open are written when it opens.
    ScriptApp.newTrigger(HANDLER_OPEN).forSpreadsheet(ss).onOpen().create();
  } catch (err) {
    made = String(err);
  }
  if (made || !ourTriggers_(ss).length) {
    console.error('SourcingAI: triggers not installed: %s', made || 'none listed');
    ui.alert('SourcingAI', 'Automatic search could not be turned on here' +
             (made ? ' (' + made + ')' : '') + '. In a test deployment Google ' +
             'does not run these triggers: use Extensions > SourcingAI > Search new rows ' +
             'now. It works automatically once the add-on is installed.', ui.ButtonSet.OK);
    return;
  }
  ui.alert('SourcingAI is on for this sheet',
           'New rows in "' + tab + '" are searched about 90 seconds after the last edit. ' +
           'Statuses and offers are written to this sheet as they come in.', ui.ButtonSet.OK);
}

function turnOff() {
  var ss = SpreadsheetApp.getActive();
  ourTriggers_(ss).forEach(function (t) { ScriptApp.deleteTrigger(t); });
  SpreadsheetApp.getUi().alert('SourcingAI', 'Turned off for this sheet. Search new rows ' +
    'now still works from this menu.', SpreadsheetApp.getUi().ButtonSet.OK);
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
  if (!waiting) return;
  callTrigger_(true);
  pullResults_(PULL_MAX_MS);
}

/** Installable onOpen: write results that came in while the sheet was closed. */
function onSheetOpen(e) {
  pullResults_(60 * 1000);
}

/** Menu item: start now, without the wait, then write the results as they come. */
function searchNow() {
  var ss = SpreadsheetApp.getActive();
  ss.toast(callTrigger_(false), 'SourcingAI', 8);
  var n = pullResults_(PULL_MAX_MS);
  if (n !== null) ss.toast(n ? n + ' update(s) written to the sheet.' :
                           'Still searching: results are written on the next edit, ' +
                           'when the sheet is opened, or with Get results now.', 'SourcingAI', 10);
}

/** Menu item: write whatever is waiting. */
function getResultsNow() {
  var n = pullResults_(60 * 1000);
  SpreadsheetApp.getActive().toast(n === null ? 'Another SourcingAI update is writing ' +
    'to this sheet right now.' : n + ' update(s) written to the sheet.', 'SourcingAI', 8);
}

// -- calls to the service ------------------------------------------------------

function post_(url, payload) {
  var headers = {
    // The user's own token: the service asks Google whose it is and checks it
    // was made for this add-on (service/api.py, _addon_refusal).
    'Authorization': 'Bearer ' + ScriptApp.getOAuthToken(),
    // ngrok's free plan shows a warning page to unknown clients without it.
    'ngrok-skip-browser-warning': '1'
  };
  // Google sign-in (IAP) in front of the service (client, 2026-11-13: "Google
  // sign-in stays on"): IAP takes the user's ID token from Proxy-Authorization
  // and passes Authorization on untouched. Needs the "openid" scope; harmless
  // where there is no IAP (local runs through ngrok).
  var idToken = ScriptApp.getIdentityToken();
  if (idToken) headers['Proxy-Authorization'] = 'Bearer ' + idToken;
  var response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    headers: headers,
    muteHttpExceptions: true,
    followRedirects: false   // IAP answers a refused call with a sign-in redirect
  });
  var code = response.getResponseCode();
  var text = response.getContentText();
  if (code >= 300 && code < 400 || (code === 401 || code === 403) && text.charAt(0) !== '{') {
    // Not our service's JSON: IAP stopped the call before it reached us.
    text = 'Google sign-in (IAP) did not let the call through. ' + text.slice(0, 200);
  }
  return {code: code, text: text};
}

function callTrigger_(edited) {
  var ss = SpreadsheetApp.getActive();
  if (!setting_('TRIGGER_URL', '')) return 'SourcingAI is not set up yet (TRIGGER_URL).';
  var reply = post_(setting_('TRIGGER_URL', ''), {
    sheet_id: ss.getId(),
    label: ss.getName(),
    requests_tab: setting_('REQUESTS_TAB', DEFAULT_TAB),
    edited: edited,
    snapshot: snapshot_(ss)
  });
  var text = reply.text.slice(0, 300);
  if (reply.code !== 200) {
    console.error('SourcingAI /trigger answered %s: %s', reply.code, text);
    return 'Could not start the search (' + reply.code + '): ' + text;
  }
  console.log('SourcingAI /trigger: %s', text);
  var body = JSON.parse(reply.text);
  if (body.started) return edited ? 'Search starts once the sheet is quiet for 90 seconds.'
                                  : 'Search started.';
  if (body.waiting) return 'Search starts once the sheet is quiet for 90 seconds.';
  if (body.rerun) return 'A search is running; it picks up the new rows when it finishes.';
  return 'Not started: ' + (body.reason || text);
}

/** What the worker reads: the whole Requests tab, and the header row of the tabs it
 *  writes to (it lines its columns up with them). Shown text, as Google's API gives it. */
function snapshot_(ss) {
  var tabs = {};
  var tab = setting_('REQUESTS_TAB', DEFAULT_TAB);
  var requests = ss.getSheetByName(tab);
  tabs[tab] = requests ? requests.getDataRange().getDisplayValues() : [];
  [RFQ_TAB, RESULTS_TAB].forEach(function (name) {
    var sheet = ss.getSheetByName(name);
    if (sheet) {
      var width = sheet.getLastColumn();
      tabs[name] = width ? [sheet.getRange(1, 1, 1, width).getDisplayValues()[0]] : [];
    }
  });
  return tabs;
}

/**
 * Collects the writes waiting for this sheet, writes them, confirms them; repeats
 * while a search runs, up to maxMs. Returns how many were written, or null when
 * another run of this add-on is already doing it for this sheet.
 */
function pullResults_(maxMs) {
  if (!setting_('TRIGGER_URL', '')) return 0;
  var lock = LockService.getDocumentLock();
  if (!lock.tryLock(1000)) return null;
  var ss = SpreadsheetApp.getActive();
  var deadline = Date.now() + maxMs;
  var written = 0;
  try {
    while (true) {
      var reply = post_(endpoint_('/sheet/pull'), {sheet_id: ss.getId()});
      if (reply.code !== 200) {
        console.error('SourcingAI /sheet/pull answered %s: %s', reply.code,
                      reply.text.slice(0, 300));
        break;
      }
      var body = JSON.parse(reply.text);
      var done = applyOps_(ss, body.ops || []);
      if (done.length) {
        SpreadsheetApp.flush();
        var ack = post_(endpoint_('/sheet/ack'), {sheet_id: ss.getId(), ids: done});
        if (ack.code !== 200) {
          // Written but not confirmed: they come again and are written again
          // (statuses: same value; RFQ lines: a duplicate line). Logged to find out.
          console.error('SourcingAI /sheet/ack answered %s: %s', ack.code, ack.text.slice(0, 300));
          break;
        }
        written += done.length;
      }
      if (!(body.ops || []).length && !body.busy) break;
      if (Date.now() + PULL_PAUSE_MS > deadline) break;
      if (!(body.ops || []).length) Utilities.sleep(PULL_PAUSE_MS);
    }
  } finally {
    lock.releaseLock();
  }
  return written;
}

// -- writing what the service sent (sourcing/sheet_relay.py) ----------------------

/** Applies the writes in order; returns the ids written (confirmed with /sheet/ack). */
function applyOps_(ss, ops) {
  var done = [];
  for (var i = 0; i < ops.length; i++) {
    var op = ops[i];
    try {
      if (op.kind === 'add_sheet') {
        if (!ss.getSheetByName(op.tab)) ss.insertSheet(op.tab);
      } else if (op.kind === 'cells') {
        var sheet = ss.getSheetByName(op.tab);
        if (sheet) writeCells_(sheet, op);
      } else if (op.kind === 'append') {
        var target = ss.getSheetByName(op.tab) || ss.insertSheet(op.tab);
        if (op.rows.length) {
          writeBlock_(target, target.getLastRow() + 1, 1, op.rows);
        }
      } else {
        console.error('SourcingAI: unknown write %s', JSON.stringify(op).slice(0, 200));
      }
      done.push(op.id);
    } catch (err) {
      // Stop here: later writes may depend on this one (e.g. a header row).
      console.error('SourcingAI: could not write %s: %s', JSON.stringify(op).slice(0, 200), err);
      break;
    }
  }
  return done;
}

function writeCells_(sheet, op) {
  var guard = op.guard || null;
  // Decided per row before anything is written: the op's own Status write would
  // otherwise make its Run ID / Notes writes look stale.
  var target = {};
  op.updates.forEach(function (u) {
    var asked = sheet.getRange(u.range).getRow();
    var key = String(asked);
    if (target[key] !== undefined) return;
    var row = asked;
    if (guard && guard.rows && guard.rows[key] !== undefined) {
      row = guardedRow_(sheet, row, guard.col, guard.rows[key]);
      if (!row) {
        console.error('SourcingAI: row with part %s not found; row %s not written',
                      guard.rows[key], key);
      }
    }
    // A person changed this row's Status since the service wrote (e.g. FAILED -> NEW
    // to search again): their edit wins (sheet_relay.stale_rows, 2026-11-16).
    if (row && guard && guard.status_col && guard.status &&
        guard.status[key] !== undefined &&
        !sameStatus_(sheet.getRange(row, guard.status_col).getDisplayValue(), guard.status[key])) {
      console.log('SourcingAI: row %s Status changed by a person; this write skipped', row);
      row = 0;
    }
    target[key] = row;
  });
  op.updates.forEach(function (u) {
    var at = sheet.getRange(u.range);
    var row = target[String(at.getRow())];
    if (row) writeBlock_(sheet, row, at.getColumn(), u.values);
  });
}

/** Empty and NEW both mean "search this row". */
function sameStatus_(a, b) {
  a = String(a || '').trim().toUpperCase() || 'NEW';
  b = String(b || '').trim().toUpperCase() || 'NEW';
  return a === b;
}

/** The row still holding this part, or the one it moved to (rows inserted or
 *  deleted since the copy was sent); 0 if it isn't unique any more. */
function guardedRow_(sheet, row, col, part) {
  var want = String(part).trim().toUpperCase();
  if (String(sheet.getRange(row, col).getDisplayValue()).trim().toUpperCase() === want) return row;
  var last = sheet.getLastRow();
  if (last < 2) return 0;
  var values = sheet.getRange(2, col, last - 1, 1).getDisplayValues();
  var found = [];
  values.forEach(function (v, i) {
    if (String(v[0]).trim().toUpperCase() === want) found.push(i + 2);
  });
  return found.length === 1 ? found[0] : 0;
}

/** Writes values like the API's RAW: text stays text ("35010-38" is not a date). */
function writeBlock_(sheet, row, col, values) {
  var width = 0;
  values.forEach(function (line) { width = Math.max(width, line.length); });
  if (!width) return;
  var need = col + width - 1;
  if (sheet.getMaxColumns() < need) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), need - sheet.getMaxColumns());
  }
  var grid = values.map(function (line) {
    var out = line.slice();
    while (out.length < width) out.push('');
    return out;
  });
  var formats = grid.map(function (line) {
    return line.map(function (v) { return typeof v === 'string' ? '@' : 'General'; });
  });
  var range = sheet.getRange(row, col, grid.length, width);
  range.setNumberFormats(formats);
  range.setValues(grid);
}

// -- sheet helpers -----------------------------------------------------------------

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
