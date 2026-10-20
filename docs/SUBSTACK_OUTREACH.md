# Local Substack research and outreach

The browser workspace talks only to a loopback Python worker. SQLite, imported sources, drafts, exports and keys stay outside `public/`. There is no sending endpoint or scheduled sender. By default nothing is deployed anywhere; the optional hosted team workspace (see **Hosted team workspace**) runs the same code on Vercel with a shared Postgres database.

## Install and run in PowerShell

Python 3.12+ and Node 18+ are sufficient. The worker and static build use the Python standard library; no pip packages, model SDK, API key or npm installation are needed for local operation. Node is needed only for the existing JavaScript tests.

```powershell
Set-Location 'C:\path\to\substack-outreach'
py -3.12 -m venv .venv-substack
Copy-Item .\substack_outreach\config.example.json .\substack.config.json
Copy-Item .\substack_outreach\dnc.example.json .\substack_outreach\dnc.json
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

### Triage signals and the do-not-contact list

Local triage matches terms in the name, description, categories and author bio. Besides the longer phrases (AI engineering, AI agents, machine learning, LLM, …) it counts the bare terms `AI`, `A.I.`, `GPT`, `ChatGPT`, `ML`, `deep learning`, `agents`, `data science` and `automation`. `AI`, `A.I.`, `ML` and `GPT` are case-sensitive and must stand alone in ASCII text, so `OpenAI`, `HTML`, French `j'ai` and `100 ml` do not count, while `AIとWeb3` does. A term inside a longer match (`agents` in `AI agents`) is not counted twice. The Technology category adds +5 to the score of a publication that already has a topical signal; the category alone is never fit evidence.

`dnc_path` (default `substack_outreach/dnc.json`, gitignored) is the do-not-contact list for vendor-owned publications. When it is missing, the committed seed `substack_outreach/dnc.example.json` is used, and the import result names the list it used. Entries match exactly (case-insensitive) on `hosts` (publication host without `www.`), `author_names`, `names` or `pub_ids`. Every entry needs an `owner`, a `reason` and a `source` explaining why it is listed:

```json
{"entries": [{"owner": "OpenAI", "hosts": ["openaiglobalaffairs.substack.com"],
              "author_names": ["OpenAI Global Affairs"], "reason": "Vendor-owned publication",
              "source": "Workbook row: author_name 'OpenAI Global Affairs'"}]}
```

`import` and `pilot` apply the list on every run, including a resumed workbook, through the same suppression as **Do not contact author**: the author and every linked publication become `Do not contact` and leave the shortlist. A personal newsletter written by a vendor employee is not vendor-owned; do not list it unless the team decides so.

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

Writing instructions live in `substack_outreach/writing_instructions.txt`. Style version 2 applies only to new drafts and explicit regeneration: 60–90 body words, hard maximum 110, excluding greeting, link line and signature. Exactly three short paragraphs, one article detail, one use-case sentence, one easy invitation, the selected approved offer, one developer link after the body, and at most one relevant approved benchmark. The model returns text and claim IDs; safe HTML is generated locally. URLs are preserved in plain-text exports. Failed email checks trigger at most one revision; unresolved problems remain visible. Research notes/flags never enter the recipient-facing email.

Product and offer wording comes only from the registry. Paragraph 2 uses the `email_phrase` of `product.model` and `product.agents`; paragraph 3 states the selected approved offer. The mock writer reads the same phrases (pending ones appear only in mock previews and are flagged `CLAIM_NOT_APPROVED`), and the style reference at the end of `writing_instructions.txt` is a structural example with `[APPROVED …]` slots, not a source of facts. No product name or capability is hard-coded anywhere else.

Invitation formats are `review`, `technical walkthrough` (the default), `roundup inclusion`, `exploratory discussion` and `sponsored post`. The sponsorship offer (`offer.sponsorship`) belongs to the sponsored-post ask only. The writer never receives it for any other ask. A draft that uses it with another ask gets `SPONSORSHIP_OFFER_REQUIRES_SPONSORED_ASK`, and a sponsored ask without it gets `SPONSORED_ASK_REQUIRES_SPONSORSHIP_OFFER`. Sponsored drafts **cannot be approved yet**: they still fail `UNSUPPORTED_PROMISE_OR_PLACEHOLDER` (sponsor wording), `PRODUCT_USE_CASE_SUPPORT_OR_LINK_MISSING` (the API-credit and technical-answer offers are required) and, for a `sponsorship` route, `EDITORIAL_CONTACT_ROUTE_REQUIRED`. The live writers refuse them for the same reason. Lifting those checks for sponsored posts is a pending team decision.

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

### Local model (free)

`provider: "local"` writes with a model on your own machine through an OpenAI-compatible chat endpoint, such as Ollama's `/v1/chat/completions`. It uses the same strict JSON schema (sent as `response_format` `json_schema`), the same single revision and the same requirements: approved product, use-case, offer and link facts, a confirmed first name, and a named evidence reviewer. It needs no key, budget or pricing. `max_requests_per_run` still caps requests, and run usage records `provider: "local"` with token counts and no cost. `local_base_url` must be a loopback URL (`127.0.0.1`, `localhost` or `::1`); the worker refuses anything else at startup and never uses a proxy for these calls.

```powershell
ollama pull YOUR_LOCAL_MODEL
ollama serve
```

```json
{
  "provider": "local",
  "model": "YOUR_LOCAL_MODEL",
  "local_base_url": "http://127.0.0.1:11434"
}
```

Use a model that follows JSON-schema output. Output that does not match the schema, or a response cut off by `max_output_tokens`, stops generation with an error rather than saving a partial draft.

To change the sender, set that person's confirmed name and actual email together in `sender`; no title/email is inferred. Restart the worker after config changes. The existing Drex classifier adapter remains untouched and is not a writing dependency.

## Shared contact history

Author IDs and explicit profile links group publications. An explicit contact route or manually supplied identity alias can connect records. Approval is blocked when that author/contact already has an approved draft, any recorded previous contact, or an opt-out. Suppression applies across linked publications.

**Contacted checkbox.** Whenever you reach a writer on any channel (email, X, LinkedIn, Substack DM or other), tick **Contacted this writer** at the top of their **Recipient & contact** card, choose the channel, optionally paste an https link to the message or profile, and press **Save contacted**. The mark records who (the signed-in email on the hosted workspace, the configured sender name locally) and when, and shows as a green **✓ Contacted** badge in everyone's list. It also covers the writer's other publications and anyone sharing the same contact email. Drafting to that writer is then blocked for everyone (`Generation blocked … prior contact`, `PREVIOUS_CONTACT_REVIEW_REQUIRED`). Saving the same mark twice on one day records it once. Marks are history, so there is no untick; record a correction as a note on a new mark.

This worker cannot know that two different handles on different channels are the same person without a user-supplied mapping. Use **Previous contact & shared history**, or import a JSON list with `publication_id`, `channel`, `event`, `source`, and optional `at`, `note`, `identity_alias`. Do this before approving a contact researched through another channel. It imports historical records, not messages:

```powershell
py -3.12 -m substack_outreach --config .\substack.config.json contact-import .\.local\contact-history.json
```

## Contact routes from about pages and Gmail

Both sources only **propose** routes. Approval stays blocked until a named person checks each one.

### About-page proposals

Set `collect_contacts: true` and `network_enabled: true` in your local config, then run the command or use **Propose contacts from about pages** in the UI toolbar:

```powershell
py -3.12 -m substack_outreach --config .\substack.config.json collect-contacts --batch 20
py -3.12 -m substack_outreach --config .\substack.config.json collect-contacts --batch 20 --retry-failed
```

For pilot publications that have a resolved `publication_url` but no confirmed route, the collector reads `{publication_url}/about` with the same robots check, rate limit, redirect refusal, 2 MB cap and same-host rule as feed collection. It extracts `mailto:` links, visible email addresses, and X, LinkedIn and personal-site links from the visible page; scripts and embedded JSON are ignored. The result is stored in `contact_route_proposed` with the about URL as its source, and `contact_route` is never written. While a proposal is open, drafts carry `CONTACT_PROPOSED_UNCONFIRMED`, which blocks approval. In the publication's **Recipient & contact** card, choose a proposed route and its type, enter your name, and press **Confirm proposed contact** (or **Dismiss proposal**). Confirming runs the usual recipient validation and records the about page as `contact_source_url`. A robots refusal, HTTP barrier or oversized page is remembered as `failed`; `--retry-failed` tries those again. Pages with nothing usable are marked `none_found`.

### Gmail reply-to import

`scripts/gmail_replyto.gs` is a Google Apps Script you run yourself at script.google.com (new project, paste the file, run `exportSubstackReplyTo`). It only reads messages under one Gmail label (default `Substack`, set in `REPLYTO_LABEL`) and writes a JSON file to your own Drive. The file is an array of `{publication_url, sender_name, reply_to, message_url}`, where `message_url` is the Gmail permalink of the thread. The publication comes from the `List-URL` header, then `List-Id`, then the first direct `/p/` post link in the body. The script sends, labels and deletes nothing. Download the file to `.local\` and import it:

```powershell
py -3.12 -m substack_outreach --config .\substack.config.json contacts-import .\.local\substack-replyto.json
py -3.12 -m substack_outreach --config .\substack.config.json contacts-import .\.local\substack-replyto.json --report .\.local\replyto-report.json
```

Each record is matched by canonical `publication_url`, falling back to the host. Matched records go through the normal recipient update as `contact_kind: "reply_to"`, `contact_confirmed: false`, with the Gmail permalink as `contact_source_url`. A publication that already has a contact route is skipped, never overwritten. Unmatched, ambiguous, skipped and invalid records go only to the report (default `.local\substack\contacts-import-report.json`), not to SQLite. A `reply_to` route stays blocked by `CONTACT_UNVERIFIED` and `EDITORIAL_CONTACT_ROUTE_REQUIRED` until a reviewer verifies it and sets the route type to the right editorial kind. Newsletter reply-to addresses can be per-subscriber relays, so check what the address actually reaches. The `contact-import` command above is different: it imports contact *history*.

## Exports, fixtures and observability

```powershell
py -3.12 -m substack_outreach --config .\substack.config.json export --format csv --scope pilot --out .\.local\pilot.csv
py -3.12 -m substack_outreach --config .\substack.config.json export --format json --scope pilot --out .\.local\pilot.json
py -3.12 -m substack_outreach --config .\substack.config.json export --format sendsheet --scope pilot --out .\.local\sendsheet.html
py -3.12 -m substack_outreach --config .\substack.config.json metrics
py -3.12 -m substack_outreach --config .\substack.config.json fixture
py -3.12 -m substack_outreach --config .\substack.config.json previews
```

Exports contain publication, author, publication URL, article URL, excerpt, angle, contact and source, subject/body, owner, status and flags. CSV values are guarded against formula injection. Draft records include safe HTML and exact evidence/claim snapshots. Do not export private data into `public/`; the CLI rejects it.

`--format sendsheet` writes one private HTML page listing only publications whose current draft is approved. The approval is checked again on export, so a draft whose approval no longer holds is left out. Each entry shows the recipient, subject and plain-text body, with a copy button for each, and an **Open in mail app** link prefilled with recipient, subject and body (`mailto:`). A non-email route gets a link to the route instead. The page sends nothing; it only removes copy-paste friction. After you send a message yourself, use **Record sent manually** in the workspace. The worker API does not serve this page, and the file holds contact data, so keep it in `.local\`.

`previews` writes `.local/substack/fixture-previews.json` and a clickable HTML companion. All three variants use **the same Alan example** and only the user-supplied summary, never fetched text or invented recipients. The article title is a supplied description, the date/contact/publication domain remain unknown, and the fit remains exploratory. The fixture cannot be approved. Generating the preview artifact does not overwrite any edited draft. Select **All imported + fixture** and search `user-supplied fixture` to review it in the UI.

Metrics report imported/shortlisted publications, research completion and failed attempts, draft versions, currently approved drafts, skip reasons and recent run usage. A fixture contributes one publication to overall counts but zero pilot records. No costs, sends, replies or conversion outcomes are fabricated.

## Hosted team workspace (Vercel)

The same workspace can run on Vercel so teammates share one database. Hosted mode uses Postgres (Neon from the Vercel Marketplace) through the pure-Python `pg8000` driver, the only package in `requirements.txt`; the local worker still needs no packages. Nothing sends email in either mode.

**Who can see what.** Sign-in is open to any Google account unless you restrict it. Everyone who signs in can read the facts registry (including unreleased numbers and launch timing), every contact route and draft, and can approve drafts and record sends. Every action is recorded with the signed-in email in `events.actor`. Set `ALLOWED_EMAIL_DOMAIN=nace.ai` (checked against Google's `hd` claim) or `ALLOWED_EMAILS=a@nace.ai,b@nace.ai` to restrict sign-in; no code change is needed.

### 1. Create the project and database

1. On vercel.com: **Add New → Project**, import `AlansPage/nace-substack-outreach`. `vercel.json` sets the build (`python3 build.py`, output `public/`) and sends every `/api/*` request to `api/index.py` with a 300-second limit.
2. In the project: **Storage → Marketplace → Neon → Connect**. Vercel adds `DATABASE_URL` to the project's environment variables. The tables are created on first use.

### 2. Create the Google sign-in client

In Google Cloud Console → **APIs & Services**:

1. **OAuth consent screen**: user type **External**, scopes `openid` and `email`. While the app is in *Testing*, only listed test users can sign in; **Publish app** to let any Google account in (these two scopes need no Google verification).
2. **Credentials → Create credentials → OAuth client ID → Web application**. Authorized redirect URI: `https://YOUR-APP.vercel.app/api/auth/callback` (your production domain).

### 3. Set the environment variables

In Vercel → **Settings → Environment Variables** (Production and Preview):

| Variable | Value |
|---|---|
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | From step 2 |
| `SESSION_SECRET` | 32+ random characters, generated below; changing it signs everyone out |
| `APP_ORIGIN` | `https://YOUR-APP.vercel.app` (no trailing slash); POST requests from any other origin are refused |
| `SUBSTACK_CONFIG` | One-line JSON with your settings, e.g. `{"sender": {"name": "Your Name", "company": "Nace AI", "email": "you@nace.ai"}, "batch_size": 20, "network_enabled": true, "collect_contacts": true}` |
| `OPENAI_API_KEY` | Optional, only with `provider: "openai"` in `SUBSTACK_CONFIG` |
| `ALLOWED_EMAIL_DOMAIN` / `ALLOWED_EMAILS` | Optional sign-in restriction |

```powershell
py -3.12 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`database` and `registry_path` cannot be set in `SUBSTACK_CONFIG`: hosted mode always uses `DATABASE_URL` and the facts registry stored in it. `provider: "local"` is refused because there is no model on Vercel's machines. Redeploy after changing variables.

### 4. Load your data from your own machine

The workbook, `facts.json` and the do-not-contact list never go through git or the Vercel build. Push them from your laptop into the shared database:

```powershell
.\.venv-substack\Scripts\python.exe -m pip install -r requirements.txt
$dbUrl = Read-Host 'DATABASE_URL from Vercel > Storage > Neon (.env tab)' -AsSecureString
$dbPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($dbUrl)
try { $env:DATABASE_URL = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($dbPtr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($dbPtr) }
```

Make a second local config, e.g. `.\.local\hosted.config.json`, that copies your `substack.config.json` but sets `"database": "env:DATABASE_URL"` and `"registry_path": "database"`, then:

```powershell
.\.venv-substack\Scripts\python.exe -m substack_outreach --config .\.local\hosted.config.json facts-push .\substack_outreach\facts.json
.\.venv-substack\Scripts\python.exe -m substack_outreach --config .\.local\hosted.config.json pilot --input .\substack-technology.xlsx
Remove-Item Env:\DATABASE_URL
```

`facts-push` validates the registry exactly as the local worker does and uploads it unchanged; approval stays a human edit to `facts.json` followed by another `facts-push`, and a changed registry invalidates earlier approvals as before. The DNC list is applied during `pilot`/`import`. `contacts-import`, `collect-contacts` and `export --format sendsheet` also work against the shared database with the same config. Importing over the network is slower than local SQLite (a few minutes for 525 rows).

### 5. Use it

Open `https://YOUR-APP.vercel.app/` and press **Sign in with Google**. The header shows who is signed in; reviewer fields are pre-filled with your email but still need the review checkbox. **Propose contacts from about pages** runs inside the 300-second limit, so keep its batch at 20 or below.

Limits: request and response bodies are capped at 4.5 MB by Vercel; the send sheet is built with the CLI, not served by the API; runs are not scheduled. To check hosted storage before a deploy, run the test suite against a disposable Neon branch: set `TEST_DATABASE_URL` to that branch's URL (it drops and recreates the tables) and run the usual unittest command with `pg8000` installed.

## Validation and limits

```powershell
py -3.12 -m unittest discover -s tests -p test_substack.py -v
npm test
py -3.12 build.py
git diff --check
```

The pilot ran locally on the 525-row Technology workbook: 253 publications with a fit signal, 1 suppressed by the do-not-contact seed, 250 shortlisted and a 20-publication pilot. Earlier it ran on the 15,617-row all-channels workbook. No live publisher retrieval, about-page fetch or model call was made; the 20 records need permitted article research. Tests cover mapping/deduplication, triage terms and the do-not-contact list, profiles, missing identity/contact data, RSS/Atom excerpts, default feed permission, about-page and Gmail contact proposals, resume, evidence and claim failures, registry-driven wording, offer rules, approval/version changes, cross-channel history, suppression, exports including the send sheet, safe links/rendering, body length, one-revision limits for the OpenAI and local writers, and the standalone page shell. Saved test fixtures live in `tests/fixtures/`.

Semantic correctness, source permissions, public identity, whether a paraphrase is fair, and outreach suitability still require human review. Official API schemas were checked, but live model access/pricing, a real local model server, real publisher feeds and about pages, and the Gmail script against a real mailbox have not been exercised. The worker is for one trusted local operator: loopback binding, Host/Origin checks, no CORS and a per-session mutation token; it is not an Internet-facing multi-user service. Static builds remain independent of the worker/database/API key.
