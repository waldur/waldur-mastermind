import collections

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db.models import Q
import six
from six.moves import input

from waldur_core.cost_tracking import CostTrackingRegister
from waldur_core.cost_tracking.models import PriceEstimate
from waldur_core.structure import models as structure_models


# XXX: This command is not working properly now.
#      We need to decide is it necessary with new structure.
class Command(BaseCommand):
    """
    This management command removes following price estimates:

    1) Price estimates with invalid content type.
       For example, resource plugin has been uninstalled,
       but its price estimates remained.

    2) Price estimates without valid scope and without details.
       For example, resource has been deleted, but its
       price estimate doesn't contain updated details,
       or its price estimates should be deleted.

    3) Price estimates for invalid month.
       Price estimates for each month should contain price estimate
       for at least one resource, one service, and one project.
       Otherwise it is considered invalid.
    """
    help = 'Delete invalid price estimates'

    def add_arguments(self, parser):
        parser.add_argument('--assume-yes', dest='assume_yes', action='store_true')
        parser.set_defaults(assume_yes=False)

    def handle(self, assume_yes, **options):
        self.assume_yes = assume_yes
        self.delete_price_estimates_for_invalid_content_types()
        self.delete_price_estimates_without_scope_and_details()
        self.delete_price_estimates_for_invalid_month()

    def confirm(self):
        if self.assume_yes:
            return True
        confirm = input('Enter [y] to continue: ')
        return confirm.strip().lower() == 'y'

    def delete_price_estimates_for_invalid_month(self):
        invalid_estimates = self.get_all_estimates_wihout_scope_in_month()
        count = invalid_estimates.count()
        if count:
            self.stdout.write('{} price estimates without scope in month would be deleted.'.
                              format(count))
            if self.confirm():
                invalid_estimates.delete()

    def get_all_estimates_wihout_scope_in_month(self):
        invalid_estimates = []
        for customer in structure_models.Customer.objects.all():
            customer_estimates = self.get_estimates_without_scope_in_month(customer)
            invalid_estimates.extend(customer_estimates)
        ids = [estimate.pk for estimate in invalid_estimates]
        return PriceEstimate.objects.filter(pk__in=ids)

    def get_estimated_models(self):
        return (
            structure_models.Customer,
            structure_models.ServiceSettings,
            structure_models.Service,
            structure_models.ServiceProjectLink,
            structure_models.Project,
        ) + tuple(CostTrackingRegister.registered_resources.keys())

    def get_estimates_without_scope_in_month(self, customer):
        """
        It is expected that valid row for each month contains at least one
        price estimate for customer, service setting, service,
        service project link, project and resource.
        Otherwise all price estimates in the row should be deleted.
        """
        estimates = self.get_price_estimates_for_customer(customer)
        if not estimates:
            return []

        tables = {model: collections.defaultdict(list)
                  for model in self.get_estimated_models()}

        dates = set()
        for estimate in estimates:
            date = (estimate.year, estimate.month)
            dates.add(date)

            cls = estimate.content_type.model_class()
            for model, table in tables.items():
                if issubclass(cls, model):
                    table[date].append(estimate)
                    break

        invalid_estimates = []
        for date in dates:
            if any(map(lambda table: len(table[date]) == 0, tables.values())):
                for table in tables.values():
                    invalid_estimates.extend(table[date])
        print(invalid_estimates)
        return invalid_estimates

    def get_price_estimates_for_customer(self, customer):
        descendants_estimates = []
        customer_descendants = customer.get_descendants()
        for descendant in customer_descendants:
            descendants_estimates += list(PriceEstimate.objects.filter(scope=descendant))
        customer_estimates = PriceEstimate.objects.filter(scope=customer)
        return list(customer_estimates) + descendants_estimates

    def delete_price_estimates_without_scope_and_details(self):
        invalid_estimates = self.get_invalid_price_estimates()
        count = invalid_estimates.count()
        if count:
            self.stdout.write('{} price estimates without scope and details would be deleted.'.
                              format(count))
            if self.confirm():
                invalid_estimates.delete()

    def get_invalid_price_estimates(self):
        query = Q(details='', object_id=None)
        for model in PriceEstimate.get_estimated_models():
            content_type = ContentType.objects.get_for_model(model)
            ids = set(model.objects.all().values_list('id', flat=True))
            if ids:
                query |= Q(content_type=content_type, object_id__in=ids)
        return PriceEstimate.objects.all().exclude(query)

    def delete_price_estimates_for_invalid_content_types(self):
        content_types = self.get_invalid_content_types()
        content_types_list = ', '.join(map(six.text_type, content_types))

        query = Q(content_type__in=content_types) | Q(content_type__isnull=True)
        invalid_estimates = PriceEstimate.objects.all().filter(query).filter()
        count = invalid_estimates.count()

        if count:
            self.stdout.write('{} price estimates for invalid content types would be deleted: {}'.
                              format(count, content_types_list))
            if self.confirm():
                invalid_estimates.delete()

    def get_invalid_content_types(self):
        valid = [
            ContentType.objects.get_for_model(model)
            for model in PriceEstimate.get_estimated_models()
        ]
        invalid = set(
            PriceEstimate.objects.all()
            .exclude(content_type__in=valid)
            .distinct()
            .values_list('content_type_id', flat=True)
        )

        return ContentType.objects.all().filter(id__in=invalid)
