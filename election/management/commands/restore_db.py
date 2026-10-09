import os
import sqlite3
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections


class Command(BaseCommand):
    help = '停止網站後還原 SQLite 備份；先備份原資料。'

    def add_arguments(self, parser):
        parser.add_argument('backup')
        parser.add_argument('--confirm', required=True)

    def handle(self, *args, **options):
        if options['confirm'] != '還原資料庫':
            raise CommandError('請以 --confirm 還原資料庫 確認，並先停止網站。')
        source = Path(options['backup']).resolve()
        target = Path(settings.DATABASES['default']['NAME']).resolve()
        if source == target or not source.is_file():
            raise CommandError('備份路徑無效。')
        connections.close_all()
        temporary = target.with_suffix('.restore.tmp')
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            raise CommandError('已有還原暫存檔，請先檢查前次還原狀態。') from None
        try:
            with sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True) as src, sqlite3.connect(temporary) as dst:
                if src.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise CommandError('備份完整性檢查失敗。')
                tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'election_identity', 'election_election', 'election_ballot'} <= tables:
                    raise CommandError('不是本系統的備份。')
                src.backup(dst)
                # Restored credentials must never revive sessions from the backup.
                dst.execute('DELETE FROM django_session')
                dst.execute('UPDATE election_election SET version = version + 1, is_open = 0')
            for suffix in ('-wal', '-shm', '-journal'):
                Path(str(target) + suffix).unlink(missing_ok=True)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        self.stdout.write('資料庫已還原；投票已關閉，所有用戶須重新登入。')
