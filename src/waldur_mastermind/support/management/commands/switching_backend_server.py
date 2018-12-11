from django.core.management.base import BaseCommand

from waldur_mastermind.support import backend as support_backend

from ... import models


class Command(BaseCommand):
    help = ("Backend data update if a server was switched.")

    def handle(self, *args, **options):
        models.RequestType.objects.all().delete()
        backend = support_backend.get_active_backend()
        backend.pull_request_types()

        for support_customer in models.SupportCustomer.objects.all():
            exists_user = backend.manager.search_users(support_customer.user.email)

            if exists_user:
                backend_user = exists_user[0]
            else:
                backend_user = backend.create_user(support_customer.user)

            support_customer.backend_id = backend_user.key
            support_customer.save()
