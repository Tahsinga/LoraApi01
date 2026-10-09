from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


DEFAULT_USERS = [
    ('Admin', '@dm1n4182', True),
    ('PRIVY', '@dmin123', False),
    ('Anesu', '@nesu123', False),
]


class Command(BaseCommand):
    help = 'Create or update the required application login accounts with hardcoded credentials.'

    def handle(self, *args, **options):
        User = get_user_model()

        for username, password, is_superuser in DEFAULT_USERS:
            user, created = User.objects.get_or_create(username=username)
            user.set_password(password)
            user.is_staff = True
            user.is_superuser = is_superuser
            user.save()

            if created:
                self.stdout.write(self.style.SUCCESS(f'Created user: {username}'))
            else:
                self.stdout.write(self.style.WARNING(f'Updated user: {username}'))

        self.stdout.write(self.style.SUCCESS('Hardcoded app login accounts ensured.'))
