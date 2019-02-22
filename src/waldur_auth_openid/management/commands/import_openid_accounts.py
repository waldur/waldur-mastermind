from __future__ import unicode_literals

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from waldur_core.core.utils import DryRunCommand

User = get_user_model()


class Command(DryRunCommand):
    help_text = 'Append civil number with country code for OpenID users.'

    def handle(self, dry_run, *args, **options):
        conf = settings.WALDUR_AUTH_OPENID
        country_code = conf['COUNTRY_CODE']
        registration_method = conf['NAME']

        with transaction.atomic():
            users = User.objects.filter(registration_method=registration_method)\
                .exclude(civil_number__startswith=country_code)\
                .exclude(civil_number='') \
                .exclude(civil_number=None)
            count = users.count()
            if not dry_run:
                for user in users:
                    user.civil_number = '%s%s' % (country_code, user.civil_number)
                    user.save(update_fields=['civil_number'])

        self.stdout.write(self.style.SUCCESS('Civil numbers have been updated for %s users.' % count))
