from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.invoices import vat


class Command(BaseCommand):
    help = (
        "Fetch standard EU VAT rates from the European Commission TEDB service "
        "for a billing month, the current one by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int)
        parser.add_argument("--month", type=int, choices=range(1, 13))

    def handle(self, *args, **options):
        year, month = options["year"], options["month"]
        if (year is None) != (month is None):
            raise CommandError("Pass both --year and --month, or neither.")
        try:
            rates = vat.refresh_vat_rates(year, month)
        except vat.VATRatesFetchError as e:
            raise CommandError(str(e)) from e
        for country, rate in sorted(rates.items()):
            self.stdout.write(f"{country}: {rate}%")
