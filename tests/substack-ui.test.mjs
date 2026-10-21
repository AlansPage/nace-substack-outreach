import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const context = { URL };
vm.runInNewContext(fs.readFileSync('substack_outreach/ui.js', 'utf8'), context);
const C = context.SubstackUIHelpers;

test('Substack safely renders source text and rejects executable links', () => {
  assert.equal(C.safeURL('javascript:alert(1)'), '');
  assert.equal(C.safeURL('https://user:pass@example.com'), '');
  assert.ok(!C.link('javascript:alert(1)', '<img src=x onerror=alert(1)>').includes('<img'));
  const rendered = C.previewHTML('<script>alert(1)</script> https://nace.ai/developers');
  assert.ok(!rendered.includes('<script>'));
  assert.ok(rendered.includes('href="https://nace.ai/developers"'));
});

test('article links stop before parentheses and em-dash prose', () => {
  const output = C.previewHTML('Your post (https://substack.com/@alreadydawn/p-197268804)—especially this detail');
  assert.ok(output.includes('href="https://substack.com/@alreadydawn/p-197268804"'));
  assert.ok(!output.includes('p-197268804)—especially"'));
});

test('draft indicator excludes greeting, link and signature; enforces hard max', () => {
  const email = n => 'Hi Alan,\n\n' + Array(n - 2).fill('word').join(' ') + '\n\nsecond\n\nthird\n\nExplore Drex: https://nace.ai/developers\n\nBest,\nAlex';
  assert.equal(C.wordCount(email(80)), 80);
  assert.ok(C.lengthLabel(email(111)).includes('OVER HARD LIMIT'));
  assert.ok(C.lengthLabel(email(59)).includes('outside target'));
});

test('standalone shell hosts the Substack panel and inlines its source', () => {
  const html = fs.readFileSync('index.template.html', 'utf8');
  assert.ok(html.includes('id="tab-substack"'));
  for (const marker of ['/*__SUBSTACK_CSS__*/', '/*__SUBSTACK_UI__*/']) assert.ok(html.includes(marker));
  // The UI boots on this event; without it the shell renders nothing.
  assert.ok(html.includes("detail: 'substack'"));
});


test('about-page proposal shows confirm controls only while unresolved and escapes routes', () => {
  const proposal = { status: 'proposed', contact_source_url: 'https://testing.example/about', collected_at: 'now',
    candidates: [{ route: '"><img src=x onerror=alert(1)>', type: 'email', found_as: 'visible text' }] };
  const html = C.proposalHTML(proposal);
  assert.ok(html.includes('CONTACT_PROPOSED_UNCONFIRMED'));
  assert.ok(html.includes('data-action="confirm-contact"') && html.includes('data-action="dismiss-contact"'));
  assert.ok(!html.includes('<img'));
  const done = C.proposalHTML({ ...proposal, status: 'confirmed', decided_by: 'Human' });
  assert.ok(!done.includes('confirm-contact') && done.includes('confirmed by Human'));
  assert.equal(C.proposalHTML(undefined), '');
});

test('hosted workspace shows who is signed in, escapes it, and offers sign-in when signed out', () => {
  assert.equal(C.userHTML(undefined), '');
  const chip = C.userHTML({ email: '<b>x@y.example</b>' });
  assert.ok(chip.includes('&lt;b&gt;x@y.example') && chip.includes('href="/api/auth/logout"'));
  assert.ok(C.signInHTML().includes('href="/api/auth/login"'));
  assert.ok(C.proposalHTML({ status: 'proposed', contact_source_url: 'https://a.example/about', candidates: [] }, 'me@x.example').includes('value="me@x.example"'));
});

test('contacted marker shows channel, who and the date, escaped', () => {
  assert.equal(C.contactedHTML(null), '');
  const html = C.contactedHTML({ event: 'Contacted', channel: 'LinkedIn', actor: '<i>alice@example.com</i>', at: '2026-10-20T09:00:00+00:00' });
  assert.ok(html.includes('LinkedIn') && html.includes('2026-10-20') && !html.includes('09:00'));
  assert.ok(html.includes('&lt;i&gt;alice@example.com'));
});

test('upload plan takes the Substack, Medium and Discord sheets and batches them', () => {
  const sample = JSON.parse(fs.readFileSync('tests/fixtures/all_channels_sample.json', 'utf8'));
  const plan = C.uploadPlan(sample);
  assert.deepEqual([...plan.skipped], ['Copy of hackernoon']);
  assert.deepEqual([...plan.tables.map(t => t.sheet)], ['Discord', 'mediun', 'substack']);
  const substack = plan.tables.find(t => t.sheet === 'substack');
  assert.equal(substack.rows, 3);
  assert.deepEqual([...substack.headers], sample.sheets.substack.headers);
  const small = C.uploadPlan(sample, 1500).tables.find(t => t.sheet === 'substack');
  assert.ok(small.batches.length > 1);
  assert.equal(small.batches.flat().length, 3);
  assert.equal(C.uploadPlan([{ url: 'https://a.substack.com', name: 'A' }]).tables[0].sheet, 'upload');
});

test('export names the file from the server and says when there are no drafts yet', () => {
  assert.equal(C.exportName('attachment; filename="substack-pilot-2026-10-20.csv"', 'x.csv'), 'substack-pilot-2026-10-20.csv');
  assert.equal(C.exportName(null, 'substack-pilot.csv'), 'substack-pilot.csv');
  assert.equal(C.exportName('attachment; filename="../../evil.csv"', 'safe.csv'), 'safe.csv');
  assert.ok(C.exportMessage('a.csv', '20', '0').includes('No drafts yet'));
  assert.ok(!C.exportMessage('a.csv', '20', '3').includes('No drafts yet'));
});

test('channel buttons show Substack, Medium and Discord with counts', () => {
  const html = C.channelButtonsHTML({ substack: 15617, medium: 183, discord: 615 }, 'medium');
  assert.ok(html.includes('data-channel="substack"') && html.includes('data-channel="discord"'));
  assert.ok(html.includes('aria-pressed="true">Medium<small>183</small>'));
  assert.equal(C.uploadPlan({ sheets: { Discord: { headers: ['name', 'invite_url'], rows: [{ name: 'x', invite_url: 'https://discord.gg/x' }] } } }).tables.length, 1);
});
