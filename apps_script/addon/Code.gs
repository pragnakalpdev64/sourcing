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
 *      confirms them (/sheet/ack). Apps Script stops any one run at 6 minutes and
 *      a search can take longer (AAR, 11 parts: 15 min, 2026-11-17), so it is
 *      called from several places:
 *        - the sidebar (Show progress; Search new rows now opens it): checks every
 *          15 s while it is open, each check a short run, and shows the worker's
 *          step and heartbeat (sourcing/progress.py);
 *        - an edit (keeps checking for up to about 5 minutes), opening the sheet,
 *          Get results now;
 *        - an hourly timer: results that come in while the sheet is closed are
 *          written within the hour.
 *      The service lets one of them write at a time (a lease, /sheet/pull), so
 *      the same RFQ lines are never written twice.
 *
 * Forced by how add-ons work:
 *   - no setup() run from the editor: users never see it; the menu turns it on;
 *   - no 90 s timer here: an add-on may not run a time trigger more than once an
 *     hour, so every edit calls /trigger at once ("edited": true) and the service
 *     waits until the sheet has been quiet for 90 s (job_main.wait_until_quiet);
 *     the hourly timer only writes waiting results. It has no open sheet, so it
 *     opens it by id: scope "spreadsheets" instead of "spreadsheets.currentonly"
 *     (2026-11-18; the admin approves the new scope once);
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
var HANDLER_TIMER = 'onTimer';
// User property: hourly timer id -> the sheet it writes (the timer gets no sheet).
var TIMER_PREFIX = 'timer:';
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
    .addItem('Show progress', 'showProgress')
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
  removeTimer_(ss);
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
  ensureTimer_(ss);
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
           'Statuses and offers are written to this sheet as they come in; Extensions > ' +
           'SourcingAI > Show progress follows a search while it runs. Results that ' +
           'come in while the sheet is closed are written within the hour.', ui.ButtonSet.OK);
}

function turnOff() {
  var ss = SpreadsheetApp.getActive();
  ourTriggers_(ss).forEach(function (t) { ScriptApp.deleteTrigger(t); });
  removeTimer_(ss);
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
  var result = pullResults_(e.source || SpreadsheetApp.getActive(), PULL_MAX_MS);
  if (result && result.busy) SpreadsheetApp.getActive().toast(STILL_RUNNING, 'SourcingAI', 15);
}

/** Installable onOpen: write results that came in while the sheet was closed. */
function onSheetOpen(e) {
  var ss = SpreadsheetApp.getActive();
  ensureTimer_(ss);      // sheets turned on before the timer existed get one now
  var result = pullResults_(ss, 60 * 1000);
  if (result && result.busy) ss.toast(STILL_RUNNING, 'SourcingAI', 15);
}

/** Menu item: start now, without the wait; the sidebar follows the search. */
function searchNow() {
  var ss = SpreadsheetApp.getActive();
  ss.toast(callTrigger_(false), 'SourcingAI', 8);
  showProgress();
}

/** Menu item: write whatever is waiting. */
function getResultsNow() {
  var ss = SpreadsheetApp.getActive();
  var message = pullMessage_(pullResults_(ss, 60 * 1000)) || 'Nothing waiting to be written.';
  ss.toast(message, 'SourcingAI', 15);
}

/** Menu item: the sidebar that follows a search (Sidebar.html). */
function showProgress() {
  SpreadsheetApp.getUi().showSidebar(
    HtmlService.createHtmlOutputFromFile('Sidebar').setTitle('SourcingAI'));
}

/** Called by the sidebar every few seconds: one short check, results written. */
function sidebarPoll() {
  var ss = SpreadsheetApp.getActive();
  var result = pullResults_(ss, 0);
  if (result === null) return {written: 0, busy: true, writing: true, progress: null};
  return result;
}

/** Hourly timer: writes results that came in while nobody had the sheet open. */
function onTimer(e) {
  var id = e && e.triggerUid ?
    PropertiesService.getUserProperties().getProperty(TIMER_PREFIX + e.triggerUid) : null;
  if (!id) return;
  var ss;
  try {
    ss = SpreadsheetApp.openById(id);
  } catch (err) {
    // Sheet deleted or no longer shared with this person: the timer has no work.
    console.error('SourcingAI timer: cannot open %s: %s', id, err);
    return;
  }
  var result = pullResults_(ss, 4 * 60 * 1000);
  if (result && result.written) console.log('SourcingAI timer: %s update(s) written', result.written);
}

// -- the hourly timer (one per person per sheet) -------------------------------

function timerFor_(ss) {
  var props = PropertiesService.getUserProperties();
  var found = null;
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === HANDLER_TIMER &&
        props.getProperty(TIMER_PREFIX + t.getUniqueId()) === ss.getId()) found = t;
  });
  return found;
}

function ensureTimer_(ss) {
  try {
    if (timerFor_(ss)) return;
    var t = ScriptApp.newTrigger(HANDLER_TIMER).timeBased().everyHours(1).create();
    PropertiesService.getUserProperties().setProperty(TIMER_PREFIX + t.getUniqueId(), ss.getId());
  } catch (err) {
    // Not fatal: results are still written on edit, open, the menu and the sidebar.
    console.error('SourcingAI: hourly timer not set up: %s', err);
  }
}

function removeTimer_(ss) {
  try {
    var t = timerFor_(ss);
    if (!t) return;
    PropertiesService.getUserProperties().deleteProperty(TIMER_PREFIX + t.getUniqueId());
    ScriptApp.deleteTrigger(t);
  } catch (err) {
    console.error('SourcingAI: hourly timer not removed: %s', err);
  }
}

// -- calls to the service ------------------------------------------------------

function post_(url, payload) {
  var headers = {
    // The user's own token: the service asks Google whose it is and checks it
    // was made for this add-on (service/api.py, _addon_refusal).
    'Authorization': 'Bearer ' + ScriptApp.getOAuthToken(),
    // The same token again: behind IAP on Cloud Run, Authorization doesn't reach
    // the service (2026-11-20, "no add-on token"); this header does.
    'X-SourcingAI-Token': ScriptApp.getOAuthToken(),
    // ngrok's free plan shows a warning page to unknown clients without it.
    'ngrok-skip-browser-warning': '1'
  };
  // Google sign-in (IAP) in front of the service (client, 2026-11-13: "Google
  // sign-in stays on"): IAP takes the user's ID token from Proxy-Authorization
  // and passes Authorization on untouched. Needs the "openid" scope; harmless
  // where there is no IAP (local runs through ngrok).
  var idToken = ScriptApp.getIdentityToken();
  if (idToken) headers['Proxy-Authorization'] = 'Bearer ' + idToken;
  var response;
  try {
    response = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload),
      headers: headers,
      muteHttpExceptions: true,
      followRedirects: false   // IAP answers a refused call with a sign-in redirect
    });
  } catch (err) {
    // Address not found, connection refused, timeout: no answer at all.
    return {code: 0, text: String(err)};
  }
  var code = response.getResponseCode();
  var text = response.getContentText();
  if (code >= 300 && code < 400 || (code === 401 || code === 403) && text.charAt(0) !== '{') {
    // Not our service's JSON: IAP stopped the call before it reached us.
    text = 'Google sign-in (IAP) did not let the call through. ' + text.slice(0, 200);
  }
  return {code: code, text: text};
}

/**
 * One line a person can act on, for an answer that isn't 200. The page a proxy
 * or ngrok sends when the service is down is HTML, not worth showing (seen
 * 2026-11-18: "The service answered 502: <!DOCTYPE html> ..."); the full answer
 * stays in the Executions log.
 */
function problem_(reply) {
  var code = reply.code;
  var text = String(reply.text || '');
  if (text.charAt(0) === '{') {
    try {
      var detail = JSON.parse(text).detail;
      if (detail) return (typeof detail === 'string' ? detail : JSON.stringify(detail)) +
                         ' (' + code + ')';
    } catch (err) {}
  }
  if (code === 0 || code === 404 || code === 502 || code === 503 || code === 504) {
    return 'The SourcingAI service is not reachable right now (it may be stopped or ' +
           'restarting). Try again in a minute; if it stays like this, tell the SourcingAI ' +
           'team. (' + (code || 'no answer') + ')';
  }
  if (text.indexOf('Google sign-in (IAP)') === 0) return text.slice(0, 120) + ' (' + code + ')';
  if (code === 429) return 'The service is busy; try again in a minute. (429)';
  return 'The SourcingAI service had a problem (' + code + '). Try again in a minute.';
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
    return 'Could not start the search: ' + problem_(reply);
  }
  console.log('SourcingAI /trigger: %s', text);
  var body = JSON.parse(reply.text);
  if (body.started) return edited ? 'Search starts once the sheet is quiet for 90 seconds.'
                                  : 'Search started.';
  if (body.waiting) return 'Search starts once the sheet is quiet for 90 seconds.';
  if (body.rerun) return 'Another search is running. This sheet is in line and starts when ' +
                         'it ends: no need to click again (Show progress follows it).';
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
 * while a search runs, up to maxMs (0 = one check, the sidebar's). Returns
 * {written, busy, writing, progress}: how many were written, whether a search
 * was still running, whether another add-on run held the sheet's write lease,
 * and the worker's progress (/sheet/pull). null when another run of this add-on
 * already holds this sheet's document lock.
 * Waiting writes are always written to the end once started (the lease is let go
 * when a pull finds nothing), within WRITE_MAX_MS.
 */
var WRITE_MAX_MS = 4 * 60 * 1000;

function pullResults_(ss, maxMs) {
  var result = {written: 0, busy: false, writing: false, progress: null};
  if (!setting_('TRIGGER_URL', '')) return result;
  // The hourly timer has no open document, so no document lock: the service's
  // lease is what keeps two writers apart there.
  var lock = LockService.getDocumentLock();
  if (lock && !lock.tryLock(1000)) return null;
  var start = Date.now();
  var deadline = start + maxMs;
  var lease = '';
  try {
    while (true) {
      var reply = post_(endpoint_('/sheet/pull'), {sheet_id: ss.getId(), lease: lease});
      if (reply.code !== 200) {
        console.error('SourcingAI /sheet/pull answered %s: %s', reply.code,
                      reply.text.slice(0, 300));
        result.error = problem_(reply);
        break;
      }
      var body = JSON.parse(reply.text);
      result.busy = !!body.busy;
      result.writing = !!body.writing;
      result.progress = body.progress || null;
      if (body.lease) lease = body.lease;
      var ops = body.ops || [];
      var done = applyOps_(ss, ops);
      if (done.length) {
        SpreadsheetApp.flush();
        var ack = post_(endpoint_('/sheet/ack'), {sheet_id: ss.getId(), ids: done});
        if (ack.code !== 200) {
          // Written but not confirmed: they come again and are written again
          // (statuses: same value; RFQ lines: a duplicate line). Logged to find out.
          console.error('SourcingAI /sheet/ack answered %s: %s', ack.code, ack.text.slice(0, 300));
          break;
        }
        result.written += done.length;
      }
      if (done.length < ops.length) break;          // a write failed: stop, try later
      if (ops.length) {
        if (Date.now() - start < WRITE_MAX_MS) continue;   // more may be waiting
        break;
      }
      if (!body.busy && !body.writing) break;
      if (Date.now() + PULL_PAUSE_MS > deadline) break;
      // Waiting, not writing: let go of the document lock meanwhile, or the
      // sidebar's checks found it held for minutes and said "Another SourcingAI
      // update is writing" (2026-11-18). Taken again before the next pull.
      if (lock) lock.releaseLock();
      Utilities.sleep(PULL_PAUSE_MS);
      while (lock && !lock.tryLock(1000)) {
        if (Date.now() + PULL_PAUSE_MS > deadline) return result;
        Utilities.sleep(PULL_PAUSE_MS);   // the sidebar is writing; it does the work
      }
    }
  } finally {
    if (lock && lock.hasLock()) lock.releaseLock();
  }
  return result;
}

var STILL_RUNNING = 'The search is still running. Follow it with Extensions > ' +
                    'SourcingAI > Show progress; its results are written there as they ' +
                    'come, on your next edit, when the sheet is opened, or within the hour.';

/** What to tell the person after a pull; '' = nothing worth a message. */
function pullMessage_(result) {
  if (result === null) return 'Another SourcingAI update is writing to this sheet right now.';
  var wrote = result.written ? result.written + ' update(s) written to the sheet. ' : '';
  if (result.busy) return wrote + STILL_RUNNING;
  return result.written ? wrote + 'Search finished.' : '';
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
      } else if (op.kind === 'insert_cols') {
        // A new column (RFQ: "Vendor Reference" as column A when the tab had none,
        // 2026-11-20); everything to its right moves one place.
        var tabAt = ss.getSheetByName(op.tab);
        if (tabAt) {
          tabAt.insertColumnBefore(op.at || 1);
          if (op.values.length) writeBlock_(tabAt, 1, op.at || 1, op.values);
        }
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
