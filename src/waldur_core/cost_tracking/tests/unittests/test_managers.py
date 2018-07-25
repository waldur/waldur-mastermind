from django.test import TransactionTestCase
from freezegun import freeze_time

from waldur_core.cost_tracking import models, ConsumableItem
from waldur_core.cost_tracking.tests import factories


class ConsumptionDetailsManagerTest(TransactionTestCase):

    def test_create_method_get_configuration_from_previous_month_details(self):
        with freeze_time("2016-08-01"):
            configuration = {ConsumableItem('storage', '1 MB'): 10240, ConsumableItem('ram', '1 MB'): 2048}
            price_estimate = factories.PriceEstimateFactory(year=2016, month=8)
            consumption_details = factories.ConsumptionDetailsFactory(price_estimate=price_estimate)
            consumption_details.update_configuration(configuration)

        with freeze_time("2016-09-01"):
            next_price_estimate = models.PriceEstimate.objects.create(
                scope=price_estimate.scope,
                month=price_estimate.month + 1,
                year=price_estimate.year,
            )
            next_consumption_details = models.ConsumptionDetails.objects.create(price_estimate=next_price_estimate)
        self.assertDictEqual(next_consumption_details.configuration, configuration)
