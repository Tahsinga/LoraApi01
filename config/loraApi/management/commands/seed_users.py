from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = 'Seed required app users for the web login.'

    def handle(self, *args, **options):
        User = get_user_model()

        accounts = [
            ("PRIVY", "@dmin123"),
            ("Anesu", "@nesu123"),
            ("Admin", "@dm1n4182"),
        ]

        for username, password in accounts:
            user, created = User.objects.get_or_create(username=username)
            if created:
                user.set_password(password)
                user.is_staff = True
                user.is_superuser = False
                user.save()
                self.stdout.write(self.style.SUCCESS(f"Created user: {username}"))
            else:
                user.set_password(password)
                user.is_staff = True
                user.is_superuser = False
                user.save()
                self.stdout.write(self.style.WARNING(f"Updated user: {username}"))

        self.stdout.write(self.style.SUCCESS('User seeding complete.'))
