import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '使用 SQLite 線上備份，儲存至資料庫旁的 backups 目錄。'

    def handle(self, *args, **options):
        source = Path(settings.DATABASES['default']['NAME'])
        folder = source.parent / 'backups'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / f'voting-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.sqlite3'
        temporary = target.with_suffix('.tmp')
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            with sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True) as src, sqlite3.connect(temporary) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('備份完整性檢查失敗。')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        self.stdout.write(str(target))
