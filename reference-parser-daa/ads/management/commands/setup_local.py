import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from parsers.models import Parser


class Command(BaseCommand):
    help = 'Create the local admin and enable the three supported parsers.'

    def handle(self, *args, **options):
        username = os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin')
        password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')
        if not password:
            raise CommandError('Set DJANGO_SUPERUSER_PASSWORD in .env first.')
        users = get_user_model()
        if not users.objects.filter(username=username).exists():
            users.objects.create_superuser(
                username=username,
                email=os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@localhost'),
                password=password,
            )
            self.stdout.write('Local admin created. Password is stored in .env.')
        else:
            self.stdout.write('Local admin already exists; password unchanged.')
        for name in ('drom', 'avito', 'autoru'):
            _, created = Parser.objects.get_or_create(name=name, defaults={'is_active': True})
            self.stdout.write(f'{name}: {"created" if created else "already configured"}')
