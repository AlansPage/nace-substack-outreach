import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync('scripts/gmail_replyto.gs', 'utf8');
const threads = JSON.parse(fs.readFileSync('tests/fixtures/gmail_threads.json', 'utf8'));

// A read-only stand-in for GmailApp built from the saved fixture.
function fakeGmail(label = 'Substack') {
  const searches = [];
  return {
    searches,
    getUserLabelByName: name => (name === label ? { name } : null),
    search: (query, start, max) => {
      searches.push([query, start, max]);
      return threads.slice(start, start + max).map(t => ({
        getPermalink: () => t.permalink,
        getMessages: () => t.messages.map(m => ({
          getFrom: () => m.from, getReplyTo: () => m.replyTo, getBody: () => m.body,
          getDate: () => new Date(m.date), getHeader: name => m.headers[name] || '',
        })),
      }));
    },
  };
}

function load() {
  const context = {};
  vm.runInNewContext(source, context);
  return context;
}

test('Gmail reply-to export reads one label and keeps provenance for every record', () => {
  const gs = load();
  const gmail = fakeGmail();
  const records = JSON.parse(JSON.stringify(gs.collectReplyTo(gmail)));
  assert.deepEqual(gmail.searches[0], ['label:"Substack"', 0, 100]);
  assert.deepEqual(records, [
    { publication_url: 'https://leeletter.example', sender_name: 'Lee', reply_to: 'lee@leeletter.example', message_url: 'https://mail.google.com/mail/u/0/#all/t-body' },
    { publication_url: 'https://patnotes.substack.com', sender_name: 'Pat Writer', reply_to: 'pat@writer.example', message_url: 'https://mail.google.com/mail/u/0/#all/t-list-id' },
    { publication_url: 'https://testing.example', sender_name: 'Sam Example', reply_to: 'reply+abc@testing.example', message_url: 'https://mail.google.com/mail/u/0/#all/t-list-url' },
    { publication_url: 'https://testing.example', sender_name: 'Sam Example', reply_to: 'reply+newer@testing.example', message_url: 'https://mail.google.com/mail/u/0/#all/t-list-url' },
    { publication_url: '', sender_name: 'Platform', reply_to: '', message_url: 'https://mail.google.com/mail/u/0/#all/t-unknown' },
  ]);
});

test('Gmail reply-to export refuses a missing label and never touches platform hosts', () => {
  const gs = load();
  assert.throws(() => gs.collectReplyTo(fakeGmail('Other')), /label not found: Substack/);
  assert.equal(gs.publicationOrigin('https://substack.com/@someone'), '');
  assert.equal(gs.publicationOrigin('http://insecure.example/p/x'), '');
  assert.deepEqual(JSON.parse(JSON.stringify(gs.parseAddress('"Q, R" <q@r.example>'))), { name: 'Q, R', address: 'q@r.example' });
  // The script only reads: no send, label, trash or modify calls exist in its source.
  for (const call of ['sendEmail', 'createDraft', 'moveToTrash', 'addLabel', 'removeLabel', 'markRead', 'reply(', 'forward(']) {
    assert.ok(!source.includes(call), call);
  }
});
