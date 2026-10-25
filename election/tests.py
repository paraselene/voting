import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, OperationalError, connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .models import Ballot, Candidate, Choice, Election, Identity, LoginAttempt
from .services import ALPHABET, authenticate, create_identity, digest, fresh_setup, manage_election, password_for, submit_ballot
from .views import client_ip


class RoundMixin:
    def setup_round(self):
        fresh_setup()
        self.voter = Identity.objects.get(number=1)
        self.password = password_for(self.voter)
        self.admin, self.admin_password = create_identity('管理員', True)
        self.candidates = list(Candidate.objects.all())
        self.election = self.manage('create', title=settings.ELECTION_TITLES[0], selected=[c.pk for c in self.candidates], maximum=10)

    def manage(self, action, value='', **kwargs):
        latest = Election.objects.order_by('-pk').first()
        return manage_election(action, value, election_id=latest.pk if latest else 0, version=latest.version if latest else 0, **kwargs)

    def submit(self, selected=(), blank=False, voter=None):
        latest = Election.objects.order_by('-pk').first()
        return submit_ballot(voter or self.voter, selected, latest.pk, latest.version, blank)

    def fields(self):
        latest = Election.objects.order_by('-pk').first()
        return {'election_id': latest.pk, 'version': latest.version}


class VotingTests(RoundMixin, TestCase):
    def setUp(self):
        self.setup_round()
        self.client.post('/', {'password': self.password})

    def test_fixed_roster_numbers_credentials_and_no_voter_names(self):
        self.assertEqual(list(Candidate.objects.values_list('name', flat=True)), settings.CANDIDATE_NAMES)
        self.assertEqual(list(Identity.objects.filter(is_admin=False).values_list('number', flat=True)), list(range(1, 20)))
        self.assertFalse(Identity.objects.filter(is_admin=False).exclude(name='').exists())
        self.assertEqual(self.admin.encrypted_password, '')
        for user in Identity.objects.filter(is_admin=False):
            password = password_for(user)
            self.assertEqual(len(password), 5)
            self.assertTrue(set(password) <= set(ALPHABET))
            self.assertNotIn(password, user.encrypted_password)
            self.assertEqual(authenticate(password.lower(), '127.0.0.1')[0], user)
        self.assertContains(self.client.get('/vote/'), '投票者編號 1')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Identity.objects.create(name='不能儲存姓名', number=19, password_index='invalid')

    def test_blank_limit_revote_and_invalid_ballot_preserves_previous(self):
        with self.assertRaises(ValidationError):
            self.submit()
        ballot = self.submit(blank=True)
        self.assertEqual(ballot.choices.count(), 0)
        ids = [c.pk for c in self.candidates[:10]]
        self.submit(ids)
        for invalid in ([c.pk for c in self.candidates[:11]], ['bad'], [999999], [ids[0], ids[0]]):
            with self.assertRaises(ValidationError):
                self.submit(invalid)
            self.assertEqual(set(ballot.choices.values_list('pk', flat=True)), set(ids))
        self.submit([ids[0]])
        self.assertEqual(Ballot.objects.count(), 1)
        self.assertEqual(ballot.choices.count(), 1)

    def test_rounds_repeated_titles_candidate_scope_and_credential_stability(self):
        first = self.election
        self.submit([self.candidates[0].pk])
        self.manage('close')
        second = self.manage('create', title=first.title, selected=[self.candidates[1].pk], maximum=1)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(password_for(self.voter), self.password)
        with self.assertRaises(ValidationError):
            self.submit([self.candidates[0].pk])
        self.submit([self.candidates[1].pk])
        self.assertEqual(Ballot.objects.filter(voter=self.voter).count(), 2)
        self.assertEqual(list(self.client.get('/vote/', {'round': first.pk}).context['candidates']), [self.candidates[1]])
        self.assertNotContains(self.client.get('/vote/'), self.candidates[0].name)
        with self.assertRaises(ValidationError):
            manage_election('open', election_id=first.pk, version=first.version)
        self.assertTrue(Election.objects.get(pk=second.pk).is_open)

    def test_lifecycle_versions_and_frozen_settings(self):
        self.submit(blank=True)
        old = self.fields()
        with self.assertRaises(ValidationError):
            self.manage('create', title=self.election.title, selected=[self.candidates[0].pk], maximum=1)
        self.manage('close')
        with self.assertRaises(ValidationError):
            manage_election('open', **old)
        with self.assertRaises(ValidationError):
            self.submit(blank=True)
        self.manage('open')
        with self.assertRaises(ValidationError):
            submit_ballot(self.voter, [], **old, blank_confirmed=True)
        self.assertEqual(Ballot.objects.count(), 1)
        self.election.title = '更改'
        with self.assertRaises(ValidationError):
            self.election.save()
        with self.assertRaises(ValidationError), transaction.atomic():
            self.election.candidates.clear()
        with self.assertRaises(ValidationError), transaction.atomic():
            self.election.candidates.remove(self.candidates[0])
        self.manage('close')
        self.manage('create', title=settings.ELECTION_TITLES[1], selected=[self.candidates[0].pk], maximum=1)
        with self.assertRaises(ValidationError):
            self.manage('candidate', '新增')
        with self.assertRaises(ValidationError):
            self.manage('disable_candidate', str(self.candidates[0].pk))

    def test_environment_configuration_applies_to_setup_and_future_rounds(self):
        saved_candidates = list(self.election.candidates.values_list('name', flat=True))
        with override_settings(ELECTION_TITLES=['自訂職位'], CANDIDATE_NAMES=[f'名單{i}' for i in range(19)]):
            self.manage('close')
            self.assertEqual(list(self.election.candidates.values_list('name', flat=True)), saved_candidates)
            new = self.manage('create', title='自訂職位', selected=[self.candidates[0].pk], maximum=1)
            self.assertEqual(new.title, '自訂職位')
            self.assertEqual(Candidate.objects.first().name, saved_candidates[0])
            self.manage('reset', '清除全部資料')
            self.assertEqual(list(Candidate.objects.values_list('name', flat=True)), [f'名單{i}' for i in range(19)])

    def test_creation_validation_and_admin_form(self):
        self.manage('close')
        for ids, maximum, title in [([],1,self.election.title),([999],1,self.election.title),([self.candidates[0].pk]*2,1,self.election.title),([self.candidates[0].pk],2,self.election.title),([self.candidates[0].pk],0,self.election.title),([self.candidates[0].pk],1,'unknown')]:
            with self.assertRaises(ValidationError):
                self.manage('create', title=title, selected=ids, maximum=maximum)
        client = Client(); client.post('/', {'password': self.admin_password})
        page = client.get('/manage/')
        self.assertContains(page, '全選')
        self.assertContains(page, '取消全選')
        self.assertEqual(page.content.decode().count('name="candidate"'), 19)
        self.assertEqual(page.content.decode().count(' checked'), 19)
        self.assertNotContains(page, '新增候選人')
        self.assertNotContains(page, '用戶姓名')
        fields = self.fields() | {'action':'create', 'title':settings.ELECTION_TITLES[1], 'maximum':1, 'candidate':[self.candidates[0].pk]}
        self.assertRedirects(client.post('/manage/', fields), '/manage/')
        self.assertEqual(Election.objects.count(), 2)

    def test_results_privacy_ties_and_historical_participation(self):
        self.submit([c.pk for c in self.candidates[:2]])
        self.submit(blank=True, voter=Identity.objects.get(number=2))
        admin = Client(); admin.post('/', {'password': self.admin_password})
        page = admin.get('/manage/')
        self.assertNotContains(page, '<th>票數</th>')
        self.assertNotContains(page, '空白票')
        self.assertFalse(any(hasattr(c,'votes') for c in page.context['results']))
        self.manage('close')
        page = admin.get('/manage/')
        self.assertEqual(page.context['voted'], 2)
        self.assertNotContains(page, '空白票')
        self.assertEqual([(c.votes,c.rank) for c in page.context['results'][:3]], [(1,1),(1,1),(0,3)])
        self.manage('create', title=self.election.title, selected=[c.pk for c in self.candidates], maximum=1)
        self.submit([self.candidates[2].pk])
        page = admin.get('/manage/', {'round':self.election.pk})
        self.assertNotContains(page, '空白票')
        self.assertEqual(page.context['voted'], 2)
        self.assertEqual([(c.votes,c.rank) for c in page.context['results'][:3]], [(1,1),(1,1),(0,3)])
        self.assertEqual(sum(u.ballot_count for u in page.context['users']), 2)
        self.assertNotContains(self.client.get('/vote/'), '票數')

    def test_clear_only_latest_and_reset_preserves_admin_invalidates_sessions(self):
        self.submit(blank=True)
        self.manage('close')
        self.manage('create', title=self.election.title, selected=[c.pk for c in self.candidates], maximum=1)
        self.submit(blank=True)
        old = self.fields()
        with self.assertRaises(ValidationError): self.manage('clear', 'wrong')
        self.manage('clear', '清除本輪選票')
        self.assertEqual(Ballot.objects.count(), 1)
        self.assertFalse(Election.objects.latest('pk').is_open)
        with self.assertRaises(ValidationError): manage_election('open', **old)
        admin = Client(); admin.post('/', {'password': self.admin_password})
        old_passwords = set(Identity.objects.filter(is_admin=False).values_list('password_index', flat=True))
        self.manage('reset', '清除全部資料')
        self.assertFalse(Election.objects.exists())
        self.assertEqual(Candidate.objects.count(),19)
        self.assertEqual(Identity.objects.filter(is_admin=False).count(),19)
        self.assertFalse(old_passwords & set(Identity.objects.filter(is_admin=False).values_list('password_index', flat=True)))
        self.assertRedirects(self.client.get('/vote/'), '/')
        self.assertEqual(admin.get('/manage/').status_code,200)
        self.assertContains(admin.get('/manage/'), '建立並開放')

    def test_full_reset_restarts_round_numbers_and_rejects_old_forms(self):
        old = self.fields()
        self.manage('close')
        second = self.manage('create', title=self.election.title, selected=[self.candidates[0].pk], maximum=1)
        self.assertEqual(second.number, 2)
        self.manage('clear', '清除本輪選票')
        self.assertEqual(second.number, 2)
        self.manage('reset', '清除全部資料')
        candidate = Candidate.objects.first()
        first = self.manage('create', title=self.election.title, selected=[candidate.pk], maximum=1)
        self.assertEqual(first.number, 1)
        self.assertNotEqual(first.pk, old['election_id'])
        with self.assertRaises(ValidationError):
            manage_election('close', **old)
        voter = Client()
        voter.post('/', {'password': password_for(Identity.objects.get(number=1))})
        self.assertContains(voter.get('/vote/'), '第 1 輪')
        admin = Client()
        admin.post('/', {'password': self.admin_password})
        self.assertContains(admin.get('/manage/'), '第 1 輪')
        self.manage('close')
        second = self.manage('create', title=self.election.title, selected=[candidate.pk], maximum=1)
        self.assertEqual(second.number, 2)

    def test_disabled_voter_qr_read_only_and_admin_permissions(self):
        ballot = self.submit([self.candidates[0].pk])
        self.manage('disable_user', str(self.voter.pk))
        self.assertEqual(authenticate(self.password,'127.0.0.1')[0],self.voter)
        self.client.get('/?qr=1')
        self.client.post('/?qr=1',{'password':self.password})
        page = self.client.get('/vote/')
        self.assertEqual(page.context['ballot'],ballot)
        self.assertContains(page,'<fieldset disabled>')
        self.assertNotContains(page,'id="submit-vote"')
        with self.assertRaises(ValidationError): self.submit(blank=True)
        self.assertEqual(ballot.choices.count(),1)
        with self.assertRaises(ValidationError): self.manage('disable_user',str(self.admin.pk))
        self.assertEqual(self.client.post('/manage/',self.fields()|{'action':'close'}).status_code,403)
        self.assertEqual(self.client.post('/credentials/',{'scope':'all'}).status_code,403)
        self.manage('enable_user', str(self.voter.pk))
        self.submit(blank=True)

    def test_csrf_on_lifecycle_and_credentials(self):
        client=Client(enforce_csrf_checks=True); client.get('/')
        client.post('/',{'password':self.admin_password,'csrfmiddlewaretoken':client.cookies['csrftoken'].value})
        for url, fields in [('/manage/',self.fields()|{'action':'close'}),('/credentials/',{'scope':'all'})]:
            self.assertEqual(client.post(url,fields).status_code,403)
        self.assertEqual(client.post('/manage/',self.fields()|{'action':'close','csrfmiddlewaretoken':client.cookies['csrftoken'].value}).status_code,302)
        self.assertFalse(Election.objects.get().is_open)

    def test_invalid_stale_post_and_rollback(self):
        fields=self.fields()|{'candidate':[self.candidates[0].pk]}
        self.assertRedirects(self.client.post('/vote/',fields),'/vote/')
        with patch('election.services.Choice.objects.bulk_create',side_effect=OperationalError('database is locked')):
            self.assertEqual(self.client.post('/vote/',self.fields()|{'blank':'yes'}).status_code,503)
        self.assertEqual(list(Ballot.objects.get().choices.all()),[self.candidates[0]])
        self.assertContains(self.client.post('/vote/',self.fields()|{'candidate':'bad'}),'候選人資料無效')
        self.assertContains(self.client.post('/vote/',{'blank':'yes'}),'輪次資料已過期')

    def test_admin_cannot_reveal_passwords_even_with_old_query_parameter(self):
        admin = Client()
        admin.post('/', {'password': self.admin_password})
        for query in ({}, {'reveal': self.voter.pk}):
            page = admin.get('/manage/', query)
            self.assertNotContains(page, self.password)
            self.assertNotContains(page, '顯示密碼')
            self.assertNotContains(page, '隱藏密碼')
            self.assertNotContains(page, 'reveal-password-form')
            self.assertNotContains(page, '<th>密碼</th>')
            self.assertFalse(any(hasattr(user, 'password_display') for user in page.context['users']))

    def test_credentials_number_only_pdf_pagination_selected_and_qr(self):
        from pypdf import PdfReader
        from reportlab.graphics.barcode.qr import QrCodeWidget
        from .pdf import credential_pdf
        admin=Client();admin.post('/',{'password':self.admin_password})
        response=admin.post('/credentials/',{'scope':'all'})
        self.assertIn('no-store',response['Cache-Control'])
        pages=PdfReader(BytesIO(response.content)).pages
        self.assertEqual([p.extract_text().count('登入憑證') for p in pages],[8,8,3])
        self.assertNotIn('姓名', ''.join(p.extract_text() for p in pages))
        self.assertNotIn('最多選 10', ''.join(p.extract_text() for p in pages))
        response=admin.post('/credentials/',{'users':[Identity.objects.get(number=19).pk,self.admin.pk]})
        text=PdfReader(BytesIO(response.content)).pages[0].extract_text()
        self.assertIn('投票者編號：19',text)
        self.assertNotIn(self.admin_password,text)
        with patch('election.pdf.QrCodeWidget',wraps=QrCodeWidget) as qr:
            credential_pdf([self.voter])
            self.assertEqual(qr.call_args.args[0],f'{settings.PUBLIC_URL}/#password={self.password}')
        self.assertEqual(admin.post('/credentials/',{'users':['bad']}).status_code,400)

    def test_collision_retries(self):
        with patch('election.services.secrets.choice', side_effect=list(self.password + '22222')):
            if self.password == '22222':
                self.skipTest('隨機憑證剛好等於碰撞測試值')
            Identity.objects.get(number=2).delete()
            _, password = create_identity(number=2)
        self.assertEqual(password, '22222')

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

    def test_qr_login_switches_accounts_and_rotates_session(self):
        client = Client(enforce_csrf_checks=True)
        client.get('/')
        token = client.cookies['csrftoken'].value
        client.post('/', {'password': self.admin_password, 'csrfmiddlewaretoken': token})
        self.assertEqual(client.session['identity'], self.admin.pk)
        old_key = client.session.session_key
        self.assertRedirects(client.get('/'), '/manage/')
        response = client.get('/?qr=1')
        self.assertContains(response, 'id="login-form"')
        self.assertEqual(client.session['identity'], self.admin.pk)
        self.assertEqual(client.post('/?qr=1', {'password': self.password}).status_code, 403)
        token = client.cookies['csrftoken'].value
        response = client.post('/?qr=1', {'password': self.password, 'csrfmiddlewaretoken': token})
        self.assertRedirects(response, '/vote/')
        self.assertEqual(client.session['identity'], self.voter.pk)
        self.assertNotEqual(client.session.session_key, old_key)
        self.assertNotEqual(client.cookies['csrftoken'].value, token)
        other = Identity.objects.get(number=2)
        password = password_for(other)
        client.get('/?qr=1')
        token = client.cookies['csrftoken'].value
        response = client.post('/?qr=1', {'password': '00000', 'csrfmiddlewaretoken': token})
        self.assertContains(response, '密碼不正確')
        self.assertEqual(client.session['identity'], self.voter.pk)
        response = client.post('/?qr=1', {'password': password, 'csrfmiddlewaretoken': token})
        self.assertRedirects(response, '/vote/')
        self.assertEqual(client.session['identity'], other.pk)

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


class ConcurrencyTests(RoundMixin, TransactionTestCase):
    def setUp(self):
        self.setup_round()

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
        fields = self.fields()
        results = self.race(*(lambda c=c: submit_ballot(self.voter, [c.pk], **fields) for c in self.candidates[:2]))
        self.assertEqual(results, ['ok', 'ok'])
        self.assertEqual(Ballot.objects.count(), 1)
        self.assertEqual(Ballot.objects.get().choices.count(), 1)

    def test_close_races_submit_and_rejects_old_lifecycle(self):
        fields=self.fields()
        results=self.race(lambda: manage_election('close',**fields), lambda: submit_ballot(self.voter,[],**fields,blank_confirmed=True))
        self.assertEqual(results[0],'ok')
        self.assertFalse(Election.objects.get().is_open)
        self.assertEqual(Ballot.objects.count(),int(results[1]=='ok'))
        with self.assertRaises(ValidationError): manage_election('open',**fields)

    def test_clear_races_submit(self):
        fields=self.fields()
        self.race(lambda: manage_election('clear','清除本輪選票',**fields),lambda: submit_ballot(self.voter,[],**fields,blank_confirmed=True))
        self.assertEqual(Ballot.objects.count(),0)
        self.assertFalse(Election.objects.get().is_open)

    def test_simultaneous_create_allows_only_one_new_round(self):
        self.manage('close')
        fields=self.fields()
        def create():
            manage_election('create',**fields,title=self.election.title,selected=[self.candidates[0].pk],maximum=1)
        self.assertEqual(sorted(self.race(create,create)),['ok','rejected'])
        self.assertEqual(Election.objects.filter(is_open=True).count(),1)
        self.assertEqual(Election.objects.count(),2)

    def test_disable_races_submit(self):
        fields=self.fields()
        result=self.race(lambda: manage_election('disable_user',str(self.voter.pk),**fields),lambda: submit_ballot(self.voter,[],**fields,blank_confirmed=True))
        self.assertEqual(result[0],'ok')
        self.assertEqual(Ballot.objects.count(),int(result[1]=='ok'))
        with self.assertRaises(ValidationError): self.submit(blank=True)

    def test_database_enforces_one_open_round(self):
        with self.assertRaises(IntegrityError),transaction.atomic():
            Election.objects.create(title='不能同時開放',max_votes=1,is_open=True)

    def test_backup_restore_rounds_and_session_revocation(self):
        self.submit([self.candidates[0].pk])
        self.manage('close')
        second=self.manage('create',title=self.election.title,selected=[self.candidates[1].pk],maximum=1)
        self.submit(blank=True)
        client=Client();client.post('/',{'password':self.password})
        output=StringIO(); call_command('backup_db',stdout=output)
        backup=Path(output.getvalue().strip())
        try:
            self.manage('clear','清除本輪選票')
            call_command('restore_db',str(backup),confirm='還原資料庫',stdout=StringIO())
            self.assertEqual(Ballot.objects.count(),2)
            self.assertFalse(Election.objects.filter(is_open=True).exists())
            self.assertEqual(Election.objects.get(pk=second.pk).version,2)
            self.assertFalse(Session.objects.exists())
            self.assertEqual(backup.stat().st_mode & 0o777,0o600)
        finally: backup.unlink(missing_ok=True)

    def test_setup_backs_up_and_rebuilds_preserving_admin(self):
        self.submit(blank=True)
        output=StringIO()
        call_command('setup_election',confirm='清除全部資料',stdout=output)
        backup=Path(output.getvalue().splitlines()[0])
        try:
            with sqlite3.connect(backup) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM election_ballot').fetchone()[0],1)
            self.assertTrue(Identity.objects.filter(pk=self.admin.pk,is_admin=True).exists())
            self.assertEqual(Identity.objects.filter(is_admin=False).count(),19)
            self.assertFalse(Election.objects.exists())
        finally: backup.unlink(missing_ok=True)
        with self.assertRaises(CommandError): call_command('setup_election',confirm='wrong')

    def test_old_schema_backup_is_rejected_without_overwriting_database(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            backup=Path(folder)/'old.sqlite3'
            with sqlite3.connect(backup) as db:
                for table in ('election_identity','election_election','election_ballot'):
                    db.execute(f'CREATE TABLE {table} (id INTEGER)')
            with self.assertRaisesMessage(CommandError,'不相容'):
                call_command('restore_db',str(backup),confirm='還原資料庫')
        self.assertEqual(Identity.objects.filter(is_admin=False).count(),19)

    def test_voter_disabling_and_submission_follow_transaction_order(self):
        disable = lambda: self.manage('disable_user', str(self.voter.pk))
        submit = lambda: self.submit([self.candidates[1].pk])
        for disable_first in (True, False):
            with self.subTest(disable_first=disable_first):
                self.manage('enable_user', str(self.voter.pk))
                ballot = self.submit([self.candidates[0].pk])
                held, attempted, release = threading.Event(), threading.Event(), threading.Event()
                first, second = (disable, submit) if disable_first else (submit, disable)
                def hold_first():
                    connections.close_all()
                    try:
                        with transaction.atomic():
                            first()
                            held.set()
                            if not release.wait(10):
                                raise AssertionError('Timed out waiting to release transaction')
                    finally:
                        connections.close_all()
                def run_second():
                    connection = connections['default']
                    def observe_begin(execute, sql, params, many, context):
                        if sql == 'BEGIN IMMEDIATE':
                            attempted.set()
                        return execute(sql, params, many, context)
                    try:
                        with connection.execute_wrapper(observe_begin):
                            try:
                                second()
                                return 'ok'
                            except ValidationError:
                                return 'rejected'
                    finally:
                        connection.close()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first_future = pool.submit(hold_first)
                    try:
                        self.assertTrue(held.wait(10))
                        second_future = pool.submit(run_second)
                        self.assertTrue(attempted.wait(10))
                    finally:
                        release.set()
                    first_future.result(timeout=10)
                    self.assertEqual(second_future.result(timeout=10), 'rejected' if disable_first else 'ok')
                self.voter.refresh_from_db()
                self.assertFalse(self.voter.is_active)
                self.assertEqual(list(ballot.choices.all()), [self.candidates[0 if disable_first else 1]])

    def test_real_sqlite_lock_timeout_preserves_ballot(self):
        self.submit([self.candidates[0].pk])
        def blocked_submit():
            connection = connections['default']
            try:
                with connection.cursor() as cursor:
                    cursor.execute('PRAGMA busy_timeout = 50')
                with self.assertRaises(OperationalError):
                    self.submit([self.candidates[1].pk])
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
