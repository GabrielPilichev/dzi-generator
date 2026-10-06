"""DZI ownership, readiness and stored-context authorization for open questions."""
from __future__ import annotations

import json
from html.parser import HTMLParser
import re
from unittest.mock import patch

from src.dzi_open_questions import fetch_dzi_open_questions
from src.generate_dzi_training_set import get_pool, choose_questions, allocate_counts
from src.subject_assessment import AssessmentScope
from tests.dzi_open_fixtures import mark_dzi_question
from tests.subject_fixtures import add_curriculum, add_membership, add_question, add_section, add_topic, four_options
from tests.support import IsolatedAppTestCase, web_app
from web.services import dzi_exams
from web.services import practical as practical_service
from web.services import quiz as quiz_service

OTHER_SUBJECTS = ('physics', 'biology', 'chemistry', 'mathematics', 'informatics')


class PracticalPanelProbe(HTMLParser):
    """Templates are inert; count only panels mounted in the document."""
    def __init__(self, html):
        super().__init__()
        self.template_depth = 0
        self.panels = 0
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'template':
            self.template_depth += 1
        if self.template_depth:
            return
        attrs = dict(attrs)
        if 'practical-assignment-panel' in attrs.get('class', '').split():
            self.panels += 1
        if tag == 'a':
            self.links.append(attrs.get('href', ''))

    def handle_endtag(self, tag):
        if tag == 'template':
            self.template_depth -= 1


class OpenQuestionScopeTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        conn = web_app.quiz_db()
        try:
            cls.sections, cls.slugs, cls.topics, cls.closed, cls.opens = {}, {}, {}, {}, {}
            cls.base_ready = {q['id'] for q in fetch_dzi_open_questions(conn)}
            number = iter(range(9000, 9500))
            def section(key, subject, grade, kind='content'):
                curriculum = add_curriculum(conn, subject, grade, 'oq-' + key)
                slug = 'oq-' + key
                sid = add_section(conn, curriculum, slug, 'OQ ' + key, grade=grade, section_type=kind)
                cls.sections[key], cls.slugs[key] = sid, slug
                topic = add_topic(conn, sid, slug + '-topic', grade=grade)
                cls.topics[key] = topic
                cls.closed[key] = [add_question(conn, topic, subject, next(number), source='oq-mc',
                                               options=four_options(), link_topic=True) for _ in range(3)]
            for grade in range(8, 13):
                section('it' + str(grade), 'it', grade)
            for subject in OTHER_SUBJECTS:
                section(subject, subject, 8)
            section('dzi', 'it', 12, 'dzi_training')
            section('dzi2', 'it', 11, 'dzi_training')
            section('physics-dzi', 'physics', 8, 'dzi_training')

            def open_question(key, source, *, topic=None, answer='отговор', prompt=None, owned=True):
                qid = add_question(conn, topic, 'informatika_it', next(number), source=source,
                                   kind='fill_in', accepted_answer=answer, prompt=prompt,
                                   link_topic=topic is not None)
                if owned:
                    mark_dzi_question(conn, qid)
                cls.opens[key] = qid
            # Ready DZI records may have ordinary or missing topics.
            open_question('a1', 'oq-source-a', topic=cls.topics['it8'])
            open_question('a2', 'oq-source-a')
            open_question('b', 'oq-source-b', topic=cls.topics['dzi'])
            open_question('answerless', 'oq-answerless', topic=cls.topics['it8'], answer=None)
            open_question('visual', 'oq-visual', topic=cls.topics['it8'], prompt='Според диаграмата попълнете стойността.')
            open_question('ordinary', 'oq-ordinary', topic=cls.topics['it8'], owned=False)
            open_question('wrong-exam', 'oq-wrong-exam', topic=cls.topics['it8'])
            conn.execute("UPDATE exams SET subject='physics' WHERE id=(SELECT exam_id FROM questions WHERE id=?)",
                         (cls.opens['wrong-exam'],))
            open_question('wrong-year', 'oq-wrong-year', topic=cls.topics['it8'])
            conn.execute('UPDATE questions SET year=2089 WHERE id=?', (cls.opens['wrong-year'],))
            # Taxonomy overlap across every IT grade must never authorize DZI opens.
            for key in ('it9', 'it10', 'it11', 'it12', 'dzi'):
                add_membership(conn, cls.topics['it8'], cls.sections[key])
            conn.commit()
            cls.expected_ready = cls.base_ready | {cls.opens[k] for k in ('a1', 'a2', 'b')}
        finally:
            conn.close()

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['tester_authenticated'] = True
            session['admin_authenticated'] = True
            session['ui_profile'] = 'admin'

    def summary(self, key):
        conn = web_app.quiz_db()
        try:
            sid = self.sections[key]
            return web_app.open_question_scope_summary(conn, sid, AssessmentScope(conn, sid))
        finally:
            conn.close()

    def form(self, key, subject='information-technologies'):
        response = self.client.get('/teacher/new', query_string={'subject': subject, 'section': self.slugs[key]})
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def assignments(self):
        conn = web_app.quiz_db()
        try:
            return conn.execute('SELECT COUNT(*) FROM quiz_assignments').fetchone()[0]
        finally:
            conn.close()

    def create(self, key, **fields):
        subject = key if key in OTHER_SUBJECTS else 'information-technologies'
        return self.client.post('/teacher/new', data={'subject': subject, 'section_id': self.sections[key],
                                                     'question_count': 3, **fields})

    def assert_rejected(self, response, before):
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True)[:300])
        self.assertEqual(self.assignments(), before)

    def plan(self, response):
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:300])
        qid = int(response.headers['Location'].rsplit('/', 1)[-1])
        conn = web_app.quiz_db()
        try:
            row = conn.execute('SELECT * FROM quiz_assignments WHERE id=?', (qid,)).fetchone()
            return dict(row)
        finally:
            conn.close()

    def test_every_ordinary_it_grade_and_other_subject_has_zero(self):
        for key in [*(f'it{g}' for g in range(8, 13)), *OTHER_SUBJECTS, 'physics-dzi']:
            with self.subTest(context=key):
                self.assertEqual(self.summary(key), {'count': 0, 'sources': []})

    def test_ordinary_it_controls_hidden_and_disabled_despite_dzi_capability(self):
        for grade in range(8, 13):
            with self.subTest(grade=grade):
                html = self.form('it' + str(grade))
                self.assertIn('id="open-question-panel" hidden', html)
                self.assertRegex(html, r'<input id="open_count"[^>]*max="0"[^>]*disabled')
                source_select = re.search(r'<select id="source_slug".*?</select>', html, re.S).group(0)
                self.assertNotIn('oq-source', source_select)
                self.assertIn('disabled', source_select)

    def test_non_it_form_has_no_dzi_open_controls(self):
        for subject in OTHER_SUBJECTS:
            with self.subTest(subject=subject):
                html = self.form(subject, subject)
                self.assertNotIn('name="open_count"', html)
                self.assertNotIn('Налични кандидати', html)

    def test_ordinary_grades_and_other_subjects_mount_no_dzi_practical_panel(self):
        conn = web_app.quiz_db()
        try:
            for key in [*(f'it{g}' for g in range(8, 13)), *OTHER_SUBJECTS]:
                with self.subTest(context=key):
                    subject = key if key in OTHER_SUBJECTS else 'information-technologies'
                    page = PracticalPanelProbe(self.form(key, subject))
                    self.assertEqual(page.panels, 0)
                    self.assertFalse(any('/dzi/source/' in link and '/practical' in link
                                         for link in page.links))
                    self.assertEqual(practical_service.fetch_practical_assignment_options(
                        conn, scope=AssessmentScope(conn, self.sections[key])), [])
        finally:
            conn.close()

    def test_explicit_dzi_sections_keep_practical_resources_despite_shared_topics(self):
        conn = web_app.quiz_db()
        try:
            for key in ('dzi', 'dzi2'):
                with self.subTest(context=key):
                    scope = AssessmentScope(conn, self.sections[key])
                    self.assertTrue(scope.allows_dzi_content)
                    sources = practical_service.fetch_practical_assignment_options(conn, scope=scope)
                    self.assertTrue(sources)
                    self.assertTrue(all(source['level'] == 'DZI' for source in sources))
                    page = PracticalPanelProbe(self.form(key))
                    self.assertEqual(page.panels, 1)
                    for source in sources:
                        self.assertIn('/dzi/source/' + source['source_slug'] + '/practical', page.links)
            self.assertEqual(PracticalPanelProbe(self.form('physics-dzi', 'physics')).panels, 0)
        finally:
            conn.close()

    def test_practical_source_identity_requires_dzi_exam_not_just_task_or_label(self):
        conn = web_app.quiz_db()
        try:
            scope = AssessmentScope(conn, self.sections['dzi'])
            before = practical_service.fetch_practical_assignment_options(conn, scope=scope)
            for year, subject, level in ((2091, 'informatika_it', 'school'), (2092, 'physics', 'DZI')):
                exam = conn.execute('INSERT INTO exams (subject,level,year,session,variant,format_version,source_file) VALUES (?,?,?,\'may\',1,?,?)',
                                    (subject, level, year, web_app.DZI_FORMAT_VERSION, f'practical-fixture-{year}.pdf')).lastrowid
                task = conn.execute("INSERT INTO exam_tasks (exam_id,task_number,exam_part,task_kind,points) VALUES (?,26,2,'practical_spreadsheet',15)",
                                    (exam,)).lastrowid
                conn.execute("INSERT INTO practical_task_resources (exam_task_id,resource_path,original_filename) VALUES (?,'data/reference/fake.xlsx','ДЗИ.xlsx')",
                             (task,))
            self.assertEqual(practical_service.fetch_practical_assignment_options(conn, scope=scope), before)
        finally:
            conn.rollback()
            conn.close()

    def test_ordinary_practical_question_remains_visible_on_section_and_topic(self):
        conn = web_app.quiz_db()
        try:
            qid = add_question(conn, self.topics['it8'], 'it', 9901, source='ordinary-practical',
                               kind='practical', prompt='Обикновено практическо упражнение', link_topic=True)
            conn.commit()
        finally:
            conn.close()
        try:
            path = '/subjects/information-technologies/8/' + self.slugs['it8']
            for url in (path, path + '/oq-it8-topic'):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn('Обикновено практическо упражнение', html)
                self.assertEqual(PracticalPanelProbe(html).panels, 0)
        finally:
            conn = web_app.quiz_db()
            conn.execute('DELETE FROM question_topic_assignments WHERE question_id=?', (qid,))
            conn.execute('DELETE FROM questions WHERE id=?', (qid,))
            conn.commit()
            conn.close()

    def test_dzi_context_uses_global_pool_even_without_topic_or_membership(self):
        conn = web_app.quiz_db()
        try:
            for key in ('dzi', 'dzi2'):
                scope = AssessmentScope(conn, self.sections[key])
                result = web_app.fetch_open_question_candidates(conn, section_id=scope.section_id, scope=scope)
                self.assertEqual({q['question_id'] for q in result}, self.expected_ready)
                self.assertEqual(self.summary(key)['count'], len(self.expected_ready))
                plan = web_app.build_mixed_quiz_plan(conn, closed_count=0, open_count=999,
                                                   section_id=scope.section_id, scope=scope)
                self.assertEqual({q['question_id'] for q in plan['open_questions']}, self.expected_ready)
        finally:
            conn.close()

    def test_sources_and_readiness_are_from_canonical_pool(self):
        conn = web_app.quiz_db()
        try:
            for source in ('oq-answerless', 'oq-visual', 'oq-ordinary', 'oq-wrong-exam', 'oq-wrong-year'):
                self.assertEqual(fetch_dzi_open_questions(conn, source_slug=source), [], source)
            self.assertEqual({q['id'] for q in fetch_dzi_open_questions(conn, source_slug='oq-source-a')},
                             {self.opens['a1'], self.opens['a2']})
        finally:
            conn.close()
        sources = {s['source_slug']: s['count'] for s in self.summary('dzi')['sources']}
        self.assertEqual(sources['oq-source-a'], 2)
        self.assertEqual(sources['oq-source-b'], 1)
        self.assertNotIn('oq-answerless', sources)
        html = self.form('dzi')
        select = re.search(r'<select id="source_slug".*?</select>', html, re.S).group(0)
        self.assertIn('oq-source-a', select)
        self.assertNotIn('oq-answerless', select)
        self.assertIn(f'max="{len(self.expected_ready)}"', html)

    def test_generation_safeguards_require_every_answer_and_reject_practical(self):
        question = {'question_type': 'fill_in', 'source_number': 16, 'prompt': 'Текст'}
        self.assertFalse(quiz_service.is_fill_in_question_auto_gradable(question, [
            {'correct_answer': '["да"]'}, {'correct_answer': '[]'}]))
        self.assertFalse(quiz_service.is_fill_in_question_auto_gradable(
            {**question, 'source_number': 26}, [{'correct_answer': '["да"]'}]))

    def test_crafted_ordinary_posts_cannot_authorize_dzi(self):
        before = self.assignments()
        for key in [*(f'it{g}' for g in range(8, 13)), *OTHER_SUBJECTS]:
            for fields in ({'open_count': 1}, {'open_count': 1, 'include_open_questions': 1},
                           {'open_count': 1, 'source_slug': 'oq-source-a', 'mode': 'dzi',
                            'section_type': 'dzi_training', 'grade': 12}):
                with self.subTest(context=key, fields=fields):
                    self.assert_rejected(self.create(key, **fields), before)

    def test_checkbox_and_source_with_zero_count_keep_ordinary_mc_only(self):
        row = self.plan(self.create('it8', open_count=0, include_open_questions=1, source_slug='oq-source-a'))
        self.assertIsNone(row['question_plan_json'])

    def test_dzi_post_source_narrows_and_agrees_with_count(self):
        row = self.plan(self.create('dzi', open_count=2, source_slug='oq-source-a'))
        plan = json.loads(row['question_plan_json'])
        self.assertEqual(set(plan['open_question_ids']), {self.opens['a1'], self.opens['a2']})
        self.assertEqual(len(plan['question_ids']), 3)
        self.assertEqual(self.plan(self.create('dzi2', open_count=1, source_slug='oq-source-b'))['section_id'],
                         self.sections['dzi2'])

    def test_dzi_post_rejects_shortfall_and_unavailable_source(self):
        before = self.assignments()
        for fields in ({'open_count': 3, 'source_slug': 'oq-source-a'},
                       {'open_count': 2, 'source_slug': 'oq-source-b'},
                       {'open_count': 1, 'source_slug': 'oq-answerless'},
                       {'open_count': 1, 'source_slug': 'unknown'}):
            self.assert_rejected(self.create('dzi', **fields), before)
        html = self.client.post('/teacher/new', data={'subject': 'it', 'section_id': self.sections['dzi'],
                                                    'question_count': 3, 'open_count': 2,
                                                    'source_slug': 'oq-source-b'}).get_data(as_text=True)
        self.assertRegex(html, r'<input id="open_count"[^>]*max="1"')

    def test_subject_section_mismatch_and_manual_plan_rejected(self):
        before = self.assignments()
        for fields in ({'subject': 'physics', 'open_count': 1},
                       {'open_question_ids': str(self.opens['a1']), 'open_count': 1}):
            self.assert_rejected(self.create('dzi', **fields), before)

    def test_scope_validation_cannot_accept_dzi_opens_in_ordinary_section(self):
        conn = web_app.quiz_db()
        try:
            with self.assertRaises(ValueError):
                AssessmentScope(conn, self.sections['it8']).validate_plan([self.opens['a1']], [self.opens['a1']])
            scope = AssessmentScope(conn, self.sections['dzi'])
            scope.validate_plan([self.opens['a2']], [self.opens['a2']], open_source_slug='oq-source-a')
            for key in ('answerless', 'visual', 'ordinary', 'wrong-exam', 'wrong-year'):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    scope.validate_plan([self.opens[key]], [self.opens[key]])
            with self.assertRaises(ValueError):
                scope.validate_plan([self.opens['a2']], [self.opens['a2']], open_source_slug='oq-source-b')
        finally:
            conn.close()

    def test_training_generator_keeps_ready_open_and_mc_composition(self):
        conn = web_app.quiz_db()
        try:
            pools = get_pool(conn, (8, 9, 10, 11, 12))
            ids = {qid for pool in pools.values() for qid in pool}
            self.assertIn(self.opens['a1'], ids)
            self.assertIn(self.opens['b'], ids)
            for key in ('answerless', 'visual', 'ordinary', 'wrong-exam', 'wrong-year'):
                self.assertNotIn(self.opens[key], ids)
            for key in ('it8', 'it9', 'it10', 'it11', 'it12', 'dzi', 'dzi2'):
                self.assertTrue(set(self.closed[key]) <= ids)
            selected = choose_questions(pools, allocate_counts(28, {8: 3, 9: 7, 10: 10, 11: 40, 12: 40}), 'oq')
            chosen = [qid for pool in selected.values() for qid in pool]
            self.assertEqual(len(chosen), 28)
            self.assertEqual(len(set(chosen)), 28)
            open_ids = {r[0] for r in conn.execute("SELECT id FROM questions WHERE question_type='fill_in'")}
            self.assertTrue(ids & open_ids <= {q['id'] for q in fetch_dzi_open_questions(conn)})
        finally:
            conn.close()

    def test_dzi_empty_pool_retains_truthful_disabled_state(self):
        with patch.object(quiz_service, 'fetch_dzi_open_questions', return_value=[]):
            html = self.form('dzi')
        self.assertNotIn('id="open-question-panel" hidden', html)
        self.assertRegex(html, r'<input id="open_count"[^>]*max="0"[^>]*disabled')
        self.assertIn('Няма налични отворени въпроси', html)
        self.assertEqual(PracticalPanelProbe(html).panels, 1)

    def test_duplicate_valid_dzi_plan_keeps_snapshot(self):
        source = self.plan(self.create('dzi', open_count=1, source_slug='oq-source-a'))
        copy = self.plan(self.client.post(f"/teacher/assignment/{source['id']}/duplicate",
                                         data={'section_id': self.sections['it8']}))
        self.assertEqual(copy['section_id'], self.sections['dzi'])  # No target-context API.
        self.assertEqual(copy['question_plan_json'], source['question_plan_json'])

    def test_duplicate_rejects_invalid_ordinary_or_not_ready_open(self):
        for key, open_key in (('it8', 'a1'), ('dzi', 'answerless'), ('dzi', 'visual'), ('dzi', 'ordinary')):
            conn = web_app.quiz_db()
            plan = json.dumps({'mixed_open_enabled': True, 'question_ids': [self.opens[open_key]],
                               'open_question_ids': [self.opens[open_key]]})
            try:
                assignment = conn.execute('INSERT INTO quiz_assignments (section_id,title_bg,question_count,question_plan_json) VALUES (?, ?, 1, ?)',
                                          (self.sections[key], 'Invalid historical plan', plan)).lastrowid
                conn.commit()
            finally:
                conn.close()
            before = self.assignments()
            self.assert_rejected(self.client.post(f'/teacher/assignment/{assignment}/duplicate'), before)

    def test_duplicate_rechecks_current_readiness(self):
        source = self.plan(self.create('dzi', open_count=1, source_slug='oq-source-b'))
        conn = web_app.quiz_db()
        try:
            conn.execute('UPDATE questions SET level=NULL WHERE id=?', (self.opens['b'],))
            conn.commit()
            self.assert_rejected(self.client.post(f"/teacher/assignment/{source['id']}/duplicate"), self.assignments())
        finally:
            conn.execute("UPDATE questions SET level='DZI' WHERE id=?", (self.opens['b'],))
            conn.commit()
            conn.close()

    def test_legacy_exam_builder_uses_ready_pool_and_worksheet_excludes_dzi_opens(self):
        from src.build_exam import get_questions_pool
        from src.build_worksheet import fetch_questions
        pool = get_questions_pool(str(web_app.QUIZ_DB_PATH))
        self.assertEqual({q['id'] for q in pool['fill_in']}, self.expected_ready)
        conn = web_app.quiz_db()
        try:
            worksheet = fetch_questions(conn, topic_slugs=[], area_ids=[], class_num=8,
                                        types=['fill_in'], difficulty=None, count=999, seed=1)
            self.assertEqual(worksheet, [])
        finally:
            conn.close()

    def test_catalogue_query_still_displays_answerless_questions(self):
        conn = web_app.quiz_db()
        try:
            exam_id = conn.execute('SELECT exam_id FROM questions WHERE id=?', (self.opens['answerless'],)).fetchone()[0]
            qid = self.opens['answerless']
            task = conn.execute("INSERT INTO exam_tasks (exam_id,task_number,exam_part,task_kind,points) VALUES (?,16,1,'short_answer',3)", (exam_id,)).lastrowid
            conn.execute("INSERT INTO exam_task_questions (task_id,question_id,role) VALUES (?,?,'primary')", (task,qid))
            conn.commit()
        finally:
            conn.close()
        conn = web_app.quiz_db()
        try:
            tasks = dzi_exams.fetch_dzi_tasks(conn, exam_id)
        finally:
            conn.close()
        self.assertIn(qid, [q['question_id'] for q in tasks['part1']])
