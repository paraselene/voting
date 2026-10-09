import hashlib
import hmac
import secrets
from datetime import timedelta

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.management.color import no_style
from django.db import connection, transaction
from django.utils import timezone

from .models import Ballot, Candidate, Choice, Election, Identity, LoginAttempt

ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'


def digest(value):
    return hmac.new(settings.PASSWORD_INDEX_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def password_for(identity):
    return Fernet(settings.PASSWORD_ENCRYPTION_KEY.encode()).decrypt(identity.encrypted_password.encode()).decode()


def voter_numbers():
    return {pk: number for number, pk in enumerate(Identity.objects.filter(is_admin=False).values_list('pk', flat=True), 1)}


@transaction.atomic
def create_identity(name, is_admin=False):
    name = name.strip()
    if not name or len(name) > 80:
        raise ValidationError('姓名須為 1 至 80 字元。')
    while True:
        password = ''.join(secrets.choice(ALPHABET) for _ in range(5))
        index = digest(password)
        if not Identity.objects.filter(password_index=index).exists():
            break
    identity = Identity.objects.create(name=name, is_admin=is_admin, password_index=index, encrypted_password='' if is_admin else Fernet(settings.PASSWORD_ENCRYPTION_KEY.encode()).encrypt(password.encode()).decode())
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
def submit_ballot(voter, selected, version, blank_confirmed=False):
    election = Election.objects.get(pk=1)
    if voter.is_admin:
        raise ValidationError('管理員不能投票。')
    if str(election.version) != str(version):
        raise ValidationError('投票資料已重設，請重新載入頁面。')
    if not election.is_open:
        raise ValidationError('投票已關閉，未更改選票。')
    try:
        ids = [int(value) for value in selected]
    except (ValueError, TypeError):
        raise ValidationError('候選人資料無效。') from None
    if len(ids) > 10 or len(ids) != len(set(ids)) or Candidate.objects.filter(pk__in=ids).count() != len(ids):
        raise ValidationError('請選擇最多 10 位有效候選人，不可重複。')
    if not ids and not blank_confirmed:
        raise ValidationError('提交空白票前，請勾選確認。')
    ballot, _ = Ballot.objects.get_or_create(voter=voter)
    ballot.choices.clear()
    Choice.objects.bulk_create([Choice(ballot=ballot, candidate_id=pk) for pk in ids])
    ballot.save()
    return ballot


@transaction.atomic
def manage_election(action, value=''):
    election = Election.objects.get(pk=1)
    if action in ('candidate', 'user'):
        if action == 'candidate' and Ballot.objects.exists():
            raise ValidationError('已有選票，候選人名單已鎖定。')
        names = [name.strip() for name in value.split(',')]
        if any(not name or len(name) > 80 for name in names):
            raise ValidationError('每個姓名須為 1 至 80 字元，以逗號分隔，不可留空。')
        if action == 'candidate':
            Candidate.objects.bulk_create([Candidate(name=name) for name in names])
        else:
            for name in names:
                create_identity(name)
    elif action == 'open':
        if not Candidate.objects.exists():
            raise ValidationError('至少需要一位候選人才可開放。')
        election.is_open = True
    elif action == 'close':
        election.is_open = False
    elif action in ('clear', 'reset'):
        confirmation = '清除全部資料' if action == 'reset' else '清除全部選票'
        if value != confirmation:
            raise ValidationError(f'請確認「{confirmation}」操作。')
        Ballot.objects.all().delete()
        if action == 'reset':
            Identity.objects.filter(is_admin=False).delete()
            Candidate.objects.all().delete()
            LoginAttempt.objects.all().delete()
            admin_ids = set(Identity.objects.values_list('pk', flat=True))
            for session in Session.objects.iterator():
                if session.get_decoded().get('identity') not in admin_ids:
                    session.delete()
            sequences = [{'table': model._meta.db_table} for model in (Identity, Candidate, Ballot, Choice)]
            with connection.cursor() as cursor:
                for sql in connection.ops.sequence_reset_by_name_sql(no_style(), sequences):
                    cursor.execute(sql)
        election.is_open = False
        election.version += 1
    else:
        raise ValidationError('無效操作。')
    election.save()
