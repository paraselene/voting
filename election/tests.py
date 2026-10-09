import threading
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.db import OperationalError, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .models import Ballot, Candidate, Choice, Election, Identity, LoginAttempt
from .services import ALPHABET, authenticate, create_identity, digest, manage_election, password_for, submit_ballot
from .views import client_ip


class VotingTests(TestCase):
    def setUp(self):
        Election.objects.get_or_create(pk=1)
        self.voter, self.password = create_identity('測試用戶')
        self.admin, self.admin_password = create_identity('管理員', True)
        self.candidates = [Candidate.objects.create(name=f'候選人{i}') for i in range(11)]
        manage_election('open')
        session = self.client.session
        session['identity'] = self.voter.pk
        session.save()

    def test_password_format_encryption_roles_and_uniqueness(self):
        seen = {self.password, self.admin_password}
        for _ in range(30):
            user, password = create_identity('同名用戶')
            self.assertEqual(len(password), 5)
            self.assertTrue(set(password) <= set(ALPHABET))
            self.assertNotIn(password, seen)
            seen.add(password)
            self.assertEqual(password_for(user), password)
            self.assertNotIn(password, user.encrypted_password)
        self.assertEqual(self.admin.encrypted_password, '')
        user, error = authenticate(self.password.lower(), '127.0.0.1')
        self.assertEqual(user, self.voter)
        self.assertFalse(error)

    def test_collision_retries(self):
        with patch('election.services.secrets.choice', side_effect=list(self.password + '22222')):
            if self.password == '22222':
                self.skipTest('隨機憑證剛好等於碰撞測試值')
            _, password = create_identity('碰撞測試')
        self.assertEqual(password, '22222')

    def test_blank_ten_replace_and_validation_preserves_old_ballot(self):
        with self.assertRaises(ValidationError):
            submit_ballot(self.voter, [], 1)
        ballot = submit_ballot(self.voter, [], 1, True)
        self.assertEqual(Ballot.objects.count(), 1)
        self.assertEqual(ballot.choices.count(), 0)
        ids = [c.pk for c in self.candidates[:10]]
        submit_ballot(self.voter, ids, 1)
        for invalid in ([c.pk for c in self.candidates], [99999], ['bad'], [ids[0], ids[0]]):
            with self.assertRaises(ValidationError):
                submit_ballot(self.voter, invalid, 1)
            self.assertEqual(set(ballot.choices.values_list('pk', flat=True)), set(ids))
        submit_ballot(self.voter, [self.candidates[-1].pk], 1)
        self.assertEqual(Ballot.objects.count(), 1)
        self.assertEqual(list(ballot.choices.all()), [self.candidates[-1]])

    def test_close_reopen_clear_stale_and_candidate_lock(self):
        submit_ballot(self.voter, [], 1, True)
        with self.assertRaises(ValidationError):
            manage_election('candidate', '不能新增')
        manage_election('close')
        with self.assertRaises(ValidationError):
            submit_ballot(self.voter, [], 1, True)
        self.assertContains(self.client.get('/vote/'), '空白票')
        manage_election('open')
        submit_ballot(self.voter, [self.candidates[0].pk], 1)
        with self.assertRaises(ValidationError):
            manage_election('clear', 'wrong')
        manage_election('clear', '清除全部選票')
        self.assertFalse(Ballot.objects.exists())
        self.assertEqual(Identity.objects.count(), 2)
        self.assertEqual(password_for(self.voter), self.password)
        manage_election('candidate', '新候選人')
        manage_election('open')
        with self.assertRaises(ValidationError):
            submit_ballot(self.voter, [self.candidates[0].pk], 1)

    def test_no_candidates_cannot_open(self):
        Candidate.objects.all().delete()
        manage_election('close')
        with self.assertRaises(ValidationError):
            manage_election('open')

    def test_batch_create_users_and_candidates(self):
        client = Client()
        client.post('/', {'password': self.admin_password})
        names = ['陳大文', '李小明', '王小華']
        for action, model in (('user', Identity), ('candidate', Candidate)):
            with self.subTest(action=action):
                response = client.post('/manage/', {'action': action, 'name': ' 陳大文,李小明, 王小華 '})
                self.assertRedirects(response, '/manage/')
                self.assertEqual(list(model.objects.filter(name__in=names).values_list('name', flat=True)), names)
        users = list(Identity.objects.filter(name__in=names))
        self.assertEqual(len({password_for(user) for user in users}), 3)
        for user in users:
            self.assertFalse(user.is_admin)
            self.assertEqual(authenticate(password_for(user), '192.0.2.1')[0], user)
        submit_ballot(self.voter, [], 1, True)
        response = client.post('/manage/', {'action': 'candidate', 'name': '鎖定一,鎖定二'}, follow=True)
        self.assertContains(response, '已有選票，候選人名單已鎖定。')
        self.assertFalse(Candidate.objects.filter(name__startswith='鎖定').exists())

    def test_invalid_batch_creates_nothing(self):
        client = Client()
        client.post('/', {'password': self.admin_password})
        for action, model in (('user', Identity), ('candidate', Candidate)):
            for value in ('', '有效姓名,', ',有效姓名', '有效姓名, ,另一位', '有效姓名,' + '長' * 81):
                with self.subTest(action=action, value=value):
                    count = model.objects.count()
                    response = client.post('/manage/', {'action': action, 'name': value}, follow=True)
                    self.assertContains(response, '每個姓名須為 1 至 80 字元')
                    self.assertEqual(model.objects.count(), count)

    def test_batch_users_roll_back_if_creation_fails(self):
        count = Identity.objects.count()
        with patch('election.services.create_identity', wraps=create_identity) as create:
            def create_or_fail(name):
                if name == '失敗用戶':
                    raise ValidationError('建立失敗')
                return create_identity(name)
            create.side_effect = create_or_fail
            with self.assertRaises(ValidationError):
                manage_election('user', '暫存用戶,失敗用戶')
        self.assertEqual(Identity.objects.count(), count)

    def test_reset_requires_admin_confirmation_and_preserves_admin_logins(self):
        other_admin, other_password = create_identity('另一位管理員', True)
        submit_ballot(self.voter, [self.candidates[0].pk], 1)
        fields = {'action': 'reset', 'confirmation': '清除全部資料'}
        self.assertEqual(self.client.post('/manage/', fields).status_code, 403)
        admin_client = Client()
        admin_client.post('/', {'password': self.admin_password})
        session_key = admin_client.session.session_key
        response = admin_client.get('/manage/')
        self.assertContains(response, '清除全部資料並保留管理員')
        self.assertContains(response, 'data-confirm=')
        self.assertContains(response, 'type="hidden" name="confirmation" value="清除全部資料"')
        self.assertNotContains(response, '請輸入「清除全部資料」')
        self.assertEqual(admin_client.get('/manage/', fields).status_code, 200)
        admin_client.post('/manage/', {'action': 'reset', 'confirmation': 'wrong'})
        self.assertTrue(Ballot.objects.exists())
        self.assertTrue(Identity.objects.filter(pk=self.voter.pk).exists())
        self.assertEqual(Candidate.objects.count(), 11)
        self.assertEqual(Election.objects.get(pk=1).version, 1)
        response = admin_client.post('/manage/', fields)
        self.assertRedirects(response, '/manage/')
        self.assertEqual(admin_client.session.session_key, session_key)
        self.assertEqual(set(Identity.objects.values_list('pk', flat=True)), {self.admin.pk, other_admin.pk})
        self.assertFalse(Ballot.objects.exists())
        self.assertFalse(Choice.objects.exists())
        self.assertFalse(Candidate.objects.exists())
        election = Election.objects.get(pk=1)
        self.assertFalse(election.is_open)
        self.assertEqual(election.version, 2)
        self.assertRedirects(self.client.get('/vote/'), '/')
        self.assertIsNone(authenticate(self.password, '192.0.2.1')[0])
        for admin, password in ((self.admin, self.admin_password), (other_admin, other_password)):
            self.assertEqual(authenticate(password, '192.0.2.2')[0], admin)

    def test_reset_rolls_back_on_deletion_failure(self):
        submit_ballot(self.voter, [self.candidates[0].pk], 1)
        with patch('election.services.Candidate.objects.all') as candidates:
            candidates.return_value.delete.side_effect = OperationalError('database is locked')
            with self.assertRaises(OperationalError):
                manage_election('reset', '清除全部資料')
        self.assertTrue(Identity.objects.filter(pk=self.voter.pk).exists())
        self.assertEqual(Candidate.objects.count(), 11)
        self.assertEqual(Ballot.objects.get(voter=self.voter).choices.count(), 1)
        election = Election.objects.get(pk=1)
        self.assertTrue(election.is_open)
        self.assertEqual(election.version, 1)

    def test_reset_restarts_ids_and_revokes_sessions_before_id_reuse(self):
        old_voter, password = create_identity('將刪除的用戶')
        old_client = Client()
        old_client.post('/', {'password': password})
        old_session_key = old_client.session.session_key
        # Advance every sequence beyond the rows that will remain after reset.
        create_identity('另一位用戶')
        submit_ballot(self.voter, [self.candidates[0].pk], 1)
        submit_ballot(old_voter, [self.candidates[1].pk], 1)
        authenticate('00000', '192.0.2.1')
        self.assertTrue(LoginAttempt.objects.exists())
        manage_election('reset', '清除全部資料')
        self.assertFalse(LoginAttempt.objects.exists())
        self.assertFalse(Session.objects.filter(session_key=old_session_key).exists())
        voter, _ = create_identity('新用戶')
        self.assertEqual(voter.pk, self.admin.pk + 1)
        self.assertEqual(voter.pk, old_voter.pk)
        self.assertRedirects(old_client.get('/vote/'), '/')
        candidate = Candidate.objects.create(name='新候選人')
        self.assertEqual(candidate.pk, 1)
        manage_election('open')
        with self.assertRaises(ValidationError):
            submit_ballot(voter, [candidate.pk], 1)
        ballot = submit_ballot(voter, [candidate.pk], 2)
        self.assertEqual(ballot.pk, 1)
        self.assertEqual(Choice.objects.get(ballot=ballot).pk, 1)

    def test_reset_rolls_back_if_sequence_reset_fails(self):
        submit_ballot(self.voter, [self.candidates[0].pk], 1)
        session_key = self.client.session.session_key
        authenticate('00000', '192.0.2.1')
        with patch('election.services.connection.ops.sequence_reset_by_name_sql', side_effect=OperationalError('reset failed')):
            with self.assertRaises(OperationalError):
                manage_election('reset', '清除全部資料')
        self.assertTrue(Session.objects.filter(session_key=session_key).exists())
        self.assertTrue(LoginAttempt.objects.exists())
        self.assertTrue(Identity.objects.filter(pk=self.voter.pk).exists())
        self.assertEqual(Candidate.objects.count(), 11)
        self.assertEqual(Ballot.objects.get(voter=self.voter).choices.count(), 1)
        self.assertEqual(Election.objects.get(pk=1).version, 1)

    def test_roles_login_logout_and_cache(self):
        anonymous = Client()
        for path in ('/vote/', '/manage/', '/credentials/'):
            self.assertEqual(anonymous.get(path).status_code, 302)
        self.assertEqual(self.client.get('/manage/').status_code, 403)
        self.assertEqual(self.client.post('/credentials/').status_code, 403)
        response = anonymous.post('/', {'password': self.admin_password.lower()})
        self.assertRedirects(response, '/manage/')
        self.assertEqual(anonymous.get('/vote/').status_code, 403)
        response = anonymous.get('/manage/')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertContains(response, '用戶與登入憑證')
        self.assertEqual(anonymous.get('/logout/').status_code, 405)
        anonymous.post('/logout/')
        self.assertEqual(anonymous.get('/manage/').status_code, 302)

    def test_dashboard_reveals_only_one_password_on_request(self):
        other, other_password = create_identity('另一位用戶')
        client = Client()
        client.post('/', {'password': self.admin_password})
        with patch('election.views.password_for', wraps=password_for) as decrypt:
            response = client.get('/manage/')
            decrypt.assert_not_called()
            self.assertFalse(any(hasattr(user, 'password_display') for user in response.context['users']))
            self.assertNotContains(response, f'<code>{self.password}</code>')
            self.assertNotContains(response, f'<code>{other_password}</code>')
            self.assertContains(response, '顯示密碼')
            response = client.get('/manage/', {'reveal': self.voter.pk})
            self.assertEqual(decrypt.call_count, 1)
            self.assertContains(response, f'<code>{self.password}</code>')
            self.assertNotContains(response, f'<code>{other_password}</code>')
            self.assertContains(response, '隱藏密碼')
            response = client.get('/manage/', {'reveal': other.pk})
            self.assertContains(response, f'<code>{other_password}</code>')
            self.assertNotContains(response, f'<code>{self.password}</code>')
            for reveal in ('', 'bad', str(self.admin.pk), '999999999999999999999999'):
                response = client.get('/manage/', {'reveal': reveal})
                self.assertFalse(any(hasattr(user, 'password_display') for user in response.context['users']))
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.client.get('/manage/', {'reveal': other.pk}).status_code, 403)
        response = Client().get('/manage/', {'reveal': self.voter.pk})
        self.assertEqual(response.status_code, 302)

    def test_csrf_and_session_rotation(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post('/', {'password': self.password})
        self.assertEqual(response.status_code, 403)
        client.get('/')
        token = client.cookies['csrftoken'].value
        session = client.session
        session['placeholder'] = True
        session.save()
        old_key = session.session_key
        self.assertEqual(client.post('/', {'password': self.password, 'csrfmiddlewaretoken': token}).status_code, 302)
        self.assertNotEqual(client.session.session_key, old_key)
        self.assertNotEqual(client.cookies['csrftoken'].value, token)
        self.assertEqual(client.post('/vote/', {'version': 1, 'blank': 'yes'}).status_code, 403)

    @override_settings(
        SECURE_SSL_REDIRECT=True,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        ALLOWED_HOSTS=['vote.hcmc.nz'],
        CSRF_TRUSTED_ORIGINS=['https://vote.hcmc.nz'],
    )
    def test_https_login_referrer_policy_and_csrf(self):
        client = Client(enforce_csrf_checks=True)
        host = {'HTTP_HOST': 'vote.hcmc.nz'}
        response = client.get('/', secure=True, **host)
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        token = client.cookies['csrftoken'].value
        fields = {'password': self.admin_password, 'csrfmiddlewaretoken': token}
        # Browsers must still provide a trusted origin or HTTPS referer.
        self.assertEqual(client.post('/', fields, secure=True, **host).status_code, 403)
        self.assertEqual(client.post('/', fields, secure=True, HTTP_ORIGIN='null', **host).status_code, 403)
        self.assertEqual(client.post('/', fields, secure=True, HTTP_ORIGIN='https://untrusted.example', **host).status_code, 403)
        response = client.post('/', fields, secure=True, HTTP_REFERER='https://vote.hcmc.nz/', **host)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/manage/')
        self.assertTrue(client.cookies['sessionid']['secure'])

    def test_throttle_shared_in_database_and_expiry(self):
        for _ in range(6):
            self.assertIsNone(authenticate('00000', '192.0.2.1')[0])
        self.assertIsNone(authenticate(self.password, '192.0.2.1')[0])
        self.assertEqual(authenticate(self.password, '192.0.2.2')[0], self.voter)
        LoginAttempt.objects.update(blocked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(authenticate(self.password, '192.0.2.1')[0], self.voter)
        LoginAttempt.objects.update(last_failure=timezone.now() - timedelta(minutes=6))
        authenticate('00000', '192.0.2.1')
        self.assertEqual(LoginAttempt.objects.get(pk=digest('ip:192.0.2.1')).failures, 1)

    @override_settings(TRUSTED_PROXY_IPS=['172.20.0.2'])
    def test_proxy_spoof_protection(self):
        from django.test import RequestFactory
        factory = RequestFactory()
        self.assertEqual(client_ip(factory.get('/', REMOTE_ADDR='192.0.2.1', HTTP_X_REAL_IP='192.0.2.99')), '192.0.2.1')
        self.assertEqual(client_ip(factory.get('/', REMOTE_ADDR='172.20.0.2', HTTP_X_REAL_IP='192.0.2.99')), '192.0.2.99')
        self.assertEqual(client_ip(factory.get('/', REMOTE_ADDR='172.20.0.2', HTTP_X_REAL_IP='bad')), '172.20.0.2')

    def test_results_ties_blank_and_privacy(self):
        other, _ = create_identity('另一位')
        submit_ballot(self.voter, [c.pk for c in self.candidates[:2]], 1)
        submit_ballot(other, [], 1, True)
        session = self.client.session
        session['identity'] = self.admin.pk
        session.save()
        for action in ('open', 'close', 'open'):
            manage_election(action)
            response = self.client.get('/manage/')
            self.assertFalse(any(hasattr(user, 'selected') for user in response.context['users']))
            if action == 'open':
                self.assertEqual((response.context['total'], response.context['voted'], response.context['unvoted']), (2, 2, 0))
                self.assertIsNone(response.context['blank'])
                self.assertFalse(any(hasattr(candidate, 'votes') or hasattr(candidate, 'rank') for candidate in response.context['results']))
                self.assertEqual([candidate.pk for candidate in response.context['results']], [candidate.pk for candidate in self.candidates])
                self.assertNotContains(response, '<th>票數</th>')
                self.assertNotContains(response, '<th>名次</th>')
                self.assertNotContains(response, '空白票')
                self.assertContains(response, '投票結束後才顯示票數及名次。')
            else:
                self.assertContains(response, '<th>票數</th>')
                self.assertContains(response, '<th>名次</th>')
                self.assertContains(response, '空白票')
                self.assertEqual([(c.votes, c.rank) for c in response.context['results'][:3]], [(1, 1), (1, 1), (0, 3)])
        manage_election('close')
        response = self.client.get('/manage/')
        self.assertEqual((response.context['total'], response.context['voted'], response.context['unvoted'], response.context['blank']), (2, 2, 0, 1))
        self.assertEqual([(c.votes, c.rank) for c in response.context['results'][:3]], [(1, 1), (1, 1), (0, 3)])
        self.assertFalse(any(hasattr(user, 'selected') for user in response.context['users']))

    def test_post_validation_and_atomic_rollback(self):
        selected = [str(self.candidates[0].pk)]
        self.assertRedirects(self.client.post('/vote/', {'candidate': selected, 'version': 1}), '/vote/')
        with patch('election.services.Choice.objects.bulk_create', side_effect=OperationalError('database is locked')):
            response = self.client.post('/vote/', {'candidate': [self.candidates[1].pk], 'version': 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(list(Ballot.objects.get(voter=self.voter).choices.values_list('pk', flat=True)), [int(selected[0])])
        self.assertContains(self.client.post('/vote/', {'candidate': 'bad', 'version': 1}), '候選人資料無效')

    def test_pdf_all_selected_and_long_names(self):
        from pypdf import PdfReader
        for _ in range(8):
            create_identity('陳王李張劉林黃吳周徐' * 8)
        session = self.client.session
        session['identity'] = self.admin.pk
        session.save()
        response = self.client.post('/credentials/', {'scope': 'all'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        reader = PdfReader(BytesIO(response.content))
        self.assertEqual(len(reader.pages), 2)
        self.assertEqual(reader.pages[0].extract_text().count('登入憑證'), 8)
        self.assertEqual(reader.pages[1].extract_text().count('登入憑證'), 1)
        self.assertIn(self.voter.name, reader.pages[0].extract_text())
        self.assertIn(self.password, reader.pages[0].extract_text())
        response = self.client.post('/credentials/', {'scope': 'selected', 'users': [self.voter.pk, self.admin.pk]})
        self.assertEqual(len(PdfReader(BytesIO(response.content)).pages), 1)
        self.assertNotIn(self.admin_password, PdfReader(BytesIO(response.content)).pages[0].extract_text())
        self.assertEqual(self.client.post('/credentials/', {'scope': 'selected'}).status_code, 302)


class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        Election.objects.update_or_create(pk=1, defaults={'is_open': True, 'version': 1})
        self.voter, _ = create_identity('並行測試')
        self.candidates = [Candidate.objects.create(name=f'並行候選人{i}') for i in range(2)]

    def race(self, *operations):
        barrier = threading.Barrier(len(operations))
        def run(operation):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                try:
                    operation()
                    return 'ok'
                except ValidationError:
                    return 'rejected'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            return list(pool.map(run, operations))

    def test_simultaneous_revotes_leave_one_complete_ballot(self):
        results = self.race(*(lambda c=c: submit_ballot(self.voter, [c.pk], 1) for c in self.candidates))
        self.assertEqual(results, ['ok', 'ok'])
        self.assertEqual(Ballot.objects.count(), 1)
        self.assertEqual(Ballot.objects.get().choices.count(), 1)

    def test_close_races_submit(self):
        results = self.race(lambda: manage_election('close'), lambda: submit_ballot(self.voter, [self.candidates[0].pk], 1))
        self.assertEqual(results[0], 'ok')
        self.assertFalse(Election.objects.get().is_open)
        self.assertEqual(Ballot.objects.count(), int(results[1] == 'ok'))

    def test_clear_races_submit_then_rejects_stale(self):
        self.race(lambda: manage_election('clear', '清除全部選票'), lambda: submit_ballot(self.voter, [self.candidates[0].pk], 1))
        self.assertEqual(Ballot.objects.count(), 0)
        manage_election('open')
        with self.assertRaises(ValidationError):
            submit_ballot(self.voter, [], 1, True)

    def test_candidate_races_first_ballot(self):
        results = self.race(lambda: manage_election('candidate', '新增候選人'), lambda: submit_ballot(self.voter, [], 1, True))
        self.assertEqual(results[1], 'ok')
        self.assertEqual(Candidate.objects.count(), 3 if results[0] == 'ok' else 2)
        with self.assertRaises(ValidationError):
            manage_election('candidate', '投票後不得新增')

    def test_real_sqlite_lock_timeout_preserves_ballot(self):
        submit_ballot(self.voter, [self.candidates[0].pk], 1)
        def blocked_submit():
            connection = connections['default']
            try:
                with connection.cursor() as cursor:
                    cursor.execute('PRAGMA busy_timeout = 50')
                with self.assertRaises(OperationalError):
                    submit_ballot(self.voter, [self.candidates[1].pk], 1)
            finally:
                connection.close()
        lock = sqlite3.connect(connections['default'].settings_dict['NAME'])
        try:
            lock.execute('BEGIN IMMEDIATE')
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(blocked_submit).result(timeout=10)
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual(list(Ballot.objects.get().choices.all()), [self.candidates[0]])

    def test_backup_and_restore(self):
        submit_ballot(self.voter, [self.candidates[0].pk], 1)
        output = StringIO()
        call_command('backup_db', stdout=output)
        backup = Path(output.getvalue().strip())
        try:
            manage_election('clear', '清除全部選票')
            call_command('restore_db', str(backup), confirm='還原資料庫', stdout=StringIO())
            self.assertEqual(Ballot.objects.count(), 1)
            self.assertFalse(Election.objects.get().is_open)
            self.assertEqual(Election.objects.get().version, 2)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        finally:
            backup.unlink(missing_ok=True)
