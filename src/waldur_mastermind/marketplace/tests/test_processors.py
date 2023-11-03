from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import utils
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories


class ProcessorsTest(test.APITransactionTestCase):
    def test_creating_of_resource(self):
        user = structure_factories.UserFactory(is_staff=True)
        success = []
        failed = []

        for offering_type in manager.get_offering_types():
            offering = factories.OfferingFactory(type=offering_type)
            item = factories.OrderItemFactory(offering=offering)
            item.set_state_executing()
            item.save(update_fields=['state'])
            utils.process_order_item(item, user)
            item.refresh_from_db()

            if not item.resource:
                failed.append(offering_type)
            else:
                success.append(offering_type)

        self.assertFalse(failed, f'failed: {failed}, success {success}')
