/**
 * Gmail → reply-to export for the local Substack outreach workspace.
 *
 * Run it yourself in Google Apps Script (script.google.com, new project, paste this file).
 * It only READS messages under one Gmail label and writes one JSON file to your own Drive.
 * It sends nothing, changes no labels and deletes nothing.
 *
 * Output: a JSON array of {publication_url, sender_name, reply_to, message_url}, where
 * message_url is the Gmail permalink of the thread the record came from (its provenance).
 * Download the file to .local\ and import it with:
 *   py -3.12 -m substack_outreach --config .\substack.config.json contacts-import .\.local\substack-replyto.json
 * Every imported route stays unconfirmed until a human checks it in the review UI.
 */

var REPLYTO_LABEL = 'Substack';   // Gmail label to scan
var REPLYTO_MAX_THREADS = 500;    // upper bound per run
var REPLYTO_PAGE = 100;           // GmailApp.search page size (API maximum is 500)

/** "Name <a@b.example>" or "a@b.example" -> {name, address}; address is '' when none is present. */
function parseAddress(value) {
  var text = String(value || '').trim();
  var angle = text.match(/^(.*?)<\s*([^<>\s]+@[^<>\s]+)\s*>\s*$/);
  if (angle) return { name: angle[1].replace(/^\s*"|"\s*$/g, '').trim(), address: angle[2] };
  var bare = text.match(/^[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+$/);
  return { name: '', address: bare ? text : '' };
}

/** https origin of a URL, without www., or '' when it is not an https URL on a publication host. */
function publicationOrigin(url) {
  var m = String(url || '').match(/^https:\/\/([a-z0-9.-]+)(?::443)?(?:[\/?#]|$)/i);
  if (!m) return '';
  var host = m[1].toLowerCase().replace(/^www\./, '');
  // substack.com itself is the platform (profiles, redirects), never a publication.
  if (host === 'substack.com' || /\.substackcdn\.com$/.test(host) || host === 'substackcdn.com') return '';
  return 'https://' + host;
}

/** Publication URL from List-URL, then List-Id, then the first direct /p/ post link in the body. */
function publicationFromMessage(headers, body) {
  var listUrl = String(headers['List-URL'] || '').match(/<(https:[^>]+)>/);
  if (listUrl && publicationOrigin(listUrl[1])) return publicationOrigin(listUrl[1]);
  var listId = String(headers['List-Id'] || '').match(/<([a-z0-9.-]+\.[a-z]{2,})>/i);
  if (listId && publicationOrigin('https://' + listId[1])) return publicationOrigin('https://' + listId[1]);
  var links = String(body || '').match(/https:\/\/[^\s"'<>]+\/p\/[^\s"'<>]+/g) || [];
  for (var i = 0; i < links.length; i++) {
    var origin = publicationOrigin(links[i]);
    if (origin) return origin;
  }
  return '';
}

/** One export record per message; newest message wins for each publication/reply-to pair. */
function collectReplyTo(gmail) {
  var label = gmail.getUserLabelByName(REPLYTO_LABEL);
  if (!label) throw new Error('Gmail label not found: ' + REPLYTO_LABEL);
  var records = {}, start = 0;
  while (start < REPLYTO_MAX_THREADS) {
    var threads = gmail.search('label:"' + REPLYTO_LABEL + '"', start, Math.min(REPLYTO_PAGE, REPLYTO_MAX_THREADS - start));
    if (!threads.length) break;
    for (var t = 0; t < threads.length; t++) {
      var messages = threads[t].getMessages();
      for (var m = 0; m < messages.length; m++) {
        var msg = messages[m];
        var headers = { 'List-URL': msg.getHeader('List-URL'), 'List-Id': msg.getHeader('List-Id') };
        var from = parseAddress(msg.getFrom());
        var record = {
          publication_url: publicationFromMessage(headers, msg.getBody()),
          sender_name: from.name,
          reply_to: parseAddress(msg.getReplyTo()).address,
          message_url: threads[t].getPermalink()
        };
        var key = record.publication_url + '|' + record.reply_to.toLowerCase();
        var at = msg.getDate().getTime();
        if (!records[key] || records[key].at < at) records[key] = { at: at, record: record };
      }
    }
    start += threads.length;
  }
  return Object.keys(records).sort().map(function (k) { return records[k].record; });
}

/** Entry point: run this function from the Apps Script editor. */
function exportSubstackReplyTo() {
  var records = collectReplyTo(GmailApp);
  var name = 'substack-replyto-' + Utilities.formatDate(new Date(), 'UTC', "yyyyMMdd'T'HHmmss'Z'") + '.json';
  var file = DriveApp.createFile(name, JSON.stringify(records, null, 2), 'application/json');
  Logger.log('Wrote %s records to %s (%s). Records without publication_url or reply_to are kept; the importer reports them.',
             records.length, name, file.getUrl());
  return records.length;
}
