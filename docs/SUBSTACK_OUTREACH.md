# Local Substack research and outreach

The browser workspace talks only to a loopback Python worker. SQLite, imported sources, drafts, exports and keys stay outside `public/`. There is no sending endpoint, scheduled sender or hosted database; nothing in this repository is deployed anywhere.

## Install and run in PowerShell

Python 3.12+ and Node 18+ are sufficient. The worker and static build use the Python standard library; no pip packages, model SDK, API key or npm installation are needed for local operation. Node is needed only for the existing JavaScript tests.

```powershell
Set-Location 'C:\path\to\substack-outreach'
py -3.12 -m venv .venv-substack
Copy-Item .\substack_outreach\config.example.json .\substack.config.json
.\.venv-substack\Scripts\python.exe build.py
.\.venv-substack\Scripts\python.exe -m substack_outreach --config .\substack.config.json pilot --input '.\All channels .xlsx' --batch 20 --shortlist 100
.\.venv-substack\Scripts\python.exe -m substack_outreach --config .\substack.config.json serve
```

Open **http://127.0.0.1:8765/** in your browser. Stop the worker with Ctrl+C. Do not overwrite an existing local config when repeating these commands. Activation is unnecessary; all commands use the venv executable directly. Without a venv, replace it with `py -3.12`.

The importer searches `All channels (1).xlsx`, `All channels .xlsx` and `All channels.xlsx`, in that order. Set `input_path` in the local config or pass `--input` for a different location. Paths in the config are relative to the repository working directory. The supplied workbook has 15,617 data rows and these actual headers:

```text
name, author_name, author_handle, url, substack_profile, categories, best_rank,
free_subscribers, free_subscribers_label, subscribers_range, paid_tier_label,
bestseller_tier, language, has_podcast, payments_state, created_at, first_post_date,
hero_text, author_bio, pub_id, author_id
```

Every row, including duplicate source rows and extra fields, is retained in SQLite. IDs normalize Excel numeric/scientific notation, and deduplication uses publication ID or canonical publication URL. Conflicting previously separate identities are flagged, not silently merged. Missing values stay unknown. Subscriber counts are imported estimates; creation/first-post dates do not establish recent activity. Local triage uses descriptive metadata and explanatory signals, never subscriber counts. Strong technical, application and exploratory fits have different priorities.

The first run imports all rows locally, shortlists 100, and persists a 20-record pilot. It does **not** fetch 15,617 publications. Repeating the command resumes the same pilot without duplicate source rows or regenerated drafts. Increasing `--batch` extends the persisted pilot; it does not rotate silently. The fixture is separate from the pilot.

## Review workflow

1. Open a pilot publication. Verify its actual publication URL, public first name and explicit published business/contact/submission route. Store source URLs, or label directly user-supplied provenance. Do not derive a person's identity from a handle. Unknown names use a neutral mock/manual greeting and a review flag; live writing requires a confirmed name.
2. Supply permitted article text or configure publisher RSS permission. Preserve date, byline, completeness and provenance. Profile pages remain separate from publication domains. When the relationship is not established, approval is blocked.
3. Review the proposed evidence or enter a grounded angle. Select one article, a short exact excerpt, what the author said, the separate proposed Drex connection, an invitation format, and selected facts. The excerpt must occur in collected text after whitespace normalization. Record the evidence reviewer.
4. Generate a draft. In the default **mock** mode the UI prominently labels previews and blocks approval. Live generation requires the explicit settings below. A manually rewritten mock can be saved as human-authored; this removes only the mock block, not the evidence/fact/contact checks.
5. Edit and save, then resolve flags. Review and approval both require a named human and semantic attestation. Deterministic checks cannot establish the meaning or truth of arbitrary prose. Pending/expired facts, unsupported numeric claims, unconfirmed release wording, invalid excerpts, wrong links, missing contact provenance, prior contact or opt-outs block approval.
6. Copy or export for human use. **Record sent manually** records an action already taken elsewhere; it does not send. **Record reply** is also manual. Sent/replied draft snapshots are immutable. Saving or explicitly regenerating an approved draft invalidates its approval; versions, hashes and approval events remain in SQLite.

The interface includes the complete status vocabulary: Imported, Shortlisted, Needs research, Researched, Drafted, Reviewed, Approved, Sent, Replied, Skipped and Do not contact. Status transitions are actions with checks rather than an unrestricted dropdown. A skip requires a reason.

Research JSON is imported in the UI or with:

```powershell
py -3.12 -m substack_outreach --config .\substack.config.json research-import .\.local\research.json
py -3.12 -m substack_outreach --config .\substack.config.json process --batch 20
```

Example `.local/research.json` (replace all example values with supplied evidence; this is not research of an actual publication):

```json
{
  "publication_id": 200,
  "articles": [{
    "title": "Actual article title",
    "url": "https://publisher.example/p/article",
    "date": "2026-10-01T12:00:00Z",
    "byline": null,
    "text": "Paste only the accessible text needed for this research task.",
    "content_status": "excerpt",
    "source_kind": "manual",
    "source_url": "https://publisher.example/p/article",
    "permission_basis": "Explain why the user is supplying this text for this task",
    "membership_confirmed": true,
    "relationship_source": "https://publisher.example/archive"
  }]
}
```

Use `manual` or `user_export`. Import up to five articles at once. Dates may be null. The worker retains at most five articles with at most 16,000 characters each; truncated/full-feed-uncertain text is labelled an excerpt. `membership_confirmed` is a human assertion supported by `relationship_source`, not inferred from a Substack profile handle. Article text, exports and their instructions are untrusted data. The model has no tools and receives only the selected excerpt, not whole feeds.

## Optional permitted RSS collection

RSS existence is **not permission**. Record publisher terms, a licence or explicit permission for this automated research use. Do not use API-derived Substack data for unsolicited outreach, undocumented APIs, authentication bypasses or paywall workarounds. A source must have:

```json
{
  "research": {
    "source_kind": "publisher_rss",
    "automated_use_permitted": true,
    "permission_basis": "Specific documented permission for this automated use",
    "permission_source_url": "https://publisher.example/terms",
    "permission_review_date": "2026-12-01",
    "feed_url": "https://publisher.example/feed.xml"
  }
}
```

Save this through the UI or `update PUBLICATION_ID .local/permission.json`. Set `network_enabled: true` in your local config, then:

```powershell
py -3.12 -m substack_outreach --config .\substack.config.json process --collect --batch 20
```

### Default feed permission (team policy decision)

`default_feed_permission` in `substack.config.json` is `null` by default, and the code never fills it in. Setting it is a **team policy decision**, not a technical setting: an authorized person decides that the team's documented basis covers automated reading of public RSS for every imported Substack publication. Record that basis, a link to where it is written down, and a review date:

```json
{
  "default_feed_permission": {
    "permission_basis": "Team policy decision: who decided, what it covers, and why",
    "permission_source_url": "https://your-team.example/policy/substack-research",
    "permission_review_date": "2027-01-31"
  }
}
```

Exactly these three keys are accepted; the URL must be HTTPS and the date `YYYY-MM-DD`, or the worker refuses to start. When set, the collector treats it as the research permission for a publication that (1) came from the workbook with a `pub_id`, which identifies it as a Substack publication, (2) has a resolved `publication_url`, and (3) has no explicit `research` block. That includes custom domains: their feed is found through the existing publisher-linked RSS/Atom discovery. An explicit per-publication `research` block always wins, even an incomplete one, and the default never adds a `feed_url`. After the review date, collection stops with `SOURCE_PERMISSION_REVIEW_REQUIRED` until someone renews it. Collected articles carry the default's `permission_basis` and `permission_source_url` as their provenance. Set it back to `null` to withdraw it. Robots rules, rate limits, redirect refusal, the 2 MB cap and same-host checks apply unchanged.

For a supported `*.substack.com` publication, `/feed` is a candidate verified by RSS parsing. Custom domains use publisher-linked RSS/Atom discovery or an explicitly supplied feed. Cross-domain feeds/redirects require separate review and manual import in this version. The collector checks robots rules, rate limits, public HTTPS destinations and permission expiry. Missing/unavailable robots rules fail closed. HTTP 401/403/429 and redirects stop collection; bounded retries apply only to transient failures. No article-page scraper is included. Up to five recent accessible feed items are sorted by dates; unknown dates remain unknown. Parsed selected excerpts and robots rules are cached; whole feeds/pages are not persisted. Existing articles are reused on resume; deliberate refresh can be supplied through research import.

## Facts, launch status, model and sender

`substack_outreach/facts.json` is the single editable, versioned registry for company, launch, product, scoped benchmarks, offers and links. Every seed is **team-supplied and pending**, not independently verified. An authorized teammate must review an entry, set `approval_status: "approved"`, `approved_by` and an ISO `approved_at`, set an appropriate `expiry_or_review_date`, and increment `version`. Optional `email_phrase` is the compact approved wording covered by that same approval. Registry snapshots are hashed and saved in SQLite. A changed registry invalidates outstanding approval, preserving the text for review.

`launch_status` is explicit: `unconfirmed`, `scheduled_confirmed` or `released_confirmed`. The launch date never changes release status. Any supplied launch date lives only in your local facts registry. The style reference is not approval of launch wording. By default emails omit release timing.

Writing instructions live in `substack_outreach/writing_instructions.txt`. Style version 2 applies only to new drafts and explicit regeneration: 60–90 body words, hard maximum 110, excluding greeting, link line and signature. Exactly three short paragraphs, one article detail, one use-case sentence, one easy invitation, approved API credits/technical answers, one developer link after the body, and at most one relevant approved benchmark. The model returns text and claim IDs; safe HTML is generated locally. URLs are preserved in plain-text exports. Failed email checks trigger at most one revision; unresolved problems remain visible. Research notes/flags never enter the recipient-facing email.

The live adapter uses OpenAI Responses with strict JSON-schema output and `store: false`. Official documentation checked during implementation:

- [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Responses migration and storage configuration](https://developers.openai.com/api/docs/guides/migrate-to-responses)

No model is silently selected. In ignored `substack.config.json`, choose a model supporting Responses structured outputs and set:

```json
{
  "provider": "openai",
  "model": "YOUR_ACCOUNT_ACCESSIBLE_MODEL",
  "allow_paid_calls": true,
  "budget_usd_per_run": 1.00,
  "input_usd_per_million": 0.0,
  "output_usd_per_million": 0.0,
  "max_requests_per_run": 10,
  "max_retries": 2,
  "max_output_tokens": 1800,
  "sender": {"name": "Your Name", "company": "Nace AI", "email": "you@example.com"}
}
```

Replace the zero pricing placeholders with current official rates for your selected model. Zero/missing prices block calls. Setting `allow_paid_calls: true` is the operator's explicit authorization; do so only after deciding the budget. Every attempted request, retry and revision counts toward request and conservative spending reservations, including ambiguous failures. Reservations are not billed cost. Actual returned token usage is recorded; calculated cost is labelled an estimate, while billed cost stays unknown if the provider does not report it. A run log lacking `finished_at` indicates an interrupted run. Repeating completed generation reuses its saved draft; explicit regeneration creates a new version and may reuse the model cache.

Set the key only in the worker's process environment. This PowerShell prompt avoids putting the key into command history:

```powershell
$substackKey = Read-Host 'OpenAI API key' -AsSecureString
$substackPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($substackKey)
try { $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($substackPtr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($substackPtr) }
py -3.12 -m substack_outreach --config .\substack.config.json serve
Remove-Item Env:\OPENAI_API_KEY
```

To change the sender, set that person's confirmed name and actual email together in `sender`; no title/email is inferred. Restart the worker after config changes. The existing Drex classifier adapter remains untouched and is not a writing dependency.

## Shared contact history

Author IDs and explicit profile links group publications. An explicit contact route or manually supplied identity alias can connect records. Approval is blocked when that author/contact already has an approved draft, any recorded previous contact, or an opt-out. Suppression applies across linked publications.

This worker cannot know that two different handles on different channels are the same person without a user-supplied mapping. Use **Previous contact & shared history**, or import a JSON list with `publication_id`, `channel`, `event`, `source`, and optional `at`, `note`, `identity_alias`. Do this before approving a contact researched through another channel. It imports historical records, not messages:

```powershell
py -3.12 -m substack_outreach --config .\substack.config.json contact-import .\.local\contact-history.json
```

## Exports, fixtures and observability

```powershell
py -3.12 -m substack_outreach --config .\substack.config.json export --format csv --scope pilot --out .\.local\pilot.csv
py -3.12 -m substack_outreach --config .\substack.config.json export --format json --scope pilot --out .\.local\pilot.json
py -3.12 -m substack_outreach --config .\substack.config.json metrics
py -3.12 -m substack_outreach --config .\substack.config.json fixture
py -3.12 -m substack_outreach --config .\substack.config.json previews
```

Exports contain publication, author, publication URL, article URL, excerpt, angle, contact and source, subject/body, owner, status and flags. CSV values are guarded against formula injection. Draft records include safe HTML and exact evidence/claim snapshots. Do not export private data into `public/`; the CLI rejects it.

`previews` writes `.local/substack/fixture-previews.json` and a clickable HTML companion. All three variants use **the same Alan example** and only the user-supplied summary, never fetched text or invented recipients. The article title is a supplied description, the date/contact/publication domain remain unknown, and the fit remains exploratory. The fixture cannot be approved. Generating the preview artifact does not overwrite any edited draft. Select **All imported + fixture** and search `user-supplied fixture` to review it in the UI.

Metrics report imported/shortlisted publications, research completion and failed attempts, draft versions, currently approved drafts, skip reasons and recent run usage. A fixture contributes one publication to overall counts but zero pilot records. No costs, sends, replies or conversion outcomes are fabricated.

## Validation and limits

```powershell
py -3.12 -m unittest discover -s tests -p test_substack.py -v
npm test
py -3.12 build.py
git diff --check
```

The pilot ran locally with 15,617 source rows, 100 shortlisted publications and a 20-publication pilot. No live publisher retrieval or paid model calls were made; the 20 records need permitted article research. Tests cover mapping/deduplication, profiles, missing identity/contact data, RSS/Atom excerpts, resume, evidence and claim failures, approval/version changes, cross-channel history, suppression, exports, safe links/rendering, body length, one-revision limits, and the standalone page shell.

Semantic correctness, source permissions, public identity, whether a paraphrase is fair, and outreach suitability still require human review. Official API schemas were checked, but live model access/pricing and real publisher feeds have not been exercised. The worker is for one trusted local operator: loopback binding, Host/Origin checks, no CORS and a per-session mutation token; it is not an Internet-facing multi-user service. Static builds remain independent of the worker/database/API key.
