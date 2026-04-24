from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Trigger OpenStack usage billing poll synchronously."

    def handle(self, *args, **options):
        from waldur_openstack.tasks import tenant_usage_billing_poll

        tenant_usage_billing_poll()
        self.stdout.write("OpenStack usage polling completed.")
