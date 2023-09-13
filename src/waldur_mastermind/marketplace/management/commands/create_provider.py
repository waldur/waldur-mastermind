from django.core.management import call_command
from django.core.management.base import BaseCommand

from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace.models import ServiceProvider


def create_customer(name):
    customer, created = Customer.objects.get_or_create(name=name)
    if created:
        return customer
    else:
        return None


def create_provider(customer):
    ServiceProvider.objects.create(customer=customer)


class Command(BaseCommand):
    help = "Create service provider"

    def add_arguments(self, parser):
        parser.add_argument('-n', type=str, help='Customer name')
        parser.add_argument('-c', nargs='+', type=str, help='Category List of categories to load')

    def handle(self, *args, **options):
        name = options['n']
        customer_instance = create_customer(name)
        if customer_instance is not None:
            call_command('load_categories', options['c'])
            create_provider(customer_instance)
            self.stdout.write(self.style.SUCCESS('Service Provider created'))
        else:
            self.stdout.write(self.style.ERROR(f'Customer {name} already exists'))
