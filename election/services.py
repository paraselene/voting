import hashlib
import hmac
import secrets
from datetime import timedelta

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Ballot, Candidate, Choice, Election, Identity, LoginAttempt

ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'


def digest(value):
    return hmac.new(settings.PASSWORD_INDEX_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def password_for(identity):
    return Fernet(settings.PASSWORD_ENCRYPTION_KEY.encode()).decrypt(identity.encrypted_password.encode()).decode()


def voter_numbers():
    return dict(Identity.objects.filter(is_admin=False).values_list('pk', 'number'))


@transaction.atomic
def create_identity(name='', is_admin=False, number=None):
    name = name.strip() if is_admin else ''
    if (is_admin and (not name or len(name) > 80)) or (not is_admin and (type(number) is not int or not 1 <= number <= 19)):
        raise ValidationError('姓名須為 1 至 80 字元。')
    while True:
        password = ''.join(secrets.choice(ALPHABET) for _ in range(5))
        index = digest(password)
        if not Identity.objects.filter(password_index=index).exists():
            break
    identity = Identity.objects.create(name=name, number=None if is_admin else number, is_admin=is_admin, password_index=index, encrypted_password='' if is_admin else Fernet(settings.PASSWORD_ENCRYPTION_KEY.encode()).encrypt(password.encode()).decode())
    return identity, password


@transaction.atomic
def authenticate(password, ip):
    now = timezone.now()
    key = digest('ip:' + ip)
    LoginAttempt.objects.filter(last_failure__lt=now - timedelta(hours=1)).delete()
    attempt = LoginAttempt.objects.filter(pk=key).first()
    if attempt and attempt.blocked_until > now:
        return None, '嘗試次數較多，請稍候最多 8 秒再試。'
    normalized = password.strip().upper()
    identity = Identity.objects.filter(password_index=digest(normalized)).first() if len(normalized) == 5 and all(c in ALPHABET for c in normalized) else None
    if identity:
        return identity, ''
    failures = (attempt.failures if attempt and now - attempt.last_failure < timedelta(minutes=5) else 0) + 1
    delay = min(8, 2 ** min(failures - 6, 3)) if failures >= 6 else 0
    LoginAttempt.objects.update_or_create(key=key, defaults={'failures': failures, 'last_failure': now, 'blocked_until': now + timedelta(seconds=delay)})
    return None, '密碼不正確，請重新輸入。'


@transaction.atomic
def submit_ballot(voter, selected, election_id, version, blank_confirmed=False):
    election = current_election(election_id, version)
    voter = Identity.objects.filter(pk=voter.pk).first()
    if voter is None:
        raise ValidationError('用戶資料無效。')
    if voter.is_admin:
        raise ValidationError('管理員不能投票。')
    if not voter.is_active:
        raise ValidationError('你的投票權限已停用，仍可查看已提交的選票。')
    if not election.is_open:
        raise ValidationError('投票已關閉，未更改選票。')
    try:
        ids = [int(value) for value in selected]
    except (ValueError, TypeError):
        raise ValidationError('候選人資料無效。') from None
    if len(ids) > election.max_votes or len(ids) != len(set(ids)) or election.candidates.filter(pk__in=ids).count() != len(ids):
        raise ValidationError(f'請選擇最多 {election.max_votes} 位有效候選人，不可重複。')
    if not ids and not blank_confirmed:
        raise ValidationError('提交空白票前，請勾選確認。')
    ballot, _ = Ballot.objects.get_or_create(election=election, voter=voter)
    ballot.choices.clear()
    Choice.objects.bulk_create([Choice(ballot=ballot, candidate_id=pk) for pk in ids])
    ballot.save()
    return ballot


def current_election(election_id, version):
    election = Election.objects.order_by('-pk').first()
    if election is None or str(election.pk) != str(election_id) or str(election.version) != str(version):
        raise ValidationError('輪次資料已過期，請重新載入頁面。')
    return election


@transaction.atomic
def fresh_setup():
    old_indexes = set(Identity.objects.filter(is_admin=False).values_list('password_index', flat=True))
    Ballot.objects.all().delete()
    Election.objects.all().delete()
    Identity.objects.filter(is_admin=False).delete()
    Candidate.objects.all().delete()
    LoginAttempt.objects.all().delete()
    admin_ids = set(Identity.objects.filter(is_admin=True).values_list('pk', flat=True))
    for session in Session.objects.iterator():
        if session.get_decoded().get('identity') not in admin_ids:
            session.delete()
    Candidate.objects.bulk_create([Candidate(name=name) for name in settings.CANDIDATE_NAMES])
    for number in range(1, 20):
        while True:
            user, _ = create_identity(number=number)
            if user.password_index not in old_indexes:
                break
            user.delete()


@transaction.atomic
def manage_election(action, value='', *, election_id=None, version=None, title='', selected=(), maximum=None):
    latest = Election.objects.order_by('-pk').first()
    if latest:
        election = current_election(election_id, version)
    elif str(election_id) != '0' or str(version) != '0':
        raise ValidationError('輪次資料已過期，請重新載入頁面。')
    else:
        election = None
    if action == 'create':
        if latest and latest.is_open:
            raise ValidationError('請先關閉目前輪次。')
        try:
            ids = [int(pk) for pk in selected]
            limit = int(maximum)
        except (ValueError, TypeError):
            raise ValidationError('輪次設定無效。') from None
        if title not in settings.ELECTION_TITLES or not 1 <= len(ids) <= 19 or len(set(ids)) != len(ids) or Candidate.objects.filter(pk__in=ids).count() != len(ids) or not 1 <= limit <= len(ids):
            raise ValidationError('請選擇 1 至 19 位候選人，上限須為 1 至候選人人數。')
        election = Election.objects.create(title=title, max_votes=limit, is_open=True)
        election.candidates.set(ids)
    elif action in ('enable_user', 'disable_user'):
        if not value.isdecimal() or len(value) > 12:
            raise ValidationError('啟用／停用對象無效。')
        target = Identity.objects.filter(pk=value, is_admin=False).first()
        if target is None:
            raise ValidationError('啟用／停用對象無效，管理員帳戶不能停用。')
        target.is_active = action == 'enable_user'
        target.save(update_fields=['is_active'])
    elif action == 'reset':
        if value != '清除全部資料':
            raise ValidationError('請確認「清除全部資料」操作。')
        fresh_setup()
    elif election and action in ('open', 'close', 'clear'):
        if action == 'clear':
            if value != '清除本輪選票':
                raise ValidationError('請確認「清除本輪選票」操作。')
            election.ballot_set.all().delete()
        election.is_open = action == 'open'
        election.version += 1
        election.save(update_fields=['is_open', 'version'])
    else:
        raise ValidationError('無效操作。')
    return election
