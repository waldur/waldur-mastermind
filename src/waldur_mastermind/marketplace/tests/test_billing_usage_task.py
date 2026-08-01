from django.test import TestCase

from waldur_mastermind.marketplace import billing_usage
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories


class ProcessComponentUsageBillingQueryTest(TestCase):
    """One billing task is queued per ComponentUsage save.

    Anything the task lazy-loads is therefore paid once per usage row, so a
    usage import creating thousands of rows multiplies every missing
    select_related by that count.
    """

    def test_billing_related_rows_are_loaded_up_front(self):
        usage = factories.ComponentUsageFactory()

        loaded = marketplace_models.ComponentUsage.objects.select_related(
            *billing_usage.BILLING_RELATED_FIELDS
        ).get(pk=usage.pk)

        # Every relation the task walks must already be in memory.
        with self.assertNumQueries(0):
            loaded.resource.project
            loaded.resource.offering
            loaded.component
            loaded.plan_period

    def test_task_does_not_lazy_load_related_rows(self):
        usage = factories.ComponentUsageFactory()
        # Warm ContentType and constance caches.
        billing_usage.process_component_usage_billing(usage.id, False, True)

        with self.assertNumQueries(4):
            # 1 usage fetch + savepoint/release + 1 policy lookup. Without the
            # select_related this was 9: resource, offering, project,
            # component and plan_period each cost an extra round-trip.
            billing_usage.process_component_usage_billing(usage.id, False, True)
