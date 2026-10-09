from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from election.services import create_identity


class Command(BaseCommand):
    help = '建立管理員；隨機密碼僅於建立時輸出。'

    def add_arguments(self, parser):
        parser.add_argument('--name', default='管理員')

    def handle(self, *args, **options):
        try:
            identity, password = create_identity(options['name'], is_admin=True)
        except ValidationError as exc:
            raise CommandError(' '.join(exc.messages)) from None
        self.stdout.write(f'管理員 #{identity.pk} 已建立。請立即安全保存密碼：{password}')
