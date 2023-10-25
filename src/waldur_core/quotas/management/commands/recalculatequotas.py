from django.core.management.base import BaseCommand

from waldur_core.quotas import fields, signals
from waldur_core.quotas.utils import get_models_with_quotas


class Command(BaseCommand):
    help = """Recalculate all quotas"""

    def handle(self, *args, **options):
        # TODO: implement other quotas recalculation
        # TODO: implement global stale quotas deletion
        self.recalculate_counter_quotas()
        self.recalculate_aggregator_quotas()
        self.stdout.write(
            'XXX: Second time to make sure that aggregators of aggregators where calculated properly.'
        )
        self.recalculate_aggregator_quotas()
        self.recalculate_custom_quotas()

    def recalculate_counter_quotas(self):
        self.stdout.write('Recalculating counter quotas')
        for model in get_models_with_quotas():
            for counter_field in model.get_quotas_fields(
                field_class=fields.CounterQuotaField
            ):
                for instance in model.objects.all():
                    counter_field.recalculate(scope=instance)
        self.stdout.write('...done')

    def recalculate_aggregator_quotas(self):
        # TODO: recalculate child quotas first
        self.stdout.write('Recalculating aggregator quotas')
        for model in get_models_with_quotas():
            for aggregator_field in model.get_quotas_fields(
                field_class=fields.UsageAggregatorQuotaField
            ):
                for instance in model.objects.all():
                    aggregator_field.recalculate(scope=instance)
        self.stdout.write('...done')

    def recalculate_custom_quotas(self):
        self.stdout.write('Recalculating custom quotas')
        signals.recalculate_quotas.send(sender=self)
        self.stdout.write('...done')
