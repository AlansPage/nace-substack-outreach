import copy
import io
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs
from zipfile import ZipFile

from substack_outreach.common import FITS, canonical, classify, dumps, html_email, is_profile, now, publication_url
from substack_outreach.config import ROOT, load_config, load_hosted_config
from substack_outreach.evidence import ASKS, BASE_CLAIMS, evidence_checks, prepare_evidence, push_registry, style_checks
from substack_outreach.fixtures import add_fixture, previews
from substack_outreach.models import INSTRUCTIONS, SCHEMA, LocalWriter, MockWriter, OpenAIWriter, writing_input
from html import escape as html_escape
from substack_outreach.pipeline import Pipeline, csv_safe, mailto_link, sendsheet_html
from substack_outreach.sources import Collector, Run, extract_contacts, parse_feed, public_https, save_articles
from substack_outreach.store import load_dnc
from substack_outreach.workbook import identifier, read_workbook

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
# Set to a disposable Postgres database (with pg8000 installed) to run the pipeline suite against hosted storage.
TEST_DATABASE_URL = os.environ.get('TEST_DATABASE_URL', '')


def reset_postgres(url):
    from substack_outreach.db import PgConnection
    db = PgConnection(url)
    for table in ('events', 'cache', 'runs', 'facts_registry', 'registry_history', 'drafts', 'identities', 'suppressions',
                  'contacts', 'articles', 'source_rows', 'imports', 'aliases', 'publications'):
        db.execute(f'DROP TABLE IF EXISTS {table} CASCADE')
    db.commit(); db.close()


def workbook(path, rows):
    """Small real OOXML fixture with reordered headers, inline strings and sparse cells."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    root = ET.Element('worksheet', xmlns=ns)
    data = ET.SubElement(root, 'sheetData')
    for n, values in enumerate(rows, 1):
        row = ET.SubElement(data, 'row', r=str(n))
        for i, value in enumerate(values):
            if value is None:
                continue
            c = ET.SubElement(row, 'c', r=f'{chr(65+i)}{n}', t='inlineStr')
            ET.SubElement(ET.SubElement(c, 'is'), 't').text = str(value)
    with ZipFile(path, 'w') as z:
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="substack" r:id="rId7"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId7" Target="worksheets/sheet3.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet3.xml', ET.tostring(root))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.config = load_config()
        self.config['database'] = TEST_DATABASE_URL or str(self.path / 'test.sqlite3')
        if TEST_DATABASE_URL:
            reset_postgres(TEST_DATABASE_URL)
        self.config['registry_path'] = str(self.path / 'facts.json')
        self.facts = json.loads((ROOT / 'substack_outreach/facts.example.json').read_text(encoding='utf-8'))
        (self.path / 'facts.json').write_text(dumps(self.facts), encoding='utf-8')
        self.p = Pipeline(self.config)

    def tearDown(self):
        self.p.store.close()
        self.temp.cleanup()

    def pub(self, name='Test engineering', author='author:42'):
        data = {'publication': name, 'publication_url': 'https://testing.example', 'profile_url': 'https://substack.com/@publicwriter',
                'source': {}, 'source_kind': 'manual', 'author_name': 'Sam Example', 'confirmed_first_name': 'Sam',
                'name_source': 'https://testing.example/about', 'owner': '', 'contact_route': 'business@testing.example',
                'contact_source_url': 'https://testing.example/contact', 'contact_kind': 'business', 'contact_confirmed': True,
                'fit_category': FITS[0], 'fit_reasons': ['Test evidence'], 'import_flags': [], 'research': {}, 'evidence': None}
        cur = self.p.store.db.execute('INSERT INTO publications(data,score,author_key,pilot,shortlisted,status) VALUES(?,100,?,1,1,?)', (dumps(data), author, 'Shortlisted'))
        self.p.store.db.commit()
        return cur.lastrowid

    def research(self, pid):
        article = {'title': 'Testing browser agents', 'url': 'https://testing.example/p/agents', 'date': now(),
                   'byline': 'Sam Example', 'text': 'Browser agents need explicit evaluation of each action before committing changes in a workflow.',
                   'content_status': 'complete', 'source_kind': 'manual', 'source_url': 'https://testing.example/p/agents',
                   'permission_basis': 'Test-only supplied text', 'membership_confirmed': True, 'relationship_source': 'https://testing.example/archive'}
        save_articles(self.p.store, self.p.store.get(pid), [article], supplied=True)
        e = prepare_evidence(self.p.store.get(pid))
        e['evidence_reviewed_by'] = 'Test reviewer'
        self.p.update(pid, {'evidence': e})
        return article

    def approve_facts(self):
        for e in self.facts['entries']:
            e.update(approval_status='approved', approved_by='Test only', approved_at=now())
        self.facts['version'] += 1
        (self.path / 'facts.json').write_text(dumps(self.facts), encoding='utf-8')

    def ready(self):
        pid = self.pub()
        self.research(pid)
        self.approve_facts()
        drafted = self.p.generate(pid)
        body = drafted['draft']['plain_text_body']
        result = self.p.edit(pid, {'subject': drafted['draft']['subject'], 'plain_text_body': body, 'human_authored': True})
        self.assertEqual(result['draft']['validation']['blocking_flags'], [])
        return pid, result

    def test_workbook_mapping_dedup_and_resume(self):
        path = self.path / 'input.xlsx'
        workbook(path, [['author_id', 'url', 'name', 'pub_id', 'author_name', 'hero_text', 'free_subscribers', 'extra'],
            ['42.0', 'https://a.example/', 'A', '1.0', None, 'AI agents and LLM engineering', '1000.0', '=1+1'],
            ['42', 'https://a.example?utm_source=x', 'A duplicate', '1', None, None, None, None],
            ['42', 'https://b.example', 'B', '2', None, 'Browser agents and model evaluation', None, None]])
        headers, rows = read_workbook(path)
        self.assertEqual(headers[0], 'author_id')
        self.assertIsNone(rows[0][1]['author_name'])
        result = self.p.store.import_workbook(path)
        self.assertEqual((result['inserted'], result['duplicates']), (2, 1))
        a = self.p.store.get(1)
        self.assertEqual(a['source']['extra'], '=1+1')
        self.assertEqual(a['source']['free_subscribers'], '1000.0')
        self.assertEqual(a['confirmed_first_name'], '')
        self.assertEqual(a['author_publications'], 2)
        self.assertEqual(self.p.store.import_workbook(path)['inserted'], 0)
        self.assertEqual(self.p.store.db.execute('SELECT count(*) FROM source_rows').fetchone()[0], 3)
        self.assertEqual(self.p.store.shortlist(100, 1)['pilot'], 1)
        self.assertEqual(self.p.store.shortlist(100, 1)['pilot'], 1)

    def test_profile_is_not_publication_and_ids_normalize(self):
        self.assertTrue(is_profile('https://substack.com/@alreadydawn'))
        self.assertEqual(publication_url('https://substack.com/@alreadydawn'), '')
        self.assertEqual(publication_url('https://x.substack.com/'), 'https://x.substack.com')
        self.assertEqual(identifier('2.70586367E8'), '270586367')

    def test_local_filter_not_subscribers_or_first_post_date(self):
        self.assertEqual(classify({'name': 'Unknown', 'categories': 'Technology', 'free_subscribers': '10000000', 'first_post_date': now()})[0], FITS[4])
        self.assertEqual(classify({'hero_text': 'AI agents, LLM, inference and model evaluation'})[0], FITS[0])
        self.assertEqual(classify({'hero_text': 'Writing on digital minimalism and human agency'})[0], FITS[2])

    def test_classifier_recall_fixture(self):
        for case in json.loads((FIXTURES / 'classifier_rows.json').read_text(encoding='utf-8')):
            fit, score, reasons = classify(case['row'])
            self.assertEqual(fit, case['fit'], case['row']['name'])
            signals = reasons[0].removeprefix('Local metadata signals: ').split(', ') if reasons[0].startswith('Local') else []
            self.assertEqual(signals, case['signals'], case['row']['name'])

    def test_technology_category_weight_ranks_but_is_not_fit_evidence(self):
        row = {'hero_text': 'Weekly notes on how AI changes everyday office work'}
        plain, tech = classify(row), classify(dict(row, categories='Technology; Podcasts'))
        self.assertEqual(plain[0], tech[0])
        self.assertEqual(tech[1], plain[1] + 5)
        self.assertIn('Category weight +5: Technology; Podcasts', tech[2])
        unrelated = classify({'hero_text': 'HTML and CSS layout tricks for hand-written pages', 'categories': 'Technology'})
        self.assertEqual((unrelated[0], unrelated[1]), (FITS[3], 0))

    def test_dnc_list_suppresses_vendor_publications_at_import(self):
        self.config['dnc_path'] = str(FIXTURES / 'dnc.json')
        path = self.path / 'input.xlsx'
        workbook(path, [['pub_id', 'author_id', 'url', 'name', 'author_name', 'hero_text', 'categories'],
            ['1', '10', 'https://www.vendorco-news.substack.com/', 'Vendor news', 'Press team', 'AI agents and LLM product updates', 'Technology'],
            ['2', '11', 'https://ops.example', 'Ops', 'VendorCo Official', 'AI automation for operations teams', 'Technology'],
            ['77', '12', 'https://lab.example', 'Lab', 'Lab desk', 'Deep learning research notes and ML', 'Technology'],
            ['3', '13', 'https://employee.example', 'Personal', 'VendorCo Official fan', 'AI agents, written by an employee, not official', 'Technology']])
        result = self.p.import_workbook(str(path), 20, 100)
        self.assertEqual(result['dnc_list'], 'dnc.json')
        self.assertEqual(sorted(r['matched'] for r in result['dnc_suppressed']), ['author_names', 'hosts', 'pub_ids'])
        statuses = {self.p.store.get(i)['publication']: self.p.store.get(i)['status'] for i in (1, 2, 3, 4)}
        self.assertEqual(statuses, {'Vendor news': 'Do not contact', 'Ops': 'Do not contact', 'Lab': 'Do not contact', 'Personal': 'Shortlisted'})
        self.assertEqual(result['shortlisted'], 1)
        reason = self.p.store.db.execute("SELECT reason FROM suppressions WHERE key='author:10'").fetchone()[0]
        self.assertIn('VendorCo', reason)
        self.assertIn('source: Test fixture', reason)
        again = self.p.import_workbook(str(path), 20, 100)
        self.assertTrue(again['resumed'])
        self.assertEqual(again['dnc_suppressed'], [])

    def test_dnc_falls_back_to_seed_and_rejects_entries_without_source(self):
        self.config['dnc_path'] = str(self.path / 'missing.json')
        entries, name = self.p.dnc_list()
        self.assertEqual(name, 'dnc.example.json')
        self.assertTrue(all(e['source'] for e in entries))
        bad = self.path / 'bad.json'
        bad.write_text(dumps({'entries': [{'owner': 'X', 'reason': 'Vendor', 'hosts': ['x.example']}]}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'owner, reason and source'):
            load_dnc(bad)

    def use_phrases(self):
        phrases = json.loads((FIXTURES / 'registry_phrases.json').read_text(encoding='utf-8'))
        for e in self.facts['entries']:
            if e['id'] in phrases:
                e['email_phrase'] = phrases[e['id']]
        (self.path / 'facts.json').write_text(dumps(self.facts), encoding='utf-8')
        return phrases

    def test_mock_wording_comes_from_registry_phrases(self):
        phrases = self.use_phrases()
        pid, p = self.ready()
        body = p['draft']['plain_text_body']
        self.assertIn("I'm Your Name from Nace AI, working on " + phrases['product.model'] + ', ' + phrases['product.agents'] + '.', body)
        self.assertIn('We can offer fixture evaluation credits and answer fixture questions.', body)
        self.assertNotIn('Drex Max', body + p['draft']['subject'])
        self.assertNotIn('screenshot', body)
        self.assertFalse([f for f in p['draft']['review_flags'] if f.startswith('CLAIM_SCOPE_OR_TEXT_CHANGED')])

    def test_mock_previews_pending_phrase_but_never_unselected_or_rejected(self):
        phrases = self.use_phrases()
        self.facts['entries'][[e['id'] for e in self.facts['entries']].index('product.agents')]['approval_status'] = 'rejected'
        (self.path / 'facts.json').write_text(dumps(self.facts), encoding='utf-8')
        pid = self.pub(); self.research(pid)
        e = self.p.store.get(pid)['evidence']; e['selected_claim_ids'] = [c for c in e['selected_claim_ids'] if c != 'offer.questions']
        self.p.update(pid, {'evidence': e})
        d = self.p.generate(pid)['draft']
        self.assertIn(phrases['product.model'], d['plain_text_body'])
        self.assertIn('CLAIM_NOT_APPROVED:product.model', d['review_flags'])
        for cid in ('product.agents', 'offer.questions'):
            self.assertNotIn(phrases[cid], d['plain_text_body'])
            self.assertNotIn(cid, d['claim_ids_used'])
        self.assertIn('PRODUCT_USE_CASE_SUPPORT_OR_LINK_MISSING', d['review_flags'])

    def test_no_product_name_or_capability_hard_coded(self):
        from substack_outreach.models import INSTRUCTIONS
        reference = INSTRUCTIONS[INSTRUCTIONS.index('STRUCTURAL EXAMPLE ONLY'):]
        self.assertIn('[APPROVED PRODUCT MODEL PHRASE]', reference)
        self.assertNotIn('release', reference)
        for path in (ROOT / 'substack_outreach').glob('*.*'):
            if path.suffix in ('.py', '.txt', '.js') :
                text = path.read_text(encoding='utf-8')
                for term in ('Drex Max', 'screenshots', 'browser-use agents', 'multimodal decision model'):
                    self.assertNotIn(term, text, f'{path.name} hard-codes {term!r}')

    def select_offer(self, pid, ask_format):
        fx = json.loads((FIXTURES / 'sponsored_selection.json').read_text(encoding='utf-8'))
        e = self.p.store.get(pid)['evidence']
        e.update(selected_claim_ids=fx['selected_claim_ids'], ask_format=ask_format, suggested_ask=ASKS[ask_format])
        self.p.update(pid, {'evidence': e})
        return fx, e

    def test_sponsored_ask_exists_but_default_stays_editorial(self):
        self.assertIn('sponsored post', ASKS)
        pid = self.pub(); self.research(pid)
        e = self.p.store.get(pid)['evidence']
        self.assertEqual(e['ask_format'], 'technical walkthrough')
        self.assertNotIn('offer.sponsorship', e['selected_claim_ids'])
        from substack_outreach.models import INSTRUCTIONS
        paragraph3 = ' '.join(INSTRUCTIONS[INSTRUCTIONS.index('Paragraph 3'):INSTRUCTIONS.index('Explore Drex')].split())
        self.assertIn('selected approved offer', paragraph3)
        self.assertNotIn('API credits', paragraph3)

    def test_editorial_ask_never_states_sponsorship_even_when_selected(self):
        pid = self.pub(); self.research(pid); self.approve_facts()
        fx, e = self.select_offer(pid, 'technical walkthrough')
        facts, _ = self.p.facts()
        payload = writing_input(self.p.store.get(pid), e, facts, self.config['sender'])
        self.assertNotIn('offer.sponsorship', [f['id'] for f in payload['approved_facts']])
        self.assertEqual(payload['pitch_angle']['ask_format'], 'technical walkthrough')
        d = self.p.generate(pid)['draft']
        self.assertIn(fx['editorial_sentence'], d['plain_text_body'])
        self.assertNotIn('sponsor', d['plain_text_body'].lower())
        self.assertNotIn('offer.sponsorship', d['claim_ids_used'])
        d = self.p.edit(pid, {'subject': d['subject'], 'plain_text_body': d['plain_text_body'], 'claim_ids_used': d['claim_ids_used'] + ['offer.sponsorship']})['draft']
        self.assertIn('SPONSORSHIP_OFFER_REQUIRES_SPONSORED_ASK', d['validation']['blocking_flags'])

    def test_sponsored_ask_states_only_the_sponsorship_offer(self):
        pid = self.pub(); self.research(pid); self.approve_facts()
        fx, e = self.select_offer(pid, 'sponsored post')
        facts, _ = self.p.facts()
        payload = writing_input(self.p.store.get(pid), e, facts, self.config['sender'])
        self.assertEqual([f['id'] for f in payload['approved_facts'] if f['scope'] == 'offer'], ['offer.sponsorship'])
        d = self.p.generate(pid)['draft']
        self.assertIn(ASKS['sponsored post'] + ' ' + fx['sponsorship_sentence'], d['plain_text_body'])
        self.assertNotIn('API credits', d['plain_text_body'])
        self.assertIn('offer.sponsorship', d['claim_ids_used'])
        self.assertNotIn('SPONSORED_ASK_REQUIRES_SPONSORSHIP_OFFER', d['review_flags'])
        # Still blocked: the "sponsor" wording check and the base-claim requirement are unchanged.
        for flag in ('UNSUPPORTED_PROMISE_OR_PLACEHOLDER', 'PRODUCT_USE_CASE_SUPPORT_OR_LINK_MISSING'):
            self.assertIn(flag, d['validation']['blocking_flags'])

    def test_contacted_checkbox_is_shared_and_blocks_other_teammates(self):
        fx = json.loads((FIXTURES / 'contacted_marks.json').read_text(encoding='utf-8'))
        a, b = self.pub('Main publication'), self.pub('Same author, second publication')
        self.research(b); self.p.generate(b)
        self.p.store.close()
        alice = self.p = Pipeline(self.config, actor='alice@example.com')
        for case in fx['invalid']:
            with self.assertRaisesRegex(ValueError, case['error']):
                alice.action(a, 'contacted', case['data'])
        alice.action(a, 'contacted', fx['mark'])
        alice.action(a, 'contacted', fx['mark'])  # same mark again on the same day is not a second contact
        rows = alice.store.db.execute('SELECT channel,event,source,actor FROM contacts').fetchall()
        self.assertEqual([tuple(r) for r in rows], [('LinkedIn', 'Contacted', 'https://www.linkedin.com/in/sam-example/', 'alice@example.com')])
        listed = {i['id']: i['contacted'] for i in alice.queue()['items']}
        self.assertEqual(listed[a]['actor'], 'alice@example.com')
        self.assertEqual((listed[b]['channel'], listed[b]['event']), ('LinkedIn', 'Contacted'))
        alice.store.close()
        bob = self.p = Pipeline(self.config, actor='bob@example.com')
        with self.assertRaisesRegex(ValueError, 'prior contact'):
            bob.generate(b, regenerate=True)
        self.assertIn('PREVIOUS_CONTACT_REVIEW_REQUIRED', bob.detail(b)['draft']['validation']['blocking_flags'])
        third = self.pub('Different author', 'author:77')
        # Same email means same person, so the unrelated writer needs its own route.
        pub = bob.store.get(third); pub['contact_route'] = 'editor@different.example'; bob.store.save(pub); bob.store.db.commit()
        self.assertEqual(bob.store.get(third)['previous_contact'], [])
        bob.action(third, 'contacted', {'confirmed': True, 'channel': 'Email'})
        self.assertEqual(bob.store.get(third)['previous_contact'][0]['source'], 'Marked contacted in the workspace by bob@example.com')

    def upload_sheet(self, sheet, last=True, extra_rows=()):
        table = json.loads((FIXTURES / 'all_channels_sample.json').read_text(encoding='utf-8'))['sheets'][sheet]
        return self.p.upload_rows({'sheet': sheet, 'headers': table['headers'], 'rows': table['rows'] + list(extra_rows), 'offset': 0, 'last': last})

    def test_json_upload_imports_substack_sheet_and_rejects_other_channels(self):
        result = self.upload_sheet('substack')
        self.assertEqual((result['inserted'], result['duplicates'], result['sheet']), (3, 0, 'substack'))
        self.assertEqual(result['shortlisted'], 2)  # the garden diary has no topical signal
        pub = self.p.store.get(1)
        self.assertEqual((pub['publication'], pub['source']['free_subscribers'], pub['author_publications']), ('Agent Notes', '12000', 2))
        self.assertTrue(self.p.store.db.execute("SELECT path FROM imports").fetchone()[0].startswith('upload:substack by '))
        for other in ('Discord', 'mediun', 'Copy of hackernoon'):
            with self.assertRaisesRegex(ValueError, 'not a Substack publication table'):
                self.upload_sheet(other)
        with self.assertRaisesRegex(ValueError, 'at most 5000'):
            self.p.upload_rows({'sheet': 'substack', 'headers': ['url'], 'rows': [{}] * 5001})

    def test_reupload_keeps_who_was_contacted(self):
        self.upload_sheet('substack')
        self.p.action(1, 'contacted', {'confirmed': True, 'channel': 'X', 'link': 'https://x.com/sam'})
        self.p.action(3, 'skip', {'reason': 'Off topic'})
        again = self.upload_sheet('substack')
        self.assertTrue(again['resumed'])
        newer = {'_row': 5, 'name': 'Agent Notes Podcast', 'author_name': 'Sam Example', 'url': 'https://agentnotespod.substack.com/',
                 'categories': 'Technology', 'hero_text': 'Interviews about AI agents and LLM evaluation', 'pub_id': '1004', 'author_id': '501'}
        result = self.upload_sheet('substack', extra_rows=[newer])
        self.assertEqual((result['inserted'], result['duplicates']), (1, 3))
        listed = {i['publication']: i for i in self.p.queue(scope='all')['items']}
        for name in ('Agent Notes', 'Agent Notes Weekly', 'Agent Notes Podcast'):
            self.assertEqual(listed[name]['contacted']['channel'], 'X', name)  # same author, including the new publication
        self.assertEqual(listed['Garden Diary']['status'], 'Skipped')
        self.assertEqual(self.p.store.db.execute('SELECT count(*) FROM contacts').fetchone()[0], 1)

    def test_export_is_a_named_download_with_counts_and_excel_friendly_csv(self):
        from substack_outreach.server import api_get
        pid, _ = self.ready()
        self.p.action(pid, 'contacted', {'confirmed': True, 'channel': 'Email'})
        other = self.pub('No draft yet', 'author:9')
        pub = self.p.store.get(other); pub['contact_route'] = 'editor@nodraft.example'; self.p.store.save(pub); self.p.store.db.commit()
        status, body, content_type, headers = api_get(self.p, self.config, '/api/substack/export', {'format': 'csv', 'scope': 'pilot'})
        self.assertEqual((status, content_type), (200, 'text/csv; charset=utf-8'))
        self.assertRegex(headers['Content-Disposition'], r'^attachment; filename="substack-pilot-\d{4}-\d{2}-\d{2}\.csv"$')
        self.assertEqual((headers['X-Export-Rows'], headers['X-Export-Drafts']), ('2', '1'))
        self.assertTrue(body.startswith(chr(0xfeff) + 'publication,'))
        self.assertIn('Contacted · Email', body)
        rows = json.loads(api_get(self.p, self.config, '/api/substack/export', {'format': 'json', 'scope': 'all'})[1])
        self.assertEqual(sorted(r['contacted'][:9] for r in rows), ['', 'Contacted'])
        self.assertEqual(api_get(self.p, self.config, '/api/substack/export', {'format': 'csv', 'scope': 'everyone'})[0], 400)

    def test_pending_claims_and_mock_cannot_approve(self):
        pid = self.pub(); self.research(pid)
        p = self.p.generate(pid)
        self.assertIn('MOCK_PREVIEW_NOT_SEND_READY', p['draft']['review_flags'])
        self.assertIn('CLAIM_NOT_APPROVED:product.model', p['draft']['review_flags'])
        with self.assertRaises(ValueError):
            self.p.action(pid, 'approve', {'reviewer': 'X', 'semantic_review_confirmed': True})

    def test_invalid_excerpt_stops_generation(self):
        pid = self.pub(); self.research(pid)
        e = self.p.store.get(pid)['evidence']; e['supporting_excerpt'] = 'Invented statement'
        self.p.update(pid, {'evidence': e})
        with self.assertRaisesRegex(ValueError, 'EXCERPT_NOT_IN_SOURCE'):
            self.p.generate(pid)

    def test_missing_name_uses_neutral_and_missing_contact_blocks(self):
        pid = self.pub(); self.research(pid)
        self.p.update(pid, {'confirmed_first_name': '', 'name_source': '', 'contact_route': '', 'contact_source_url': '', 'contact_confirmed': False})
        p = self.p.generate(pid)
        self.assertTrue(p['draft']['plain_text_body'].startswith('Hello,'))
        self.assertIn('CONTACT_UNVERIFIED', p['draft']['review_flags'])

    def test_approval_invalidation_preserves_versions_and_edits(self):
        pid, p = self.ready()
        p = self.p.action(pid, 'approve', {'reviewer': 'Human', 'semantic_review_confirmed': True})
        self.assertEqual(p['draft']['hash'], p['draft']['approved_hash'])
        old = p['draft']['plain_text_body']
        edited = old.replace('Your article', 'Your recent article')
        p = self.p.edit(pid, {'subject': p['draft']['subject'], 'plain_text_body': edited})
        self.assertEqual(p['draft']['plain_text_body'], edited)
        self.assertIsNone(p['draft']['approved_hash'])
        p = self.p.generate(pid)
        self.assertEqual(p['draft']['plain_text_body'], edited)
        p = self.p.generate(pid, regenerate=True)
        self.assertNotEqual(p['draft']['plain_text_body'], edited)
        self.assertIsNone(p['draft']['approved_hash'])
        self.assertEqual(self.p.store.db.execute('SELECT count(*) FROM drafts WHERE publication_id=?', (pid,)).fetchone()[0], 4)

    def test_registry_change_invalidates_approval_without_editing_body(self):
        pid, p = self.ready()
        p = self.p.action(pid, 'approve', {'reviewer': 'Human', 'semantic_review_confirmed': True})
        body = p['draft']['plain_text_body']
        self.facts['entries'][2]['approval_status'] = 'pending'
        (self.path / 'facts.json').write_text(dumps(self.facts), encoding='utf-8')
        p = self.p.detail(pid)
        self.assertIsNone(p['draft']['approved_hash'])
        self.assertEqual(p['draft']['plain_text_body'], body)

    def test_contact_history_cross_channel_and_dnc(self):
        a, b = self.pub(), self.pub('Second publication')
        self.p.action(a, 'history', {'channel': 'Reddit', 'source': 'https://reddit.com/comments/test', 'event': 'Replied'})
        self.assertEqual(len(self.p.store.get(b)['previous_contact']), 1)
        with self.assertRaises(ValueError): self.p.generate(b)
        self.p.action(a, 'dnc', {'reason': 'Requested opt-out'})
        self.assertTrue(self.p.store.get(b)['do_not_contact'])
        self.assertEqual(self.p.store.get(b)['status'], 'Do not contact')

    def test_only_one_approved_draft_per_author(self):
        a, p = self.ready()
        self.p.action(a, 'approve', {'reviewer': 'X', 'semantic_review_confirmed': True})
        b = self.pub('Another publication'); self.research(b)
        p = self.p.generate(b)
        self.assertIn('AUTHOR_ALREADY_HAS_APPROVED_DRAFT', p['draft']['review_flags'])

    def test_manual_sent_retains_approved_snapshot(self):
        pid, _ = self.ready()
        self.p.action(pid, 'approve', {'reviewer': 'X', 'semantic_review_confirmed': True})
        p = self.p.action(pid, 'sent', {})
        self.assertEqual(p['status'], 'Sent')
        self.assertEqual(p['draft']['hash'], p['draft']['approved_hash'])
        self.assertEqual(len(p['previous_contact']), 1)
        self.assertEqual(self.p.action(pid, 'replied', {})['status'], 'Replied')

    def test_source_edit_invalidates_approval_but_identical_resume_does_not(self):
        pid, _ = self.ready()
        pub = self.p.store.get(pid)
        article = {k: v for k, v in pub['articles'][0].items() if k != 'id'}
        self.p.action(pid, 'approve', {'reviewer': 'X', 'semantic_review_confirmed': True})
        save_articles(self.p.store, self.p.store.get(pid), [article], supplied=True)
        self.assertIsNotNone(self.p.detail(pid)['draft']['approved_hash'])
        article['text'] += ' Additional supplied evidence.'
        save_articles(self.p.store, self.p.store.get(pid), [article], supplied=True)
        self.assertIsNone(self.p.detail(pid)['draft']['approved_hash'])

    def test_unsupported_claim_launch_and_placeholder(self):
        pid, p = self.ready()
        text = p['draft']['plain_text_body'].replace('We can offer API credits', 'Drex Max is released with 99% accuracy. We can offer API credits')
        p = self.p.edit(pid, {'subject': '[recipient] TODO', 'plain_text_body': text})
        for f in ('LAUNCH_STATUS_UNCONFIRMED', 'UNSUPPORTED_NUMERIC_CLAIM', 'UNRESOLVED_PLACEHOLDER'):
            self.assertIn(f, p['draft']['review_flags'])

    def test_html_and_exports_preserve_links_without_scripts_or_formulas(self):
        pid, p = self.ready()
        html = html_email('Your post (https://testing.example/p/a)—detail <img src=x onerror=alert(1)>')
        self.assertIn('href="https://testing.example/p/a"', html)
        self.assertNotIn('<img', html)
        self.assertEqual(csv_safe('  =HYPERLINK("x")'), '\'  =HYPERLINK("x")')
        export = json.loads(self.p.export())
        self.assertIn('https://testing.example/p/agents', export[0]['email_body'])
        self.assertIn('https://nace.ai/developers', export[0]['email_body'])
        self.assertNotIn('OPENAI_API_KEY', self.p.export('csv'))

    def test_fixture_three_variants_never_fetched_or_pilot(self):
        examples = previews(self.p)
        self.assertEqual(len(examples), 3)
        self.assertTrue(all('Alan fixture' in p['label'] for p in examples))
        self.assertTrue(all(60 <= p['checks']['body_word_count'] <= 90 for p in examples))
        self.assertTrue(all(not p['checks']['flags'] for p in examples))
        pid = add_fixture(self.p)
        self.assertEqual(self.p.store.get(pid)['pilot'], 0)
        self.assertEqual(self.p.store.get(pid)['articles'][0]['source_kind'], 'user_fixture')

    def test_live_writer_requires_authorization_budget_model_and_key(self):
        run = Run(self.p.store, self.config, 'test')
        with self.assertRaisesRegex(ValueError, 'Paid calls require'):
            OpenAIWriter(self.p.store, self.config, run)
        self.config.update(allow_paid_calls=True, budget_usd_per_run=1, model='test')
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'OPENAI_API_KEY'):
                OpenAIWriter(self.p.store, self.config, run)

    def test_model_only_receives_selected_approved_evidence(self):
        pid = self.pub(); self.research(pid)
        pub = self.p.store.get(pid); pub['source'] = {'secret_note': 'do not pass'}
        facts, _ = self.p.facts()
        payload = writing_input(pub, pub['evidence'], facts, self.config['sender'])
        self.assertEqual(payload['approved_facts'], [])
        self.assertNotIn('do not pass', dumps(payload))
        self.assertNotIn('research', payload)
        self.assertNotIn('review_flags', payload)

    def test_one_revision_only_for_failing_email(self):
        pid = self.pub(); self.research(pid)
        original = MockWriter.generate
        calls = []
        def bad(writer, payload, evidence, revision=None):
            calls.append(revision)
            d = original(writer, payload, evidence, revision)
            d['subject'] = '[recipient]'
            return d
        with patch.object(MockWriter, 'generate', bad):
            p = self.p.generate(pid)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0])
        self.assertIsNotNone(calls[1])
        self.assertIn('UNRESOLVED_PLACEHOLDER', p['draft']['review_flags'])

    def test_request_and_spending_caps_are_persisted(self):
        self.config.update(max_requests_per_run=1, budget_usd_per_run=.1)
        run = Run(self.p.store, self.config, 'test')
        with self.assertRaises(ValueError): run.request(.11)
        run.request(.05)
        with self.assertRaises(ValueError): run.request(.01)
        self.assertEqual(self.p.store.db.execute('SELECT requests FROM runs WHERE id=?', (run.id,)).fetchone()[0], 1)

    def test_network_off_and_permissions_checked_before_fetch(self):
        pid = self.pub()
        run = Run(self.p.store, self.config, 'test')
        with patch('urllib.request.OpenerDirector.open') as fetch:
            with self.assertRaisesRegex(ValueError, 'NETWORK_DISABLED'):
                Collector(self.p.store, self.config, run).collect(self.p.store.get(pid))
            self.config['network_enabled'] = True
            with self.assertRaisesRegex(ValueError, 'SOURCE_PERMISSION_REQUIRED'):
                Collector(self.p.store, self.config, run).collect(self.p.store.get(pid))
            fetch.assert_not_called()

    def default_permission_pub(self, pub_id='555', research=None):
        pid = self.pub()
        pub = self.p.store.get(pid)
        pub['source'] = {'pub_id': pub_id} if pub_id else {}
        pub['research'] = research or {}
        self.p.store.save(pub); self.p.store.db.commit()
        self.config['network_enabled'] = True
        self.config['default_feed_permission'] = json.loads((FIXTURES / 'default_feed_permission.json').read_text(encoding='utf-8'))
        return pid

    def test_default_feed_permission_collects_custom_domain(self):
        pid = self.default_permission_pub()
        pages = {'https://testing.example': (FIXTURES / 'custom_domain_home.html').read_bytes(),
                 'https://testing.example/feed': (FIXTURES / 'custom_domain_feed.xml').read_bytes()}
        collector = Collector(self.p.store, self.config, Run(self.p.store, self.config, 'test'))
        with patch.object(collector, 'allowed_get', side_effect=lambda url, host: pages[url]) as fetch:
            rows = collector.collect(self.p.store.get(pid))
        self.assertEqual([c.args for c in fetch.call_args_list], [('https://testing.example', 'testing.example'), ('https://testing.example/feed', 'testing.example')])
        self.assertEqual(rows[0]['url'], 'https://testing.example/p/scoring')
        self.assertTrue(rows[0]['membership_confirmed'])
        self.assertEqual(rows[0]['permission_source_url'], 'https://policy.example/substack-research')
        self.assertIn('team policy', rows[0]['permission_basis'])

    def test_default_feed_permission_never_overrides_or_widens(self):
        collector = lambda: Collector(self.p.store, self.config, Run(self.p.store, self.config, 'test'))
        # Explicit research block wins, even when incomplete.
        pid = self.default_permission_pub(research={'source_kind': 'publisher_rss', 'permission_basis': 'Explicit but not permitted'})
        with self.assertRaisesRegex(ValueError, 'SOURCE_PERMISSION_REQUIRED'):
            collector().permission(self.p.store.get(pid))
        # Not a workbook Substack row: no pub_id.
        pid = self.default_permission_pub(pub_id=None)
        with self.assertRaisesRegex(ValueError, 'SOURCE_PERMISSION_REQUIRED'):
            collector().permission(self.p.store.get(pid))
        # Unresolved publication URL.
        pid = self.default_permission_pub()
        pub = self.p.store.get(pid); pub['publication_url'] = ''; self.p.store.save(pub); self.p.store.db.commit()
        with self.assertRaisesRegex(ValueError, 'SOURCE_PERMISSION_REQUIRED'):
            collector().permission(self.p.store.get(pid))
        # Expired review date and the default null value.
        pid = self.default_permission_pub()
        self.config['default_feed_permission']['permission_review_date'] = '2000-01-01'
        with self.assertRaisesRegex(ValueError, 'SOURCE_PERMISSION_REVIEW_REQUIRED'):
            collector().permission(self.p.store.get(pid))
        self.config['default_feed_permission'] = None
        with self.assertRaisesRegex(ValueError, 'SOURCE_PERMISSION_REQUIRED'):
            collector().permission(self.p.store.get(pid))

    def test_default_feed_permission_config_shape(self):
        good = json.loads((FIXTURES / 'default_feed_permission.json').read_text(encoding='utf-8'))
        self.assertIsNone(load_config()['default_feed_permission'])
        for bad in ({}, dict(good, feed_url='https://x.example/feed'), dict(good, permission_source_url='http://policy.example'),
                    dict(good, permission_review_date='soon'), dict(good, permission_basis='ok'), 'yes'):
            path = self.path / 'cfg.json'
            path.write_text(dumps({'default_feed_permission': bad}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'default_feed_permission'):
                load_config(path)
        path.write_text(dumps({'default_feed_permission': good}), encoding='utf-8')
        self.assertEqual(load_config(path)['default_feed_permission'], good)

    ABOUT = [{'route': 'Editor@Testing.example', 'type': 'email', 'found_as': 'mailto link'},
             {'route': 'partners@testing.example', 'type': 'email', 'found_as': 'visible text'},
             {'route': 'https://x.com/samexample', 'type': 'x', 'found_as': 'link'},
             {'route': 'https://linkedin.com/in/sam-example', 'type': 'linkedin', 'found_as': 'link'},
             {'route': 'https://samexample.dev', 'type': 'site', 'found_as': 'link'}]

    def test_about_page_contact_extraction(self):
        page = (FIXTURES / 'about_page.html').read_text(encoding='utf-8')
        self.assertEqual(extract_contacts(page, 'https://testing.example/about'), self.ABOUT)

    def contact_pub(self):
        pid = self.pub()
        self.p.update(pid, {'contact_route': '', 'contact_source_url': '', 'contact_kind': '', 'contact_confirmed': False})
        self.config.update(collect_contacts=True, network_enabled=True)
        return pid

    def test_collect_contacts_stores_unconfirmed_proposal_that_blocks_approval(self):
        pid = self.contact_pub()
        page = (FIXTURES / 'about_page.html').read_bytes()
        with patch.object(Collector, 'allowed_get', return_value=page) as fetch:
            results = self.p.collect_contacts(5)
            self.assertEqual(fetch.call_args.args, ('https://testing.example/about', 'testing.example'))
            self.assertEqual(self.p.collect_contacts(5), [])  # resumable: no second fetch
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(results[0]['result'], 'proposed')
        pub = self.p.store.get(pid)
        proposal = pub['contact_route_proposed']
        self.assertEqual((proposal['route'], proposal['contact_source_url'], proposal['contact_confirmed']),
                         ('Editor@Testing.example', 'https://testing.example/about', False))
        self.assertEqual(proposal['candidates'], self.ABOUT)
        self.assertEqual((pub['contact_route'], pub['contact_confirmed']), ('', False))
        self.research(pid)
        draft = self.p.generate(pid)['draft']
        self.assertIn('CONTACT_PROPOSED_UNCONFIRMED', draft['validation']['blocking_flags'])
        with self.assertRaisesRegex(ValueError, 'CONTACT_PROPOSED_UNCONFIRMED'):
            self.p.action(pid, 'approve', {'reviewer': 'X', 'semantic_review_confirmed': True})
        with self.assertRaisesRegex(ValueError, 'named reviewer'):
            self.p.action(pid, 'contact_proposal', {'decision': 'confirm'})
        with self.assertRaisesRegex(ValueError, 'one of the proposed routes'):
            self.p.action(pid, 'contact_proposal', {'decision': 'confirm', 'route': 'other@else.example', 'reviewer': 'Human'})
        p = self.p.action(pid, 'contact_proposal', {'decision': 'confirm', 'route': 'partners@testing.example', 'contact_kind': 'business', 'reviewer': 'Human'})
        self.assertEqual((p['contact_route'], p['contact_source_url'], p['contact_kind'], p['contact_confirmed']),
                         ('partners@testing.example', 'https://testing.example/about', 'business', True))
        self.assertEqual((p['contact_route_proposed']['status'], p['contact_route_proposed']['decided_by']), ('confirmed', 'Human'))
        self.assertNotIn('CONTACT_PROPOSED_UNCONFIRMED', p['draft']['review_flags'])

    def test_contact_proposal_dismiss_and_gates(self):
        pid = self.contact_pub()
        self.config['collect_contacts'] = False
        with patch.object(Collector, 'allowed_get') as fetch:
            with self.assertRaisesRegex(ValueError, 'CONTACT_COLLECTION_DISABLED'):
                self.p.collect_contacts()
            self.config.update(collect_contacts=True, network_enabled=False)
            with self.assertRaisesRegex(ValueError, 'NETWORK_DISABLED'):
                self.p.collect_contacts()
            fetch.assert_not_called()
        self.config['network_enabled'] = True
        with patch.object(Collector, 'allowed_get', return_value=(FIXTURES / 'about_page.html').read_bytes()):
            self.p.collect_contacts()
        p = self.p.action(pid, 'contact_proposal', {'decision': 'dismiss', 'reviewer': 'Human'})
        self.assertEqual((p['contact_route'], p['contact_route_proposed']['status']), ('', 'dismissed'))
        with self.assertRaisesRegex(ValueError, 'No unresolved'):
            self.p.action(pid, 'contact_proposal', {'decision': 'confirm', 'reviewer': 'Human'})

    def test_contact_collection_obeys_robots_and_retries_failed_only_on_request(self):
        pid = self.contact_pub()
        def get(collector, url, host, use_cache=True):
            return b'User-agent: *\nDisallow: /about\n' if url.endswith('/robots.txt') else self.fail('about page fetched despite robots')
        with patch.object(Collector, 'get', get):
            self.assertIn('ROBOTS_DISALLOWED', self.p.collect_contacts()[0]['error'])
        self.assertEqual(self.p.store.get(pid)['contact_route_proposed']['status'], 'failed')
        self.assertEqual(self.p.collect_contacts(), [])
        with patch.object(Collector, 'allowed_get', return_value=b'<p>No contact here.</p>'):
            self.assertEqual(self.p.collect_contacts(retry_failed=True)[0]['result'], 'none_found')
        self.assertNotIn('CONTACT_PROPOSED_UNCONFIRMED', str(self.p.store.get(pid)))

    def bare_pub(self, name, url, author):
        pid = self.pub(name, author)
        pub = self.p.store.get(pid)
        pub.update(publication_url=url, contact_route='', contact_source_url='', contact_kind='', contact_confirmed=False)
        self.p.store.save(pub); self.p.store.db.commit()
        return pid

    def test_reply_to_import_matches_by_url_then_host_and_reports_the_rest(self):
        alpha = self.bare_pub('Alpha', 'https://alpha.example', 'author:a')
        beta = self.bare_pub('Beta', 'https://beta.example', 'author:b')
        gamma = self.bare_pub('Gamma', 'https://gamma.example', 'author:g')
        testing = self.pub()  # already has a confirmed business route
        report = self.p.import_reply_to(json.loads((FIXTURES / 'replyto_export.json').read_text(encoding='utf-8')))
        self.assertEqual([(r['publication_id'], r['matched_by']) for r in report['imported']], [(alpha, 'publication_url'), (beta, 'host')])
        self.assertEqual([r['reason'] for r in report['unmatched']], ['no_match', 'no_publication_url'])
        self.assertEqual([(r['publication_id'], r['index']) for r in report['skipped']], [(alpha, 2), (testing, 5)])
        self.assertEqual([r['index'] for r in report['invalid']], [6, 7, 8])
        self.assertIn('Contact route must be', report['invalid'][0]['reason'])
        a = self.p.store.get(alpha)
        self.assertEqual((a['contact_route'], a['contact_source_url'], a['contact_kind'], a['contact_confirmed']),
                         ('reply@alpha.example', 'https://mail.google.com/mail/u/0/#all/t-alpha', 'reply_to', False))
        self.assertEqual(self.p.store.get(testing)['contact_route'], 'business@testing.example')
        self.assertEqual(self.p.store.get(gamma)['contact_route'], '')
        self.assertFalse(self.p.store.db.execute("SELECT 1 FROM publications WHERE data LIKE '%unknown.example%'").fetchone())
        self.research(alpha)
        flags = self.p.generate(alpha)['draft']['validation']['blocking_flags']
        self.assertIn('CONTACT_UNVERIFIED', flags)
        self.assertIn('EDITORIAL_CONTACT_ROUTE_REQUIRED', flags)

    def test_contacts_import_cli_writes_private_report(self):
        import subprocess, sys
        cfg = self.path / 'cfg.json'
        cfg.write_text(dumps({'database': self.config['database'], 'registry_path': self.config['registry_path']}), encoding='utf-8')
        self.bare_pub('Alpha', 'https://alpha.example', 'author:a')
        report = self.path / 'report.json'
        r = subprocess.run([sys.executable, '-m', 'substack_outreach', '--config', str(cfg), 'contacts-import', str(FIXTURES / 'replyto_export.json'), '--report', str(report)],
                           cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['imported'], 1)
        self.assertEqual(len(json.loads(report.read_text(encoding='utf-8'))['unmatched']), 5)
        r = subprocess.run([sys.executable, '-m', 'substack_outreach', '--config', str(cfg), 'contacts-import', str(FIXTURES / 'replyto_export.json'), '--report', str(ROOT / 'public' / 'r.json')],
                           cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, 2)
        self.assertIn('public static assets', r.stderr)

    def test_sendsheet_lists_only_current_approved_drafts(self):
        fx = json.loads((FIXTURES / 'sendsheet.json').read_text(encoding='utf-8'))
        pid, p = self.ready()
        p = self.p.edit(pid, {'subject': fx['subject'], 'plain_text_body': p['draft']['plain_text_body'], 'human_authored': True})
        self.assertEqual(p['draft']['validation']['blocking_flags'], [])
        self.p.action(pid, 'approve', {'reviewer': 'Human', 'semantic_review_confirmed': True})
        other = self.pub('Unapproved publication', 'author:99'); self.research(other); self.p.generate(other)
        page = self.p.export('sendsheet')
        self.assertEqual(page.count('<article>'), 1)
        self.assertNotIn('Unapproved publication', page)
        self.assertIn('Q&amp;A? &lt;b&gt;agents&lt;/b&gt; &amp; &quot;scoring&quot;', page)
        self.assertNotIn('<b>agents', page)
        body = self.p.store.get(pid)['draft']['plain_text_body']
        self.assertIn(html_escape(mailto_link('business@testing.example', fx['subject'], body)), page)
        self.assertEqual(page.count('data-copy="'), 3)
        self.assertIn('Nothing is sent from this page', page)
        # A withdrawn approval (registry change) drops the draft from the sheet.
        self.facts['entries'][2]['approval_status'] = 'pending'
        (self.path / 'facts.json').write_text(dumps(self.facts), encoding='utf-8')
        self.assertIn('No publication is currently approved', self.p.export('sendsheet'))

    def test_sendsheet_mailto_encoding_and_url_routes(self):
        fx = json.loads((FIXTURES / 'sendsheet.json').read_text(encoding='utf-8'))
        m = fx['mailto']
        self.assertEqual(mailto_link(m['to'], m['subject'], m['body']), m['expected'])
        page = sendsheet_html([fx['url_route_row']], now())
        self.assertNotIn('mailto:', page)
        self.assertIn('href="https://forms.example/submit"', page)
        import subprocess, sys
        cfg = self.path / 'cfg.json'
        cfg.write_text(dumps({'database': self.config['database'], 'registry_path': self.config['registry_path']}), encoding='utf-8')
        run = lambda out: subprocess.run([sys.executable, '-m', 'substack_outreach', '--config', str(cfg), 'export', '--format', 'sendsheet', '--out', str(out)],
                                         cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(run(self.path / 'sheet.html').returncode, 0)
        self.assertIn('<!doctype html>', (self.path / 'sheet.html').read_text(encoding='utf-8'))
        refused = run(ROOT / 'public' / 'sheet.html')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('public static assets', refused.stderr)

    def test_rss_and_atom_parsing_and_incomplete_content(self):
        rss = b'<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><title>Old</title><link>https://x.example/old</link><pubDate>Tue, 01 Sep 2026 12:00:00 GMT</pubDate><description>Old excerpt</description></item><item><title>New</title><link>https://x.example/new</link><pubDate>Thu, 01 Oct 2026 12:00:00 GMT</pubDate><content:encoded>&lt;p&gt;Available text&lt;/p&gt;</content:encoded></item></channel></rss>'
        rows = parse_feed(rss, 'https://x.example/feed', 1)
        self.assertEqual(rows[0]['title'], 'New')
        self.assertEqual(rows[0]['content_status'], 'excerpt')
        self.assertEqual(rows[0]['text'], 'Available text')
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link href="https://x.example/a"/><summary>Partial</summary><author><name>Sam</name></author></entry></feed>'
        self.assertEqual(parse_feed(atom, 'https://x.example/feed')[0]['byline'], 'Sam')
        with self.assertRaises(ValueError): parse_feed(b'<!DOCTYPE a><rss/>', 'https://x.example/feed')

    def test_private_network_and_api_urls_rejected(self):
        with self.assertRaises(ValueError): public_https('https://127.0.0.1/private')
        with self.assertRaises(ValueError): public_https('https://x.substack.com/api/v1/posts')
        with self.assertRaises(ValueError): public_https('https://substack.com/@someone')

    def test_style_count_required_links_and_hard_max(self):
        pid, p = self.ready()
        d = p['draft']; e = d['evidence_record']
        text = d['plain_text_body']
        count = style_checks(text, d['subject'], e, d['sender'])['body_word_count']
        self.assertTrue(60 <= count <= 90)
        text = text.replace('\n\nExplore Drex:', ' ' + 'word ' * 120 + '\n\nExplore Drex:')
        self.assertIn('BODY_EXCEEDS_110_WORD_HARD_MAX', style_checks(text, '', e, d['sender'])['flags'])
        text = d['plain_text_body'].replace('https://nace.ai/developers', '[link]')
        flags = style_checks(text, '', e, d['sender'])['flags']
        self.assertIn('DEVELOPER_LINK_REQUIRED_AFTER_BODY', flags)
        self.assertIn('UNRESOLVED_PLACEHOLDER', flags)

    def test_edited_article_detail_still_checked(self):
        pid, p = self.ready()
        d = p['draft']
        text = d['plain_text_body'].replace(d['evidence_record']['email_detail'], 'an invented audience preference')
        p = self.p.edit(pid, {'subject': d['subject'], 'plain_text_body': text})
        self.assertIn('ARTICLE_DETAIL_NOT_SUPPORTED', p['draft']['review_flags'])

    def test_live_adapter_request_shape_usage_and_cache_without_network(self):
        pid = self.pub(); self.research(pid); self.approve_facts()
        self.config.update(provider='openai', model='test-model', allow_paid_calls=True,
                           budget_usd_per_run=1, input_usd_per_million=1, output_usd_per_million=2)
        pub = self.p.store.get(pid); facts, _ = self.p.facts()
        payload = writing_input(pub, pub['evidence'], facts, self.config['sender'])
        output = MockWriter().generate(payload, pub['evidence'])
        response = {'status': 'completed', 'id': 'test-response', 'model': 'test-model',
                    'usage': {'input_tokens': 100, 'output_tokens': 200},
                    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': dumps(output)}]}]}
        run = Run(self.p.store, self.config, 'test')
        class FakeResponse(io.BytesIO):
            pass
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-not-real'}), patch('urllib.request.OpenerDirector.open', return_value=FakeResponse(dumps(response).encode())) as call:
            writer = OpenAIWriter(self.p.store, self.config, run)
            result = writer.generate(payload, pub['evidence'])
            self.assertEqual(result['subject'], output['subject'])
            request = call.call_args[0][0]
            body = json.loads(request.data)
            self.assertFalse(body['store'])
            self.assertTrue(body['text']['format']['strict'])
            self.assertNotIn('tools', body)
            self.assertNotIn('test-not-real', request.data.decode())
            writer.generate(payload, pub['evidence'])
            self.assertEqual(call.call_count, 1)
        self.assertEqual(run.requests, 1)
        self.assertEqual(run.usage[0]['usage']['input_tokens'], 100)
        self.assertIsNone(run.usage[0]['actual_cost_usd'])
        self.assertAlmostEqual(run.usage[0]['estimated_cost_usd'], .0005)

    def ollama_response(self, draft, finish='stop'):
        envelope = json.loads((FIXTURES / 'ollama_chat_response.json').read_text(encoding='utf-8'))
        envelope['choices'][0]['message']['content'] = dumps(draft)
        envelope['choices'][0]['finish_reason'] = finish
        class FakeResponse(io.BytesIO):
            pass
        return FakeResponse(dumps(envelope).encode())

    def test_config_provider_whitelist_and_loopback_local_url(self):
        cfg = self.path / 'cfg.json'
        for provider, ok in (('mock', True), ('openai', True), ('local', True), ('anthropic', False), ('ollama', False)):
            cfg.write_text(dumps({'provider': provider}), encoding='utf-8')
            if ok:
                self.assertEqual(load_config(cfg)['provider'], provider)
            else:
                with self.assertRaisesRegex(ValueError, 'Supported writing adapters: mock, openai, local'):
                    load_config(cfg)
        self.assertEqual(load_config()['local_base_url'], 'http://127.0.0.1:11434')
        for url in ('http://localhost:11434', 'http://[::1]:11434'):
            cfg.write_text(dumps({'provider': 'local', 'local_base_url': url}), encoding='utf-8')
            self.assertEqual(load_config(cfg)['local_base_url'], url)
        for url in ('http://192.168.1.20:11434', 'https://ollama.example', 'http://user:pw@127.0.0.1:11434', 'file:///tmp/x', ''):
            cfg.write_text(dumps({'provider': 'local', 'local_base_url': url}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'loopback'):
                load_config(cfg)

    def test_local_writer_request_shape_usage_and_cache(self):
        pid = self.pub(); self.research(pid); self.approve_facts()
        # No budget, pricing or key is needed for a local model.
        self.config.update(provider='local', model='fixture-model', allow_paid_calls=False, budget_usd_per_run=0)
        pub = self.p.store.get(pid); facts, _ = self.p.facts()
        payload = writing_input(pub, pub['evidence'], facts, self.config['sender'])
        output = MockWriter().generate(payload, pub['evidence'])
        run = Run(self.p.store, self.config, 'test')
        with patch.dict('os.environ', {}, clear=True), patch('urllib.request.OpenerDirector.open', return_value=self.ollama_response(output)) as call:
            writer = LocalWriter(self.p.store, self.config, run)
            self.assertEqual(writer.generate(payload, pub['evidence']), output)
            request = call.call_args[0][0]
            self.assertEqual(request.full_url, 'http://127.0.0.1:11434/v1/chat/completions')
            self.assertIsNone(request.get_header('Authorization'))
            body = json.loads(request.data)
            self.assertEqual(body['model'], 'fixture-model')
            self.assertEqual(body['response_format'], {'type': 'json_schema', 'json_schema': {'name': 'outreach_email', 'strict': True, 'schema': SCHEMA}})
            self.assertEqual(body['messages'][0], {'role': 'system', 'content': INSTRUCTIONS})
            self.assertNotIn('tools', body)
            writer.generate(payload, pub['evidence'])
            self.assertEqual(call.call_count, 1)
        self.assertEqual(run.requests, 1)
        self.assertEqual(run.usage[0]['provider'], 'local')
        self.assertEqual(run.usage[0]['usage'], {'input_tokens': 812, 'output_tokens': 164})
        self.assertIsNone(run.usage[0]['actual_cost_usd'])
        self.assertNotIn('estimated_cost_usd', run.usage[0])

    def test_local_writer_rejects_bad_output_and_needs_model(self):
        pid = self.pub(); self.research(pid); self.approve_facts()
        self.config.update(provider='local', model='')
        run = Run(self.p.store, self.config, 'test')
        with self.assertRaisesRegex(ValueError, 'Set model'):
            LocalWriter(self.p.store, self.config, run)
        self.config['model'] = 'fixture-model'
        pub = self.p.store.get(pid); facts, _ = self.p.facts()
        payload = writing_input(pub, pub['evidence'], facts, self.config['sender'])
        for response, message in ((self.ollama_response({'subject': 'x'}), 'expected schema'),
                                  (self.ollama_response(MockWriter().generate(payload, pub['evidence']), 'length'), 'incomplete')):
            with patch('urllib.request.OpenerDirector.open', return_value=response):
                with self.assertRaisesRegex(ValueError, message):
                    LocalWriter(self.p.store, self.config, run).generate(payload, pub['evidence'])

    def test_local_provider_keeps_review_gate_and_one_revision(self):
        pid = self.pub(); self.research(pid); self.approve_facts()
        self.config.update(provider='local', model='fixture-model')
        pub = self.p.store.get(pid); facts, _ = self.p.facts()
        bad = MockWriter().generate(writing_input(pub, pub['evidence'], facts, self.config['sender']), pub['evidence'])
        bad['subject'] = '[recipient]'
        e = self.p.store.get(pid)['evidence']; e['evidence_reviewed_by'] = ''
        self.p.update(pid, {'evidence': e})
        with patch('urllib.request.OpenerDirector.open') as call:
            with self.assertRaisesRegex(ValueError, 'Review the evidence-backed angle'):
                self.p.generate(pid)
            call.assert_not_called()
        e['evidence_reviewed_by'] = 'Test reviewer'
        self.p.update(pid, {'evidence': e})
        with patch('urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: self.ollama_response(bad)) as call:
            d = self.p.generate(pid)['draft']
        self.assertEqual(call.call_count, 2)
        self.assertIn('revision_request', call.call_args_list[1][0][0].data.decode())
        self.assertEqual((d['provider'], d['model']), ('local', 'fixture-model'))
        self.assertIn('UNRESOLVED_PLACEHOLDER', d['review_flags'])
        usage = self.p.store.metrics()['runs'][0]['usage']
        self.assertEqual([u['provider'] for u in usage], ['local', 'local'])

    def test_feed_cache_only_keeps_selected_articles(self):
        pid = self.pub()
        self.config['network_enabled'] = True
        self.p.update(pid, {'research': {'source_kind': 'publisher_rss', 'automated_use_permitted': True,
            'permission_basis': 'Explicit test permission', 'permission_source_url': 'https://testing.example/terms',
            'permission_review_date': '2999-01-01', 'feed_url': 'https://testing.example/feed'}})
        rss = ('<rss><channel>' + ''.join(f'<item><title>Article {n}</title><link>https://testing.example/p/{n}</link><description>Text {n}</description></item>' for n in range(10)) + '</channel></rss>').encode()
        run = Run(self.p.store, self.config, 'test')
        collector = Collector(self.p.store, self.config, run)
        with patch.object(collector, 'allowed_get', return_value=rss) as fetch:
            self.assertEqual(len(collector.collect(self.p.store.get(pid))), 5)
            self.assertEqual(len(collector.collect(self.p.store.get(pid))), 5)
            self.assertEqual(fetch.call_count, 1)
        cached = self.p.store.db.execute("SELECT data FROM cache WHERE key LIKE 'rss:%'").fetchone()[0]
        self.assertNotIn('Article 9', cached)

    def test_unsupported_api_derived_research_rejected(self):
        pid = self.pub()
        with self.assertRaisesRegex(ValueError, 'API-derived'):
            save_articles(self.p.store, self.p.store.get(pid), [{'url': 'https://testing.example/post', 'text': 'Text', 'source_kind': 'substack_api', 'permission_basis': 'Unknown'}], supplied=True)

    def test_generation_dependency_does_not_erase_completed_research(self):
        pid = self.pub(); self.research(pid)
        self.config['provider'] = 'openai'
        self.p.process(20)
        pub = self.p.store.get(pid)
        self.assertEqual(pub['status'], 'Researched')
        self.assertIn('Paid calls require', pub['draft_error'])
        self.assertEqual(len(pub['articles']), 1)
        self.assertEqual(self.p.store.metrics()['generation_failed_attempts'], 1)
        self.assertEqual(self.p.store.metrics()['research_failed_attempts'], 0)


    def test_every_statement_the_code_issues_translates_to_postgres(self):
        from substack_outreach.db import translate
        recorder = RecordingConnection(self.p.store.db)
        self.p.store.db = recorder
        pid, _ = self.ready()
        self.p.action(pid, 'approve', {'reviewer': 'Human', 'semantic_review_confirmed': True})
        self.p.export('csv'); self.p.export('sendsheet'); self.p.queue(search='Test'); self.p.store.metrics()
        self.p.match_publication('https://testing.example')
        other = self.pub('Other', 'author:7')
        self.p.action(other, 'history', {'channel': 'X', 'source': 'https://x.example/1'})
        self.p.action(other, 'dnc', {'reason': 'Opt-out'})
        self.config.update(collect_contacts=True, network_enabled=True)
        third = self.bare_pub('Third', 'https://third.example', 'author:3')
        with patch.object(Collector, 'allowed_get', return_value=(FIXTURES / 'about_page.html').read_bytes()):
            self.p.collect_contacts(5)
        add_fixture(self.p)
        push_registry(self.p.store, self.facts, 'Test')
        self.assertGreater(len(set(recorder.sql)), 40)
        for sql in set(recorder.sql):
            text, _ = translate(sql)
            if text is None:
                continue
            for sqlite_only in ('INSERT OR', 'json_extract', 'json_set', 'PRAGMA'):
                self.assertNotIn(sqlite_only, text, sql)

    def test_translation_rules(self):
        from substack_outreach.db import translate
        self.assertEqual(translate("INSERT OR REPLACE INTO cache VALUES(?,?,?)"),
                         ("INSERT INTO cache(key,data,at) VALUES(?,?,?) ON CONFLICT (key) DO UPDATE SET data=EXCLUDED.data, at=EXCLUDED.at", False))
        self.assertEqual(translate("INSERT OR IGNORE INTO identities VALUES(?,?)"), ("INSERT INTO identities VALUES(?,?) ON CONFLICT DO NOTHING", False))
        self.assertEqual(translate("INSERT INTO runs(kind,started_at) VALUES(?,?)"), ("INSERT INTO runs(kind,started_at) VALUES(?,?) RETURNING id", True))
        self.assertEqual(translate("SELECT id FROM publications WHERE json_extract(data,'$.publication_url')=?")[0],
                         "SELECT id FROM publications WHERE (data::jsonb->>'publication_url')=?")
        self.assertEqual(translate("SELECT count(*) FROM publications WHERE data LIKE ?")[0], "SELECT count(*) FROM publications WHERE data ILIKE ?")
        self.assertEqual(translate("PRAGMA foreign_keys=ON"), (None, False))

    def test_postgres_connection_with_a_stand_in_driver(self):
        import sys, types
        from substack_outreach.db import PgConnection
        calls = {'sql': [], 'commits': 0, 'rollbacks': 0}
        class FakeCursor:
            description = None
            def execute(self, sql, params):
                calls['sql'].append((sql, params))
                self.description = [('id',)] if 'RETURNING id' in sql else ([('status',)] if sql.startswith('SELECT') else None)
            def fetchall(self):
                return [[41]] if self.description == [('id',)] else [['Approved']]
            def close(self):
                pass
        class FakeConnection:
            def cursor(self): return FakeCursor()
            def commit(self): calls['commits'] += 1
            def rollback(self): calls['rollbacks'] += 1
            def close(self): pass
        dbapi = types.SimpleNamespace(paramstyle='format', connect=lambda **kw: calls.update(kwargs=kw) or FakeConnection())
        module = types.ModuleType('pg8000'); module.dbapi = dbapi
        with patch.dict(sys.modules, {'pg8000': module, 'pg8000.dbapi': dbapi}):
            db = PgConnection('postgresql://user:p%40ss@db.example:5433/outreach?sslmode=require')
            self.assertEqual(dbapi.paramstyle, 'qmark')
            self.assertEqual({k: calls['kwargs'][k] for k in ('user', 'password', 'host', 'port', 'database')},
                             {'user': 'user', 'password': 'p@ss', 'host': 'db.example', 'port': 5433, 'database': 'outreach'})
            self.assertIsNotNone(calls['kwargs']['ssl_context'])
            with db:
                self.assertEqual(db.execute('INSERT INTO runs(kind,started_at) VALUES(?,?)', ('x', 'now')).lastrowid, 41)
            self.assertEqual(db.execute('SELECT status FROM publications').fetchone()['status'], 'Approved')
            with self.assertRaises(RuntimeError), db:
                raise RuntimeError('boom')
        self.assertEqual((calls['commits'], calls['rollbacks']), (1, 1))
        self.assertEqual(calls['sql'][0], ('INSERT INTO runs(kind,started_at) VALUES(?,?) RETURNING id', ('x', 'now')))

    def test_postgres_rows_behave_like_sqlite_rows(self):
        from substack_outreach.db import Cursor, Row, is_postgres, resolve
        row = Row((7, 'Approved'), ['id', 'status'])
        self.assertEqual((row[0], row['status'], dict(row)), (7, 'Approved', {'id': 7, 'status': 'Approved'}))
        self.assertEqual(Cursor([row], 7).lastrowid, 7)
        with patch.dict('os.environ', {'DATABASE_URL': 'postgresql://u:p@db.example/x'}):
            self.assertTrue(is_postgres('env:DATABASE_URL'))
        with patch.dict('os.environ', {}, clear=True), self.assertRaisesRegex(ValueError, 'DATABASE_URL is not set'):
            resolve('env:DATABASE_URL')
        self.assertFalse(is_postgres(str(self.path / 'x.sqlite3')))

    def test_registry_from_database_and_actor_attribution(self):
        self.config['registry_path'] = 'database'
        with self.assertRaisesRegex(ValueError, 'run facts-push'):
            self.p.facts()
        first = push_registry(self.p.store, self.facts, 'Pusher')
        self.assertEqual((first['entries'], first['approved']), (len(self.facts['entries']), 0))
        self.assertEqual(self.p.facts()[0]['version'], self.facts['version'])
        newer = copy.deepcopy(self.facts); newer['version'] += 1
        push_registry(self.p.store, newer, 'Pusher')
        self.assertEqual(self.p.facts()[0]['version'], newer['version'])
        bad = copy.deepcopy(self.facts); bad['entries'][0]['approval_status'] = 'approved'
        with self.assertRaisesRegex(ValueError, 'approved_by and approved_at'):
            push_registry(self.p.store, bad, 'Pusher')
        self.p.store.close()
        self.p = Pipeline(self.config, actor='reviewer@example.com')
        pid = self.pub()
        self.p.action(pid, 'skip', {'reason': 'Not a fit'})
        self.assertEqual(self.p.store.db.execute("SELECT actor FROM events WHERE kind='approval_invalidated'").fetchone()[0], 'reviewer@example.com')

    def test_hosted_config_fixes_storage_and_refuses_local_provider(self):
        config = load_hosted_config({'SUBSTACK_CONFIG': dumps({'database': 'elsewhere.sqlite3', 'batch_size': 5})})
        self.assertEqual((config['database'], config['registry_path'], config['hosted'], config['batch_size']), ('env:DATABASE_URL', 'database', True, 5))
        with self.assertRaisesRegex(ValueError, 'not valid JSON'):
            load_hosted_config({'SUBSTACK_CONFIG': '{bad'})
        with self.assertRaisesRegex(ValueError, 'provider local'):
            load_hosted_config({'SUBSTACK_CONFIG': dumps({'provider': 'local', 'model': 'm'})})


class TeamPageTests(unittest.TestCase):
    def test_team_page_bakes_substack_rows_ranked_and_escaped(self):
        sys_path = str(ROOT / 'team_page')
        import sys
        sys.path.insert(0, sys_path)
        try:
            import build_team_page as tp
        finally:
            sys.path.remove(sys_path)
        sample = json.loads((FIXTURES / 'all_channels_sample.json').read_text(encoding='utf-8'))
        table = sample['sheets']['substack']
        table['rows'].append(dict(table['rows'][0], _row=9, pub_id='1009', author_id='599', name='Close </script> tag',
                                  url='https://closetag.substack.com', hero_text='LLM inference notes'))
        rows = tp.writers(table, load_dnc(FIXTURES / 'dnc.json'))
        self.assertEqual([w['name'] for w in rows][:2], ['Agent Notes', 'Close </script> tag'])
        self.assertEqual([w['short'] for w in rows], [1, 1, 1, 0])  # the garden diary has no topical signal
        page = tp.render(rows)
        self.assertNotIn('__WRITERS__', page)
        data = page.split('<script type="application/json" id="writers-data">')[1].split('</script>')[0]
        self.assertEqual(len(json.loads(data)), 4)
        self.assertNotIn('discord.gg', page)

class WorkbookErrorTests(unittest.TestCase):
    """Structurally broken .xlsx files must fail as one ValueError, never a raw traceback."""
    NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def zip(self, name, members):
        p = self.path / name
        with ZipFile(p, 'w') as z:
            for member, body in members.items():
                z.writestr(member, body)
        return p

    def book(self, rel_target='worksheets/sheet1.xml'):
        return {'xl/workbook.xml': f'<workbook xmlns="{self.NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="substack" r:id="rId1"/></sheets></workbook>',
                'xl/_rels/workbook.xml.rels': f'<Relationships><Relationship Id="rId1" Target="{rel_target}"/></Relationships>'}

    def assert_readable_error(self, p):
        with self.assertRaises(ValueError) as ctx:
            read_workbook(p)
        self.assertIn('not a readable .xlsx', str(ctx.exception))

    def test_not_a_zip(self):
        p = self.path / 'plain.xlsx'
        p.write_bytes(b'hello')
        self.assert_readable_error(p)

    def test_missing_sheet_relationship(self):
        members = self.book()
        members['xl/_rels/workbook.xml.rels'] = '<Relationships></Relationships>'
        self.assert_readable_error(self.zip('norel.xlsx', members))

    def test_shared_string_index_out_of_range(self):
        members = self.book()
        members['xl/sharedStrings.xml'] = f'<sst xmlns="{self.NS}"><si><t>name</t></si></sst>'
        members['xl/worksheets/sheet1.xml'] = f'<worksheet xmlns="{self.NS}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>7</v></c></row></sheetData></worksheet>'
        self.assert_readable_error(self.zip('badstr.xlsx', members))

    def test_mapping_errors_keep_their_specific_message(self):
        members = self.book()
        members['xl/worksheets/sheet1.xml'] = f'<worksheet xmlns="{self.NS}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>name</t></is></c><c r="B1" t="inlineStr"><is><t>name</t></is></c></row></sheetData></worksheet>'
        with self.assertRaises(ValueError) as ctx:
            read_workbook(self.zip('dup.xlsx', members))
        self.assertIn('Duplicate workbook headers', str(ctx.exception))


class CliConfigErrorTests(unittest.TestCase):
    """A bad --config must produce a one-line Error and exit 2, not a stack trace."""

    def run_cli(self, *args):
        import subprocess, sys
        return subprocess.run([sys.executable, '-m', 'substack_outreach', *args], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')

    def test_missing_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_cli('--config', str(Path(d) / 'nope.json'), 'metrics')
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith('Error: config file not found'), r.stderr)
        self.assertNotIn('Traceback', r.stderr)

    def test_invalid_json_config(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / 'bad.json'
            bad.write_text('{bad', encoding='utf-8')
            r = self.run_cli('--config', str(bad), 'metrics')
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith('Error: config is not valid JSON'), r.stderr)
        self.assertNotIn('Traceback', r.stderr)

class RecordingConnection:
    """Wraps the SQLite connection and records every statement the code issues."""
    def __init__(self, db):
        self.db, self.sql = db, []

    def execute(self, sql, params=()):
        self.sql.append(sql)
        return self.db.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.db, name)

    def __enter__(self):
        return self.db.__enter__()

    def __exit__(self, *exc):
        return self.db.__exit__(*exc)


class FakeSocket:
    def __init__(self, data):
        self.rfile, self.out = io.BytesIO(data), io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        return self.rfile

    def sendall(self, data):
        self.out.write(data)


class HostedTests(unittest.TestCase):
    def setUp(self):
        from substack_outreach import auth
        self.auth = auth
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        fx = json.loads((FIXTURES / 'google_claims.json').read_text(encoding='utf-8'))
        self.env = dict(fx['env'], DATABASE_URL=str(self.path / 'hosted.sqlite3'), SUBSTACK_CONFIG=dumps({'batch_size': 5}))
        self.claims, self.token_response = fx['claims'], fx['token_response']
        self.settings = auth.AuthSettings.from_env(self.env)
        # The storage adapter resolves env:DATABASE_URL from the process environment, as on Vercel.
        self.environ = patch.dict('os.environ', {'DATABASE_URL': self.env['DATABASE_URL']})
        self.environ.start()
        facts = json.loads((ROOT / 'substack_outreach/facts.example.json').read_text(encoding='utf-8'))
        p = Pipeline(load_hosted_config(self.env))
        push_registry(p.store, facts, 'Test')
        cur = p.store.db.execute('INSERT INTO publications(data,score,author_key,pilot,shortlisted,status) VALUES(?,100,?,1,1,?)',
                                 (dumps({'publication': 'Hosted test', 'publication_url': 'https://hosted.example', 'source': {}, 'fit_category': FITS[0],
                                         'fit_reasons': [], 'import_flags': [], 'research': {}, 'evidence': None, 'author_name': 'A'}), 'author:1', 'Shortlisted'))
        self.pid = cur.lastrowid
        p.store.db.commit(); p.store.close()

    def tearDown(self):
        self.environ.stop()
        self.temp.cleanup()

    def request(self, method, path, headers=None, body=b''):
        from substack_outreach.hosted import handler
        env = self.env
        class Handler(handler):
            environ = env
        lines = [f'{method} {path} HTTP/1.1', 'Host: outreach.example.test'] + [f'{k}: {v}' for k, v in (headers or {}).items()]
        if body:
            lines.append(f'Content-Length: {len(body)}')
        sock = FakeSocket(('\r\n'.join(lines) + '\r\n\r\n').encode() + body)
        Handler(sock, ('127.0.0.1', 0), None)
        head, _, payload = sock.out.getvalue().partition(b'\r\n\r\n')
        head = head.decode('latin-1').split('\r\n')
        return int(head[0].split()[1]), [h.split(': ', 1) for h in head[1:]], payload

    def signed_in(self, email='Reviewer@Example.com'):
        value = self.auth.session_value(self.settings, {'sub': '1', 'email': email.lower()})
        return f'{self.auth.SESSION_COOKIE}={value}'

    def test_sign_in_flow_and_signed_out_refusal(self):
        status, _, body = self.request('GET', '/api/index?path=substack/bootstrap')
        self.assertEqual((status, json.loads(body)['login']), (401, '/api/auth/login'))
        status, headers, _ = self.request('GET', '/api/auth/login')
        location = dict(headers)['Location']
        self.assertEqual(status, 302)
        self.assertTrue(location.startswith('https://accounts.google.com/o/oauth2/v2/auth?'))
        params = {k: v[0] for k, v in parse_qs(location.split('?', 1)[1]).items()}
        self.assertEqual((params['redirect_uri'], params['scope'], params['client_id']),
                         ('https://outreach.example.test/api/auth/callback', 'openid email', self.settings.client_id))
        flow_cookie = next(v for k, v in headers if k == 'Set-Cookie' and v.startswith('ss_oauth='))
        self.assertIn('HttpOnly; Secure; SameSite=Lax', flow_cookie)
        flow = flow_cookie.split(';')[0]
        claims = dict(self.claims, nonce=params['nonce'], exp=int(__import__('time').time()) + 600)
        with patch('substack_outreach.auth.exchange_code', return_value=claims):
            status, _, _ = self.request('GET', '/api/auth/callback?code=c&state=wrong', {'Cookie': flow})
            self.assertEqual(status, 400)
            status, headers, _ = self.request('GET', f"/api/auth/callback?code=c&state={params['state']}", {'Cookie': flow})
        self.assertEqual((status, dict(headers)['Location']), (302, '/'))
        session = next(v for k, v in headers if k == 'Set-Cookie' and v.startswith('ss_session=')).split(';')[0]
        status, _, body = self.request('GET', '/api/substack/bootstrap', {'Cookie': session})
        boot = json.loads(body)
        self.assertEqual((status, boot['user'], boot['hosted']), (200, {'email': 'reviewer@example.com'}, True))
        self.assertEqual(boot['token'], self.auth.csrf_token(self.settings, session))

    def test_mutations_need_origin_and_session_token_and_record_actor(self):
        cookie = self.signed_in()
        token = self.auth.csrf_token(self.settings, cookie)
        body = dumps({'reason': 'Not a fit'}).encode()
        base = {'Cookie': cookie, 'Content-Type': 'application/json', 'Origin': 'https://outreach.example.test', 'X-Local-Token': token}
        for broken in ({'Origin': 'https://evil.example'}, {'X-Local-Token': 'nope'}):
            status, _, _ = self.request('POST', f'/api/substack/publication/{self.pid}/skip', base | broken, body)
            self.assertEqual(status, 403)
        status, _, _ = self.request('POST', f'/api/substack/publication/{self.pid}/skip', {k: v for k, v in base.items() if k != 'Cookie'}, body)
        self.assertEqual(status, 401)
        status, _, payload = self.request('POST', f'/api/index?path=substack/publication/{self.pid}/skip', base, body)
        self.assertEqual((status, json.loads(payload)['status']), (200, 'Skipped'))
        p = Pipeline(load_hosted_config(self.env))
        self.assertEqual(p.store.db.execute("SELECT actor FROM events WHERE kind='approval_invalidated'").fetchone()[0], 'reviewer@example.com')
        p.store.close()

    def test_claims_cookies_and_settings_are_checked(self):
        auth, s = self.auth, self.settings
        now = 1_800_000_000
        good = dict(self.claims, nonce='n', exp=now + 60)
        self.assertEqual(auth.check_claims(s, good, 'n', now), {'sub': '1234567890', 'email': 'reviewer@example.com'})
        for change, message in (({'aud': 'other'}, 'not issued'), ({'iss': 'https://evil.example'}, 'not issued'), ({'exp': now - 1}, 'expired'),
                                ({'nonce': 'x'}, 'matched'), ({'email_verified': False}, 'confirm an email')):
            with self.assertRaisesRegex(ValueError, message):
                auth.check_claims(s, dict(good, **change), 'n', now)
        domain = auth.AuthSettings.from_env(dict(self.env, ALLOWED_EMAIL_DOMAIN='nace.ai'))
        with self.assertRaisesRegex(ValueError, 'only accepts accounts from nace.ai'):
            auth.check_claims(domain, good, 'n', now)
        listed = auth.AuthSettings.from_env(dict(self.env, ALLOWED_EMAILS='someone@else.example'))
        with self.assertRaisesRegex(ValueError, 'sign-in list'):
            auth.check_claims(listed, good, 'n', now)
        value = auth.session_value(s, {'sub': '1', 'email': 'a@b.example'}, now)
        self.assertEqual(auth.read_session(s, f'ss_session={value}', now + 10), {'sub': '1', 'email': 'a@b.example'})
        self.assertIsNone(auth.read_session(s, f'ss_session={value}', now + auth.SESSION_SECONDS + 1))
        tampered = value[:-2] + ('AA' if not value.endswith('AA') else 'BB')
        self.assertIsNone(auth.read_session(s, f'ss_session={tampered}', now + 10))
        for missing in ('SESSION_SECRET', 'APP_ORIGIN'):
            with self.assertRaisesRegex(ValueError, missing):
                auth.AuthSettings.from_env({k: v for k, v in self.env.items() if k != missing})
        with self.assertRaisesRegex(ValueError, 'APP_ORIGIN'):
            auth.AuthSettings.from_env(dict(self.env, APP_ORIGIN='http://outreach.example.test'))

    def test_code_exchange_reads_only_the_id_token(self):
        payload = dict(self.claims, nonce='n', exp=1)
        id_token = 'h.' + self.auth.b64(dumps(payload).encode()) + '.sig'
        response = io.BytesIO(dumps(dict(self.token_response, id_token=id_token)).encode())
        opener = type('Opener', (), {'open': lambda self, request, timeout: response})()
        self.assertEqual(self.auth.exchange_code(self.settings, 'code', opener)['sub'], '1234567890')
        failing = type('Opener', (), {'open': lambda self, request, timeout: io.BytesIO(b'{"error": "invalid_grant", "access_token": "secret"}')})()
        with self.assertRaises(ValueError) as ctx:
            self.auth.exchange_code(self.settings, 'code', failing)
        self.assertNotIn('secret', str(ctx.exception))

    def test_missing_configuration_is_a_clear_500(self):
        self.env.pop('GOOGLE_CLIENT_SECRET')
        status, _, body = self.request('GET', '/api/substack/bootstrap')
        self.assertEqual(status, 500)
        self.assertIn('GOOGLE_CLIENT_SECRET', json.loads(body)['error'])

    def test_vercel_entry_point_and_private_inputs(self):
        ignore = (ROOT / '.vercelignore').read_text(encoding='utf-8')
        for private in ('.local/', '*.xlsx', 'substack.config.json', 'substack_outreach/facts.json', 'substack_outreach/dnc.json', '*.sqlite3'):
            self.assertIn(private, ignore.splitlines())
        import tomllib
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
        self.assertEqual(project['tool']['vercel']['entrypoint'], 'app:app')
        self.assertEqual(project['project']['dependencies'], ['pg8000==1.31.2'])
        # New Vercel projects ignore api/*.py functions; a stale vercel.json "functions" entry fails the build.
        self.assertFalse((ROOT / 'api').exists())
        # The Python preset makes Vercel run app.py even when the dashboard says "static site".
        self.assertEqual(json.loads((ROOT / 'vercel.json').read_text(encoding='utf-8'))['framework'], 'python')
        self.assertNotIn('functions', json.loads((ROOT / 'vercel.json').read_text(encoding='utf-8')))
        import app
        self.assertIs(app.app, __import__('substack_outreach.hosted', fromlist=['app']).app)

    def wsgi(self, method, path, headers=None, body=b''):
        from substack_outreach.hosted import app
        query = path.split('?', 1)[1] if '?' in path else ''
        environ = {'REQUEST_METHOD': method, 'PATH_INFO': path.split('?')[0], 'QUERY_STRING': query, 'wsgi.input': io.BytesIO(body)}
        for name, value in (headers or {}).items():
            key = name.upper().replace('-', '_')
            environ[key if key in ('CONTENT_TYPE', 'CONTENT_LENGTH') else 'HTTP_' + key] = value
        if body:
            environ['CONTENT_LENGTH'] = str(len(body))
        seen = {}
        with patch.dict('os.environ', self.env):
            payload = b''.join(app(environ, lambda status, pairs: seen.update(status=status, headers=pairs)))
        return int(seen['status'].split()[0]), seen['headers'], payload

    def test_wsgi_app_serves_page_signin_and_mutations_like_vercel(self):
        status, headers, body = self.wsgi('GET', '/')
        self.assertEqual((status, dict(headers)['Content-Type']), (200, 'text/html; charset=utf-8'))
        self.assertIn(b'id="tab-substack"', body)
        self.assertEqual(self.wsgi('GET', '/api/substack/bootstrap')[0], 401)
        self.assertEqual(self.wsgi('GET', '/nothing-here')[0], 404)
        cookie = self.signed_in()
        status, _, body = self.wsgi('GET', '/api/substack/bootstrap', {'Cookie': cookie})
        self.assertEqual((status, json.loads(body)['user']['email']), (200, 'reviewer@example.com'))
        status, headers, _ = self.wsgi('GET', '/api/substack/export?format=csv&scope=pilot', {'Cookie': cookie})
        self.assertTrue(dict(headers)['Content-Disposition'].startswith('attachment; filename="substack-pilot-'))
        payload = dumps({'reason': 'Not a fit'}).encode()
        base = {'Cookie': cookie, 'Content-Type': 'application/json; charset=utf-8', 'Origin': 'https://outreach.example.test',
                'X-Local-Token': self.auth.csrf_token(self.settings, cookie)}
        status, _, body = self.wsgi('POST', f'/api/substack/publication/{self.pid}/skip', base, payload)
        self.assertEqual((status, json.loads(body)['status']), (200, 'Skipped'))
        self.assertEqual(self.wsgi('POST', f'/api/substack/publication/{self.pid}/skip', base | {'Origin': 'https://evil.example'}, payload)[0], 403)



if __name__ == '__main__':
    unittest.main()
