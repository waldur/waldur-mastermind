import datetime

from django.conf import settings
from django.test import TestCase

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class TestHandlers(TestCase):
    def setUp(self) -> None:
        settings.WALDUR_HPC['ENABLED'] = True
        settings.WALDUR_HPC['INTERNAL_EMAIL_PATTERNS'] = ['user@internal']
        settings.WALDUR_HPC['EXTERNAL_EMAIL_PATTERNS'] = ['user@external']
        self.internal_customer = structure_factories.CustomerFactory()
        self.external_customer = structure_factories.CustomerFactory()
        settings.WALDUR_HPC['INTERNAL_CUSTOMER_UUID'] = self.internal_customer.uuid.hex
        settings.WALDUR_HPC['EXTERNAL_CUSTOMER_UUID'] = self.external_customer.uuid.hex
        offering = marketplace_factories.OfferingFactory(shared=True)
        settings.WALDUR_HPC['OFFERING_UUID'] = offering.uuid.hex
        plan = marketplace_factories.PlanFactory(offering=offering)
        settings.WALDUR_HPC['PLAN_UUID'] = plan.uuid.hex

    def test_internal_user(self):
        user = structure_factories.UserFactory(email='user@internal')
        self.assertTrue(
            structure_models.Project.objects.filter(
                name=user.username, customer=self.internal_customer
            ).exists()
        )
        project = structure_models.Project.objects.get(
            name=user.username, customer=self.internal_customer
        )
        self.assertTrue(
            marketplace_models.Order.objects.filter(
                project=project,
                created_by=user,
                state=marketplace_models.Order.States.EXECUTING,
            ).exists()
        )

    def test_external_user(self):
        user = structure_factories.UserFactory(email='user@external')
        self.assertTrue(
            structure_models.Project.objects.filter(
                name=user.username, customer=self.external_customer
            ).exists()
        )
        project = structure_models.Project.objects.get(
            name=user.username, customer=self.external_customer
        )
        self.assertTrue(
            marketplace_models.Order.objects.filter(
                project=project,
                created_by=user,
                state=marketplace_models.Order.States.EXECUTING,
            ).exists()
        )

    def test_change_from_internal_to_external(self):
        user = structure_factories.UserFactory(email='user@internal')
        user.email = 'user@external'
        user.save()
        self.assertTrue(
            structure_models.Project.objects.filter(
                name=user.username, customer=self.external_customer
            ).exists()
        )

    def test_order_does_not_create_if_previous_order_has_not_been_processed(self):
        user = structure_factories.UserFactory(email='user@internal')
        user.last_login = datetime.datetime.now() + datetime.timedelta(days=1)
        user.save()

        project = structure_models.Project.objects.get(
            name=user.username, customer=self.internal_customer
        )

        self.assertEqual(
            marketplace_models.Order.objects.filter(
                project=project,
                created_by=user,
            ).count(),
            1,
        )

    def test_order_does_not_create_if_resource_has_been_created_successes_or_terminated(
        self,
    ):
        # first login
        user = structure_factories.UserFactory(email='user@internal')

        # order processing
        project = structure_models.Project.objects.get(
            name=user.username, customer=self.internal_customer
        )
        order = marketplace_models.Order.objects.get(
            project=project,
            created_by=user,
        )
        order.complete()
        order.save()
        resource = marketplace_factories.ResourceFactory(
            offering=order.offering, project=project
        )
        order.resource = resource
        order.save()

        # second login
        user.last_login = datetime.datetime.now() + datetime.timedelta(days=1)
        user.save()
        self.assertEqual(
            marketplace_models.Order.objects.filter(
                project=project,
                created_by=user,
            ).count(),
            1,
        )

        # terminate resource
        resource.set_state_terminated()
        resource.save()

        # third login
        user.last_login = datetime.datetime.now() + datetime.timedelta(days=1)
        user.save()
        self.assertEqual(
            marketplace_models.Order.objects.filter(
                project=project,
                created_by=user,
            ).count(),
            1,
        )

    def test_new_order_is_created_if_previous_order_has_failed(self):
        # first login
        user = structure_factories.UserFactory(email='user@internal')

        # order processing
        project = structure_models.Project.objects.get(
            name=user.username, customer=self.internal_customer
        )
        order = marketplace_models.Order.objects.get(
            project=project,
            created_by=user,
        )
        order.fail()
        order.save()

        # second login
        user.last_login = datetime.datetime.now() + datetime.timedelta(days=1)
        user.save()

        self.assertEqual(
            marketplace_models.Order.objects.filter(
                project=project,
                created_by=user,
            ).count(),
            2,
        )

        # third login
        user.last_login = datetime.datetime.now() + datetime.timedelta(days=2)
        user.save()

        self.assertEqual(
            marketplace_models.Order.objects.filter(
                project=project,
                created_by=user,
            ).count(),
            2,
        )

    def test_ignoring_of_other_orders(self):
        user = structure_factories.UserFactory(email='user@internal')
        project = structure_models.Project.objects.get(
            name=user.username, customer=self.internal_customer
        )
        self.assertEqual(
            marketplace_models.Order.objects.filter(
                project=project,
                created_by=user,
                state=marketplace_models.Order.States.EXECUTING,
            ).count(),
            1,
        )

        marketplace_factories.OrderFactory(project=project, created_by=user)
        self.assertEqual(
            marketplace_models.Order.objects.filter(
                project=project,
                created_by=user,
            ).count(),
            2,
        )

        # second login
        user.last_login = datetime.datetime.now() + datetime.timedelta(days=1)
        user.save()

        self.assertEqual(
            marketplace_models.Order.objects.filter(
                project=project,
                created_by=user,
            ).count(),
            2,
        )
