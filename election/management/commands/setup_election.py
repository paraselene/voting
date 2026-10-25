from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from election.services import fresh_setup


class Command(BaseCommand):
    help = '先備份，再建立固定候選人及 19 位編號用戶；保留管理員。'

    def add_arguments(self, parser):
        parser.add_argument('--confirm', required=True)

    def handle(self, *args, **options):
        if options['confirm'] != '清除全部資料':
            raise CommandError('請以 --confirm 清除全部資料 確認。舊用戶憑證將失效。')
        call_command('backup_db', stdout=self.stdout)
        fresh_setup()
        self.stdout.write('已建立 19 位編號用戶及 19 位候選人，請由管理頁匯出憑證。')
