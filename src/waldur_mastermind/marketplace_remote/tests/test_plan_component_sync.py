import uuid
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import signals
from django.test import TestCase

from waldur_core.logging import models as logging_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_remote import utils


class ImportPlansTest(TestCase):
    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering
        )
        self.components_map = {self.component.type: self.component}
        self.remote_plan_uuid = uuid.uuid4()

    def build_remote_plan(self, price, amount):
        """A stand-in for the plan the generated API client returns.

        The remote API serializes prices as strings, which is what makes an
        unchanged price look like a change locally.
        """
        return SimpleNamespace(
            uuid=self.remote_plan_uuid,
            prices=SimpleNamespace(to_dict=lambda: {self.component.type: price}),
            quotas=SimpleNamespace(to_dict=lambda: {self.component.type: amount}),
            to_dict=lambda: {
                "name": "LUMI Common",
                "description": "",
                "archived": False,
            },
        )

    def import_plan(self, price, amount=10):
        utils.import_plans(
            self.offering,
            [self.build_remote_plan(price, amount)],
            self.components_map,
        )

    def count_component_saves(self, price, amount=10):
        saved = []

        def record(sender, instance, **kwargs):
            saved.append(instance)

        signals.post_save.connect(
            record, sender=marketplace_models.PlanComponent, dispatch_uid="test_import"
        )
        try:
            self.import_plan(price, amount)
        finally:
            signals.post_save.disconnect(
                sender=marketplace_models.PlanComponent, dispatch_uid="test_import"
            )
        return len(saved)

    def get_component(self):
        return marketplace_models.PlanComponent.objects.get(
            plan__offering=self.offering, component=self.component
        )

    def price_events(self):
        return logging_models.Event.objects.filter(
            event_type="marketplace_plan_component_current_price_updated"
        )

    def test_first_import_creates_the_plan_component(self):
        self.import_plan("0.0106")

        self.assertEqual(self.get_component().price, Decimal("0.0106000000"))
        self.assertEqual(self.get_component().amount, 10)

    def test_reimporting_the_same_price_does_not_write_the_component(self):
        self.import_plan("0.0106")

        self.assertEqual(self.count_component_saves("0.0106"), 0)
        self.assertEqual(self.get_component().price, Decimal("0.0106000000"))
        self.assertFalse(self.price_events().exists())

    def test_reimporting_a_real_price_change_updates_and_logs_it(self):
        self.import_plan("0.0106")

        self.assertEqual(self.count_component_saves("0.0206"), 1)
        self.assertEqual(self.get_component().price, Decimal("0.0206000000"))
        self.assertEqual(self.price_events().count(), 1)

    def test_reimporting_a_real_quota_change_updates_it(self):
        self.import_plan("0.0106")

        self.assertEqual(self.count_component_saves("0.0106", amount=20), 1)
        self.assertEqual(self.get_component().amount, 20)
        self.assertFalse(self.price_events().exists())


class PullPlanComponentFieldsTest(TestCase):
    """The path an hourly offering pull takes for a plan that already exists.

    OfferingPullTask hands existing plans to sync_plan_components, which goes
    through pull_fields -- not through import_plans, which only sees plans that
    are new.
    """

    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering
        )
        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.plan, component=self.component, price="0.0106", amount=10
        )
        self.plan_component.refresh_from_db()

    def pull(self, price, amount=10):
        return utils.pull_fields(
            ["price", "amount"],
            self.plan_component,
            {"price": price, "amount": amount},
        )

    def test_pulling_the_same_price_changes_nothing(self):
        self.assertEqual(self.pull("0.0106"), set())
        self.assertFalse(
            logging_models.Event.objects.filter(
                event_type="marketplace_plan_component_current_price_updated"
            ).exists()
        )

    def test_pulling_a_real_price_change_is_stored_exactly(self):
        self.assertEqual(self.pull("0.0206"), {"price"})

        self.plan_component.refresh_from_db()
        self.assertEqual(self.plan_component.price, Decimal("0.0206000000"))

    def test_pulling_a_real_quota_change_is_stored(self):
        self.assertEqual(self.pull("0.0106", amount=20), {"amount"})

        self.plan_component.refresh_from_db()
        self.assertEqual(self.plan_component.amount, 20)

    def test_an_unparseable_price_is_left_alone(self):
        self.assertEqual(self.pull("not-a-number"), set())

        self.plan_component.refresh_from_db()
        self.assertEqual(self.plan_component.price, Decimal("0.0106000000"))


class PullDecimalFieldTest(TestCase):
    """pull_fields is shared by every remote pull, not just plan components.

    OfferingComponent carries decimal columns of its own -- min_value,
    max_value, default_limit, limit_amount -- which the hourly component pull
    puts through the same comparison at two decimal places rather than ten.
    """

    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, min_value="1.00"
        )
        self.component.refresh_from_db()

    def pull(self, min_value):
        return utils.pull_fields(
            ["min_value"], self.component, {"min_value": min_value}
        )

    def test_a_difference_the_column_cannot_hold_is_not_a_change(self):
        # Saving 1.005 into a two-place column stores 1.00, so treating it as
        # a change would rewrite the row on every pull, forever.
        self.assertEqual(self.pull("1.005"), set())

        self.component.refresh_from_db()
        self.assertEqual(self.component.min_value, Decimal("1.00"))

    def test_a_real_change_is_stored_at_the_column_precision(self):
        self.assertEqual(self.pull("2.5"), {"min_value"})

        self.component.refresh_from_db()
        self.assertEqual(self.component.min_value, Decimal("2.50"))

    def test_a_value_beyond_the_column_is_left_alone(self):
        self.assertEqual(self.pull("1E+30"), set())

        self.component.refresh_from_db()
        self.assertEqual(self.component.min_value, Decimal("1.00"))

    def test_a_value_that_is_not_a_number_is_left_alone(self):
        for remote_value in ("Infinity", "nan", "-inf", "not-a-number"):
            with self.subTest(remote_value=remote_value):
                self.assertEqual(self.pull(remote_value), set())

                self.component.refresh_from_db()
                self.assertEqual(self.component.min_value, Decimal("1.00"))


class PullNullFromRemoteTest(TestCase):
    """A value the remote cleared has to reach the local mirror."""

    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, min_value="1.00"
        )
        self.component.refresh_from_db()

    def test_a_cleared_bound_is_cleared_locally(self):
        changed = utils.pull_fields(["min_value"], self.component, {"min_value": None})

        self.assertEqual(changed, {"min_value"})
        self.component.refresh_from_db()
        self.assertIsNone(self.component.min_value)

    def test_a_bound_that_is_already_clear_is_not_rewritten(self):
        self.component.min_value = None
        self.component.save(update_fields=["min_value"])

        self.assertEqual(
            utils.pull_fields(["min_value"], self.component, {"min_value": None}), set()
        )

    def test_a_null_is_not_written_into_a_column_that_refuses_it(self):
        # description is NOT NULL locally; assigning the null would raise
        # IntegrityError on save and take the whole offering pull with it.
        self.assertEqual(
            utils.pull_fields(["description"], self.component, {"description": None}),
            set(),
        )

        self.component.refresh_from_db()
        self.assertEqual(self.component.description, "")
