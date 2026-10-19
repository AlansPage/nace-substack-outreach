import copy
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from substack_outreach.common import FITS, canonical, classify, dumps, html_email, is_profile, now, publication_url
from substack_outreach.config import ROOT, load_config
from substack_outreach.evidence import ASKS, BASE_CLAIMS, evidence_checks, prepare_evidence, style_checks
from substack_outreach.fixtures import add_fixture, previews
from substack_outreach.models import MockWriter, OpenAIWriter, writing_input
from substack_outreach.pipeline import Pipeline, csv_safe
from substack_outreach.sources import Collector, Run, extract_contacts, parse_feed, public_https, save_articles
from substack_outreach.store import load_dnc
from substack_outreach.workbook import identifier, read_workbook

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


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
        self.config['database'] = str(self.path / 'test.sqlite3')
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
        self.p.store.db.execute("UPDATE publications SET data=json_set(data,'$.publication_url','') WHERE id=?", (pid,))
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

if __name__ == '__main__':
    unittest.main()
