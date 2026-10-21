(function () {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const safeURL = value => { try { const u = new URL(value); return ['http:', 'https:'].includes(u.protocol) && !u.username && !u.password ? u.href : ''; } catch { return ''; } };
  const link = (url, text) => safeURL(url) ? `<a href="${esc(safeURL(url))}" target="_blank" rel="noopener noreferrer">${esc(text || url)}</a>` : esc(text || 'Unknown');
  const wordCount = value => { const parts = value.trim().split('\n\n'); const body = parts.length >= 4 ? parts.slice(1, -2).join(' ') : ''; return body.trim() ? body.trim().split(/\s+/u).length : 0; };
  const lengthLabel = value => { const n = wordCount(value); return `${n} body words · target 60–90 · maximum 110${n > 110 ? ' · OVER HARD LIMIT' : n < 60 || n > 90 ? ' · outside target' : ''}`; };
  const previewHTML = value => value.split(/(https?:\/\/[^\s<>"()—]+)/g).map(s => {
    const u = s.startsWith('http') ? s.replace(/[.,;)]+$/, '') : '';
    return safeURL(u) ? link(u, u) + esc(s.slice(u.length)) : esc(s);
  }).join('').replace(/\n/g, '<br>');
  // An about-page proposal stays separate from the contact route until a named human confirms it.
  const proposalHTML = (proposal, reviewer = '') => {
    if (!proposal) return '';
    const source = link(proposal.contact_source_url, 'about page');
    if (proposal.status === 'none_found') return `<p class="ss-note">No contact found on the ${source} (${esc(proposal.collected_at)}).</p>`;
    if (proposal.status === 'failed') return `<p class="ss-note">About page not read: ${esc(proposal.error)}. Retry with collect-contacts --retry-failed.</p>`;
    if (proposal.status !== 'proposed') return `<p class="ss-note">About-page proposal ${esc(proposal.status)} by ${esc(proposal.decided_by)} · ${source}</p>`;
    const options = proposal.candidates.map(c => `<option value="${esc(c.route)}">${esc(c.route)} · ${esc(c.type)} · ${esc(c.found_as)}</option>`).join('');
    return `<div class="ss-proposal"><p class="ss-warning">CONTACT_PROPOSED_UNCONFIRMED · found on the ${source}. Blocks approval until confirmed or dismissed.</p>`
      + `<div class="ss-form-grid"><label class="ss-field">Proposed route<select id="ss-proposal-route">${options}</select></label>`
      + `<label class="ss-field">Route type<select id="ss-proposal-kind">${['contact', 'business', 'submission', 'sponsorship'].map(k => `<option>${k}</option>`).join('')}</select></label>`
      + `<label class="ss-field">Confirmed by<input id="ss-proposal-reviewer" value="${esc(reviewer)}"></label></div>`
      + `<p><button data-action="confirm-contact">Confirm proposed contact</button> <button data-action="dismiss-contact">Dismiss proposal</button></p></div>`;
  };
  // Hosted workspace: who is signed in, or the way in. Local mode has no user and shows nothing.
  const userHTML = user => user ? `<span class="ss-chip">Signed in as ${esc(user.email)} · <a href="/api/auth/logout">Sign out</a></span>` : '';
  // Shared "already contacted" marker: shown in the list and on the writer's panel for every teammate.
  const contactedHTML = c => c ? `<span class="ss-contacted">✓ ${esc(c.event || 'Contacted')} · ${esc(c.channel)} · ${esc(c.actor || 'not recorded who')} · ${esc(String(c.at || '').slice(0, 10))}</span>` : '';
  const signInHTML = () => 'Sign in to open the shared workspace. <a href="/api/auth/login">Sign in</a>';
  // Upload: accept the all-sheets JSON export, {headers, rows}, or a plain array of rows. Only tables with
  // url/pub_id columns are Substack publications; the rest are reported as skipped. Batches stay well under
  // the 4.5 MB request cap.
  const uploadPlan = (data, maxChars = 1000000, maxRows = 5000) => {
    const tables = Array.isArray(data) ? { upload: { rows: data } } : data && data.sheets ? data.sheets : { upload: data || {} };
    const plan = { tables: [], skipped: [] };
    for (const [sheet, table] of Object.entries(tables)) {
      const rows = Array.isArray(table && table.rows) ? table.rows : [];
      const headers = Array.isArray(table.headers) ? table.headers : [...new Set(rows.flatMap(r => Object.keys(r || {})).filter(k => k !== '_row'))];
      if (!rows.length || !(headers.includes('url') || headers.includes('pub_id'))) { plan.skipped.push(sheet); continue; }
      const batches = [];
      let batch = [], size = 0;
      for (const row of rows) {
        const length = JSON.stringify(row).length;
        if (batch.length && (size + length > maxChars || batch.length >= maxRows)) { batches.push(batch); batch = []; size = 0; }
        batch.push(row); size += length;
      }
      if (batch.length) batches.push(batch);
      plan.tables.push({ sheet, headers, rows: rows.length, batches });
    }
    return plan;
  };
  const exportName = (disposition, fallback) => (String(disposition || '').match(/filename="([^"\/]+)"/) || [])[1] || fallback;
  const exportMessage = (name, rows, drafts) => `Exported ${name}: ${rows ?? '?'} rows, ${drafts ?? '?'} with an email draft. Check your Downloads folder.`
    + (Number(drafts) === 0 ? ' No drafts yet: rows still need research or a generated draft, so subject and body columns are empty.' : '');
  globalThis.SubstackUIHelpers = { esc, safeURL, link, wordCount, lengthLabel, previewHTML, proposalHTML, userHTML, signInHTML, contactedHTML, uploadPlan, exportName, exportMessage };
  if (typeof document === 'undefined') return;
  const root = document.getElementById('tab-substack');
  if (!root) return;
  const $ = selector => root.querySelector(selector);
  let boot = null, selected = null, scope = 'pilot', offset = 0, dirty = false, busy = false;
  const field = (id, label, value = '', area = false) => `<label class="ss-field">${esc(label)}${area ? `<textarea id="ss-${id}" rows="4">${esc(value)}</textarea>` : `<input id="ss-${id}" value="${esc(value)}">`}</label>`;
  const select = (id, label, options, value) => `<label class="ss-field">${esc(label)}<select id="ss-${id}">${options.map(o => `<option${o === value ? ' selected' : ''}>${esc(o)}</option>`).join('')}</select></label>`;
  const checked = value => value ? ' checked' : '';
  const val = id => $(`#ss-${id}`)?.value || '';
  const tick = id => !!$(`#ss-${id}`)?.checked;
  function notice(message, error = false) { const el = $('#ss-message'); if (el) { el.textContent = message; el.className = error ? 'ss-message ss-error' : 'ss-message'; } }
  async function api(path, data) {
    const response = await fetch('/api/substack/' + path, data === undefined ? { credentials: 'same-origin' } : { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-Local-Token': boot.token }, body: JSON.stringify(data) });
    const result = await response.json();
    if (!response.ok) throw Object.assign(new Error(result.error || `Worker returned ${response.status}`), { status: response.status });
    return result;
  }
  function skeleton() {
    root.innerHTML = `<header class="ss-hero"><div><p class="ss-eyebrow">NACE AI · LOCAL RESEARCH WORKSPACE</p><h1>Substack outreach</h1><p>Evidence first. A short, personal invitation. A human decision.</p></div><span class="ss-chip">Manual sending only</span></header>
      <div id="ss-message" class="ss-message" role="status" aria-live="polite">Connecting to local worker…</div>
      <div id="ss-stats" class="ss-stats"></div>
      <div class="ss-toolbar"><label>Queue <select id="ss-scope"><option value="pilot">20-publication pilot</option><option value="shortlisted">Shortlist</option><option value="all">All imported + fixture</option></select></label><label>Status <select id="ss-status"><option value="">All statuses</option></select></label><label class="ss-search">Search <input id="ss-search" type="search" placeholder="Publication, author, topic"></label><button id="ss-refresh">Refresh</button><button data-export="csv">Export CSV</button><button data-export="json">Export JSON</button><button id="ss-contacts">Propose contacts from about pages</button><button id="ss-upload">Upload JSON</button><input id="ss-upload-file" type="file" accept=".json,application/json" hidden></div>
      <div class="ss-layout"><aside class="ss-queue" aria-label="Publication queue"><div id="ss-list"></div><div class="ss-pagination"><button id="ss-prev">Previous</button><span id="ss-count"></span><button id="ss-next">Next</button></div></aside><section id="ss-detail" aria-label="Publication review"><div class="ss-empty"><h2>Choose a publication</h2><p>Review its sources, verify the name and contact route, then draft.</p><p>Subscriber counts are imported estimates. Metadata matches are only triage signals.</p></div></section></div>`;
  }
  async function start() {
    if (boot) return;
    skeleton();
    try {
      boot = await api('bootstrap');
      $('#ss-scope').options[0].textContent = `${boot.batch_size}-publication pilot`;
      root.querySelector('.ss-hero').insertAdjacentHTML('beforeend', userHTML(boot.user));
      $('#ss-contacts').disabled = !(boot.collect_contacts && boot.network_enabled);
      $('#ss-contacts').title = $('#ss-contacts').disabled ? 'Needs collect_contacts and network_enabled set to true in the local config' : `Reads up to ${boot.batch_size} pilot about pages`;
      $('#ss-status').insertAdjacentHTML('beforeend', boot.statuses.map(s => `<option>${esc(s)}</option>`).join(''));
      root.addEventListener('click', handleClick);
      root.addEventListener('change', e => { if (['ss-scope', 'ss-status'].includes(e.target.id)) { scope = val('scope'); offset = 0; refresh().catch(report); } });
      root.addEventListener('input', e => { if (['ss-subject', 'ss-body'].includes(e.target.id)) { dirty = true; $('#ss-length').textContent = lengthLabel(val('body')); $('#ss-rendered').innerHTML = previewHTML(val('body')); } });
      $('#ss-upload-file').addEventListener('change', e => { const file = e.target.files[0]; e.target.value = ''; if (file) upload(file).catch(report); });
      $('#ss-search').addEventListener('keydown', e => { if (e.key === 'Enter') { offset = 0; refresh().catch(report); } });
      await refresh();
      notice(`${boot.hosted ? 'Shared workspace connected' : 'Local worker connected'} · ${boot.provider === 'mock' ? 'MOCK mode: previews cannot be approved' : 'Writing provider: ' + boot.provider} · Sender: ${boot.sender.name}, ${boot.sender.email} · Network ${boot.network_enabled ? 'permission-gated' : 'disabled'}`);
    } catch (error) {
      boot = null;
      if (error.status === 401) { $('#ss-message').innerHTML = signInHTML(); return; }
      const local = ['localhost', '127.0.0.1'].includes(location.hostname);
      notice(local ? `Local worker unavailable. Run python -m substack_outreach serve. ${error.message}` : `Workspace unavailable: ${error.message}`, true);
    }
  }
  const report = error => notice(error.message || String(error), true);
  async function refresh() {
    const [queue, metrics] = await Promise.all([api(`queue?scope=${encodeURIComponent(scope)}&search=${encodeURIComponent(val('search'))}&status=${encodeURIComponent(val('status'))}&offset=${offset}`), api('metrics')]);
    $('#ss-stats').innerHTML = [['Imported', metrics.imported_publications], ['Shortlisted', metrics.shortlisted_publications], ['Researched', metrics.research_completed], ['Draft versions', metrics.drafts_generated], ['Approved', metrics.drafts_approved]].map(([label, n]) => `<div><strong>${esc(n)}</strong><span>${label}</span></div>`).join('');
    $('#ss-list').innerHTML = queue.items.map(p => `<button class="ss-row${selected?.id === p.id ? ' ss-selected' : ''}" data-pub="${p.id}"><span class="ss-row-title">${esc(p.publication || 'Unnamed publication')}</span><span>${esc(p.author_name || 'Author unknown')}</span><span class="ss-row-meta">${esc(p.fit_category)} · ${esc(p.status)}</span>${p.author_publications > 1 ? `<span class="ss-warning">${p.author_publications} publications share this author</span>` : ''}${p.do_not_contact ? '<span class="ss-warning">Do not contact</span>' : ''}${contactedHTML(p.contacted)}</button>`).join('') || '<p class="ss-empty">No records in this queue. Run the pilot command to import the workbook.</p>';
    $('#ss-count').textContent = `${queue.total ? offset + 1 : 0}–${Math.min(offset + queue.items.length, queue.total)} / ${queue.total}`;
    $('#ss-prev').disabled = offset === 0;
    $('#ss-next').disabled = offset + 100 >= queue.total;
  }
  async function exportFile(format) {
    notice(`Preparing the ${format.toUpperCase()} export…`);
    const response = await fetch(`/api/substack/export?format=${format}&scope=${encodeURIComponent(scope)}`, { credentials: 'same-origin' });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(`Export failed: ${detail.error || 'worker returned ' + response.status}`);
    }
    const blob = await response.blob();
    const name = exportName(response.headers.get('Content-Disposition'), `substack-${scope}.${format}`);
    // The link must be in the page for Chrome, Edge and Firefox to start the download; keep the blob alive long enough.
    const href = URL.createObjectURL(blob), anchor = Object.assign(document.createElement('a'), { href, download: name });
    anchor.style.display = 'none';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(href), 60000);
    notice(exportMessage(name, response.headers.get('X-Export-Rows'), response.headers.get('X-Export-Drafts')));
  }
  async function upload(file) {
    if (busy) return;
    busy = true;
    try {
      notice(`Reading ${file.name}…`);
      const plan = uploadPlan(JSON.parse(await file.text()));
      if (!plan.tables.length) throw new Error(`No Substack table found in ${file.name} (needs url or pub_id columns). Skipped: ${plan.skipped.join(', ') || 'none'}`);
      let inserted = 0, duplicates = 0, suppressed = 0;
      for (const table of plan.tables) {
        let offset = 0;
        for (const [i, rows] of table.batches.entries()) {
          notice(`Uploading ${table.sheet}: part ${i + 1} of ${table.batches.length} (${table.rows} rows). Keep this tab open.`);
          const result = await api('upload', { sheet: table.sheet, headers: table.headers, rows, offset, last: i === table.batches.length - 1 });
          inserted += result.inserted || 0; duplicates += result.duplicates || 0; suppressed += (result.dnc_suppressed || []).length;
          offset += rows.length;
        }
      }
      await refresh();
      notice(`Upload done: ${inserted} new publications, ${duplicates} already known (their contacted marks, drafts and status are kept), ${suppressed} on the do-not-contact list.${plan.skipped.length ? ' Skipped (not Substack tables): ' + plan.skipped.join(', ') + '.' : ''}`);
    } finally { busy = false; }
  }
  async function open(id) {
    if (dirty) throw new Error('Save the edited draft before switching publications.');
    selected = await api('publication/' + id);
    renderDetail();
  }
  function renderDetail() {
    const p = selected, d = p.draft, e = p.evidence || p.evidence_candidate || {}, v = d?.validation;
    $('#ss-detail').innerHTML = `<div class="ss-detail-head"><p class="ss-eyebrow">${esc(p.status)} · RECORD ${p.id}</p><h2>${esc(p.publication || 'Unnamed publication')}</h2><p>${esc(p.author_name || 'Author unknown')} · ${link(p.publication_url, 'Publication')} · ${link(p.profile_url, 'Substack profile')}</p><div class="ss-tags"><span class="ss-chip">${esc(p.fit_category)}</span>${p.source_kind === 'user_fixture' ? '<span class="ss-chip ss-warning">USER-SUPPLIED FIXTURE · NOT FETCHED</span>' : ''}</div><p>${p.fit_reasons.map(esc).join(' ')}</p></div>
      <div class="ss-card"><h3>Recipient & contact</h3><div class="ss-contacted-box">${contactedHTML(p.previous_contact[0])}<label><input id="ss-contacted-check" type="checkbox"> Contacted this writer (email, X, LinkedIn, Substack DM, …). Teammates will see it and drafting to this writer is blocked.</label><div class="ss-form-grid">${select('contacted-channel', 'Channel', boot.contact_channels || ['Email'], 'Email')}${field('contacted-link', 'Link to the message or profile (optional, https)')}</div><button data-action="contacted">Save contacted</button></div><div class="ss-form-grid">${field('author', 'Public author name', p.author_name)}${field('first', 'Confirmed public first name (leave blank if unknown)', p.confirmed_first_name)}${field('name-source', 'Name source URL or user-supplied provenance', p.name_source)}${field('owner', 'Owner', p.owner)}${field('contact', 'Published business/contact/submission route', p.contact_route)}${field('contact-source', 'Contact source URL or user-supplied provenance', p.contact_source_url)}${select('contact-kind', 'Route type', ['', 'business', 'contact', 'submission', 'user_supplied', 'sponsorship', 'reply_to'], p.contact_kind)}${select('fit', 'Fit category', boot.fits, p.fit_category)}</div><label><input id="ss-contact-confirmed" type="checkbox"${checked(p.contact_confirmed)}> I verified this published route or supplied it directly.</label>${proposalHTML(p.contact_route_proposed, boot.user?.email || '')}<p><button data-action="metadata">Save recipient & owner</button> <button data-action="dnc" class="ss-danger">Do not contact author</button></p><p class="ss-note">Sponsorship routes are kept separate from editorial pitches. Confirmed identity links and contacts apply across publications and channels.</p><details><summary>Previous contact (${p.previous_contact.length}) & shared history</summary><pre>${esc(JSON.stringify(p.previous_contact, null, 2))}</pre><div class="ss-form-grid">${field('history-channel', 'Channel (email, Reddit, X, …)')}${field('history-source', 'Source: message URL or supplied record')}${field('history-alias', 'Optional explicit shared identity key')}${field('history-note', 'Contact note')}</div><button data-action="history">Record previous contact</button></details></div>
      <div class="ss-card"><h3>Article evidence</h3>${p.articles.length ? p.articles.map(a => `<div class="ss-source"><strong>${link(a.url, a.title || 'Untitled article')}</strong><p>${esc(a.date || 'Date unknown')} · ${esc(a.content_status)} · ${esc(a.source_kind)} · ${esc(a.byline || 'Byline unknown')}</p><blockquote>${esc(e.article_url === a.url ? e.supporting_excerpt : a.text.slice(0, 280))}</blockquote><details><summary>Collected text & provenance</summary><pre>${esc(a.text)}</pre><p>Retrieved: ${esc(a.retrieved_at)} · ${esc(a.permission_basis)} · ${link(a.source_url, 'Source')}</p></details></div>`).join('') : '<p>No collected article. Import supplied text or configure permitted publisher RSS.</p>'}
      <details><summary>Import article text or research export</summary><p>Paste a JSON array of up to five articles using the documented schema, or import the JSON file from the CLI. No article is fetched by this form.</p>${field('articles-json', 'Articles JSON', '[\n  {\n    "title": "", "url": "", "date": null, "byline": null,\n    "text": "", "content_status": "excerpt",\n    "source_kind": "manual", "source_url": "",\n    "permission_basis": "", "membership_confirmed": false,\n    "relationship_source": ""\n  }\n]', true)}<button data-action="research">Import research</button></details>
      <details><summary>Source permission & RSS settings</summary><p>Feed availability alone does not establish permission. The CLI collector also requires network_enabled=true.</p>${field('research-json', 'Permission record JSON', JSON.stringify(p.research && Object.keys(p.research).length ? p.research : { source_kind: 'publisher_rss', automated_use_permitted: false, permission_basis: '', permission_source_url: '', permission_review_date: '', feed_url: '' }, null, 2), true)}${field('publication-url', 'Resolved publication URL (not profile)', p.publication_url)}${field('publication-source', 'Evidence supporting publication URL', p.publication_url_source)}<button data-action="permission">Save source settings</button></details>
      <details${p.articles.length ? ' open' : ''}><summary>Personalization record · separate from email</summary><div class="ss-form-grid">${select('article-choice', 'Selected article', ['', ...p.articles.map(a => a.url)], e.article_url)}${select('ask-format', 'Invitation format', Object.keys(boot.asks), e.ask_format)}${field('excerpt', 'Short supporting excerpt (must occur in collected text)', e.supporting_excerpt, true)}${field('point', 'What the author actually said', e.specific_point, true)}${field('angle', 'Proposed Drex connection (keep exploratory angles hypothetical)', e.drex_connection, true)}${field('ask', 'One easy-to-answer invitation question', e.suggested_ask, true)}${field('evidence-reviewer', 'Evidence reviewed by', e.evidence_reviewed_by)}${field('benchmark-relevance', 'Why a selected benchmark is directly relevant', e.benchmark_relevance)}</div><div class="ss-facts">${boot.facts.entries.map(f => `<label><input type="checkbox" name="ss-claim" value="${esc(f.id)}"${checked(e.selected_claim_ids?.includes(f.id))}> <strong>${esc(f.id)}</strong> · ${esc(f.approval_status)}<span>${esc(f.email_phrase || f.text)}</span></label>`).join('')}</div><button data-action="evidence">Save evidence</button></details></div>
      <div class="ss-card"><h3>Email draft</h3><p class="ss-note">60–90 body words, maximum 110. Three short paragraphs. Greeting, developer link and signature are excluded. New style applies on generation; saving preserves your text.</p>${d?.provider === 'mock' ? '<p class="ss-preview-warning">MOCK PREVIEW — pending facts may appear. Not send-ready.</p>' : ''}${field('subject', 'Subject', d?.subject)}${field('body', 'Plain-text email (editable)', d?.plain_text_body, true)}<p id="ss-length" class="ss-length">${esc(lengthLabel(d?.plain_text_body || ''))}</p><label><input id="ss-human" type="checkbox"> I authored/reworked this as a human draft (removes mock mode; all evidence and fact checks still apply).</label><div class="ss-actions"><button data-action="edit"${!d ? ' disabled' : ''}>Save edits</button><button data-action="regenerate">${d ? 'Regenerate · invalidates approval' : 'Generate draft'}</button><button data-action="copy"${!d ? ' disabled' : ''}>Copy email</button></div><details><summary>Safe HTML preview</summary><div id="ss-rendered" class="ss-email">${previewHTML(d?.plain_text_body || '')}</div></details>
      <h4>Validation & facts used</h4><p>${esc(v?.method || 'Not validated')} · Human semantic review is required.</p><ul class="ss-flags">${(d?.review_flags || e.review_flags || ['NEEDS_RESEARCH']).map(f => `<li>${esc(f)}</li>`).join('')}</ul><p>Claim IDs: ${esc(d?.claim_ids_used?.join(', ') || 'None')}</p><p>Approved version: ${esc(d?.approved_hash ? d.version + ' · ' + d.approved_hash.slice(0, 16) : 'None')} ${esc(d?.approved_by || '')}</p>${field('reviewer', 'Reviewer', boot.user?.email || '')}
      <label><input id="ss-attest" type="checkbox"> I checked the article detail, fit, claims, launch wording, sender, one ask, source limitations and contact history. Automated checks are not a semantic guarantee.</label><div class="ss-actions"><button data-action="review">Mark reviewed</button><button data-action="approve">Approve current version</button><button data-action="sent">Record sent manually</button><button data-action="replied">Record reply</button></div>${field('skip-reason', 'Skip reason')}<button data-action="skip">Skip publication</button></div>
      <details class="ss-card"><summary>Original workbook fields (preserved)</summary><p>Subscriber counts are imported estimates, not independently verified. first_post_date is not recent activity.</p><pre>${esc(JSON.stringify(p.source, null, 2))}</pre></details>`;
    dirty = false;
    $('#ss-ask-format').addEventListener('change', () => {
      $('#ss-ask').value = boot.asks[val('ask-format')];
      // The sponsorship offer belongs to the sponsored-post ask only.
      const sponsorship = root.querySelector('input[name="ss-claim"][value="offer.sponsorship"]');
      if (sponsorship) sponsorship.checked = val('ask-format') === 'sponsored post';
    });
  }
  async function handleClick(event) {
    const button = event.target.closest('button');
    if (!button || busy) return;
    try {
      busy = true;
      if (button.dataset.pub) return await open(Number(button.dataset.pub));
      if (button.id === 'ss-refresh') return await refresh();
      if (button.id === 'ss-upload') { $('#ss-upload-file').click(); return; }
      if (button.id === 'ss-contacts') {
        notice('Reading about pages through the permission-gated collector…');
        const { results } = await api('collect-contacts', { batch: boot.batch_size });
        const count = key => results.filter(r => r.result === key).length;
        notice(`About pages: ${count('proposed')} with proposals, ${count('none_found')} without contacts, ${results.filter(r => r.error).length} failed. Nothing is confirmed until you confirm it.`);
        if (selected) { selected = await api('publication/' + selected.id); renderDetail(); }
        return await refresh();
      }
      if (button.id === 'ss-prev' || button.id === 'ss-next') { offset += button.id === 'ss-prev' ? -100 : 100; return await refresh(); }
      if (button.dataset.export) return await exportFile(button.dataset.export);
      if (!button.dataset.action || !selected) return;
      let action = button.dataset.action, data = {};
      if (action === 'copy') {
        await navigator.clipboard.writeText(`Subject: ${val('subject')}\n\n${val('body')}`);
        notice('Email copied. Review flags remain in the workspace. Nothing was sent.'); return;
      }
      if (dirty && !['edit', 'regenerate'].includes(action)) throw new Error('Save the edited email first so it is not lost.');
      if (action === 'metadata') { action = 'update'; data = { author_name: val('author'), confirmed_first_name: val('first'), name_source: val('name-source'), owner: val('owner'), contact_route: val('contact'), contact_source_url: val('contact-source'), contact_kind: val('contact-kind'), contact_confirmed: tick('contact-confirmed'), fit_category: val('fit') }; }
      else if (action === 'permission') { action = 'update'; data = { research: JSON.parse(val('research-json')), publication_url: val('publication-url'), publication_url_source: val('publication-source') }; }
      else if (action === 'research') data = { articles: JSON.parse(val('articles-json')) };
      else if (action === 'contacted') data = { confirmed: tick('contacted-check'), channel: val('contacted-channel'), link: val('contacted-link') };
      else if (['confirm-contact', 'dismiss-contact'].includes(action)) { data = { decision: action === 'confirm-contact' ? 'confirm' : 'dismiss', route: val('proposal-route'), contact_kind: val('proposal-kind'), reviewer: val('proposal-reviewer') }; action = 'contact_proposal'; }
      else if (action === 'evidence') {
        const a = selected.articles.find(a => a.url === val('article-choice'));
        if (!a) throw new Error('Select a collected article first.');
        data = { author_name: selected.author_name || '', name_source: selected.name_source || '', publication: selected.publication, publication_url: selected.publication_url, fit_category: selected.fit_category, article_title: a.title || '', article_url: a.url, article_date: a.date || '', specific_point: val('point'), supporting_excerpt: val('excerpt'), drex_connection: val('angle'), suggested_ask: val('ask'), ask_format: val('ask-format'), contact_route: selected.contact_route, contact_source_url: selected.contact_source_url, selected_claim_ids: [...root.querySelectorAll('input[name="ss-claim"]:checked')].map(x => x.value), evidence_reviewed_by: val('evidence-reviewer'), benchmark_relevance: val('benchmark-relevance'), ready_to_draft: true, review_flags: [] };
      } else if (action === 'edit') data = { subject: val('subject'), plain_text_body: val('body'), human_authored: tick('human') };
      else if (['review', 'approve'].includes(action)) data = { reviewer: val('reviewer'), semantic_review_confirmed: tick('attest') };
      else if (action === 'history') data = { channel: val('history-channel'), source: val('history-source'), identity_alias: val('history-alias'), note: val('history-note') };
      else if (['skip', 'dnc'].includes(action)) data = { reason: val('skip-reason') };
      notice(action === 'regenerate' ? 'Generating with the configured local worker adapter…' : 'Saving…');
      selected = await api(`publication/${selected.id}/${action}`, data);
      renderDetail(); await refresh(); notice(action === 'regenerate' ? 'New draft version saved. Prior approval is invalidated. Review all flags.' : 'Saved locally.');
    } catch (error) { report(error); }
    finally { busy = false; }
  }
  window.addEventListener('smap:tab', event => { if (event.detail === 'substack') start(); });
})();
