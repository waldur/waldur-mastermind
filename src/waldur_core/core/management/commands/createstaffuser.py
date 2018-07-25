from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Create a user with a specified username and password. User will be created as staff."

    def add_arguments(self, parser):
        parser.add_argument('-u', '--username', dest='username', required=True)
        parser.add_argument('-p', '--password', dest='password', required=True)

    def handle(self, *args, **options):
        User = get_user_model()

        username = options['username']
        password = options['password']

        user, created = User.objects.get_or_create(
            username=username, defaults=dict(last_login=timezone.now(), is_staff=True))
        if not created:
            raise CommandError('Username %s is already taken.' % username)

        user.set_password(password)
        user.save()
        self.stdout.write(self.style.SUCCESS('User %s has been created.' % username))
