from django.core.management.base import BaseCommand

from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace.management.commands import load_categories
from waldur_mastermind.marketplace.models import ServiceProvider


class Command(BaseCommand):
    help = "Create a service provider with a linked customer and load categories"

    def add_arguments(self, parser):
        parser.add_argument("-n", type=str, help="Customer name")
        parser.add_argument(
            "-c", nargs="+", type=str, help="List of categories to load"
        )

    def handle(self, *args, **options):
        name = options["n"]
        customer, created = Customer.objects.get_or_create(name=name)
        if created:
            for category in options["c"]:
                try:
                    loaded_category = load_categories.load_category(category)
                except KeyError:
                    self.stdout.write(
                        self.style.ERROR(f"Category {category} not found")
                    )
                    continue
                if loaded_category:
                    self.stdout.write(self.style.SUCCESS(f"Loaded category {category}"))
            ServiceProvider.objects.create(customer=customer)
            self.stdout.write(self.style.SUCCESS("Service Provider created"))
        else:
            self.stdout.write(self.style.ERROR(f"Customer {name} already exists"))
