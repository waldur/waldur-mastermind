from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError
from django.utils import timezone
from rest_framework import test, status
from six.moves import mock

from waldur_core.core import utils as core_utils
from waldur_core.logging import models, loggers
from waldur_core.logging.tests import factories
# Dependency from `structure` application exists only in tests
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories


class AlertsListTest(test.APITransactionTestCase):

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.owner = structure_factories.UserFactory()
        self.customer.add_user(self.owner, structure_models.CustomerRole.OWNER)

    def test_customer_owner_can_see_alert_about_his_project(self):
        project = structure_factories.ProjectFactory(customer=self.customer)
        alert = factories.AlertFactory(scope=project)

        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.AlertFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(alert.uuid.hex, [a['uuid'] for a in response.data])

    def test_customer_owner_cannot_see_alert_about_other_customer(self):
        alert = factories.AlertFactory()

        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.AlertFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(alert.uuid.hex, [a['uuid'] for a in response.data])

    def test_alert_list_can_be_filtered_by_scope(self):
        project = structure_factories.ProjectFactory(customer=self.customer)
        alert1 = factories.AlertFactory(scope=project)
        alert2 = factories.AlertFactory()

        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.AlertFactory.get_list_url(), data={
            'scope': structure_factories.ProjectFactory.get_url(project)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(alert1.uuid.hex, [a['uuid'] for a in response.data])
        self.assertNotIn(alert2.uuid.hex, [a['uuid'] for a in response.data])

    def test_alert_list_can_be_filtered_by_scope_type(self):
        # XXX: this tests will removed after content type filter implementation at portal
        project = structure_factories.ProjectFactory(customer=self.customer)
        alert1 = factories.AlertFactory(scope=project)
        alert2 = factories.AlertFactory(scope=self.customer)

        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.AlertFactory.get_list_url(), data={'scope_type': 'customer'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(alert2.uuid.hex, [a['uuid'] for a in response.data])
        self.assertNotIn(alert1.uuid.hex, [a['uuid'] for a in response.data])

    def test_alert_list_can_be_aggregated_for_concreate_customer(self):
        project = structure_factories.ProjectFactory(customer=self.customer)
        alert1 = factories.AlertFactory(scope=project)
        alert2 = factories.AlertFactory()

        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.AlertFactory.get_list_url(), data={
            'aggregate': 'customer', 'uuid': self.customer.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(alert1.uuid.hex, [a['uuid'] for a in response.data])
        self.assertNotIn(alert2.uuid.hex, [a['uuid'] for a in response.data])

    def test_alert_list_can_be_filtered_by_created_date(self):
        project = structure_factories.ProjectFactory(customer=self.customer)
        alert1 = factories.AlertFactory(scope=project, created=timezone.now() - timedelta(days=1))
        alert2 = factories.AlertFactory(scope=project, created=timezone.now() - timedelta(days=3))

        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.AlertFactory.get_list_url(), data={
            'created_from': core_utils.datetime_to_timestamp(timezone.now() - timedelta(days=2)),
            'created_to': core_utils.datetime_to_timestamp(timezone.now())})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(alert1.uuid.hex, [a['uuid'] for a in response.data])
        self.assertNotIn(alert2.uuid.hex, [a['uuid'] for a in response.data])

    def test_alert_list_can_be_filtered_by_severity_list(self):
        project = structure_factories.ProjectFactory(customer=self.customer)
        alert1 = factories.AlertFactory(scope=project, severity=models.Alert.SeverityChoices.WARNING)
        alert2 = factories.AlertFactory(scope=project, severity=models.Alert.SeverityChoices.ERROR)
        alert3 = factories.AlertFactory(scope=project, severity=models.Alert.SeverityChoices.INFO)

        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.AlertFactory.get_list_url(), data={'severity': ['Warning', 'Error']})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(alert1.uuid.hex, [a['uuid'] for a in response.data])
        self.assertIn(alert2.uuid.hex, [a['uuid'] for a in response.data])
        self.assertNotIn(alert3.uuid.hex, [a['uuid'] for a in response.data])

    def test_alert_can_be_filtered_by_content_type(self):
        project = structure_factories.ProjectFactory(customer=self.customer)
        alert1 = factories.AlertFactory(scope=project)
        alert2 = factories.AlertFactory(scope=self.customer)

        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.AlertFactory.get_list_url(), data={'content_type': ['structure.project']})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(alert1.uuid.hex, [a['uuid'] for a in response.data])
        self.assertNotIn(alert2.uuid.hex, [a['uuid'] for a in response.data])

    def test_alert_can_be_filtered_by_scope(self):
        project = structure_factories.ProjectFactory(customer=self.customer)
        alert1 = factories.AlertFactory(scope=project)
        alert2 = factories.AlertFactory(scope=self.customer)

        self.client.force_authenticate(self.owner)
        response = self.client.get(
            factories.AlertFactory.get_list_url(),
            data={'scope': structure_factories.ProjectFactory.get_url(project)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(alert1.uuid.hex, [a['uuid'] for a in response.data])
        self.assertNotIn(alert2.uuid.hex, [a['uuid'] for a in response.data])


class AlertsCreateUpdateDeleteTest(test.APITransactionTestCase):

    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.staff = get_user_model().objects.create_superuser(
            username='staff', password='staff', email='staff@example.com')
        self.alert = factories.AlertFactory.build(scope=self.project, severity=10)
        severity_names = dict(models.Alert.SeverityChoices.CHOICES)
        self.valid_data = {
            'scope': structure_factories.ProjectFactory.get_url(self.project),
            'alert_type': self.alert.alert_type,
            'message': self.alert.message,
            'severity': severity_names[self.alert.severity],
        }
        self.url = factories.AlertFactory.get_list_url()

    def test_alert_can_be_created_by_staff(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        ct = ContentType.objects.get_for_model(structure_models.Project)
        self.assertTrue(models.Alert.objects.filter(
            content_type=ct, object_id=self.project.id, alert_type=self.alert.alert_type).exists())

    def test_alert_severity_can_be_updated(self):
        self.alert.save()
        self.valid_data['severity'] = 'Error'

        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        ct = ContentType.objects.get_for_model(structure_models.Project)
        self.assertTrue(models.Alert.objects.filter(
            pk=self.alert.pk,
            content_type=ct,
            object_id=self.project.id,
            alert_type=self.alert.alert_type,
            severity=models.Alert.SeverityChoices.ERROR).exists()
        )

    def test_alert_can_be_closed(self):
        self.alert.save()

        self.client.force_authenticate(self.staff)
        response = self.client.post(factories.AlertFactory.get_url(self.alert, 'close'))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        ct = ContentType.objects.get_for_model(structure_models.Project)
        self.assertTrue(models.Alert.objects.filter(
            content_type=ct,
            object_id=self.project.id,
            alert_type=self.alert.alert_type,
            closed__isnull=False).exists()
        )


class TestAlertActions(test.APITransactionTestCase):

    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.staff = get_user_model().objects.create_superuser(
            username='staff', password='staff', email='staff@example.com')
        self.alert = factories.AlertFactory(scope=self.project)
        self.admin = structure_factories.UserFactory()
        self.project.add_user(self.admin, structure_models.ProjectRole.ADMINISTRATOR)

    def test_alert_can_be_closed_by_staff(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(factories.AlertFactory.get_url(self.alert, 'close'))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        reread_alert = models.Alert.objects.get(pk=self.alert.pk)
        self.assertTrue(reread_alert.closed is not None)

    def test_alert_can_not_be_closed_by_project_administrator(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(factories.AlertFactory.get_url(self.alert, 'close'))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        reread_alert = models.Alert.objects.get(pk=self.alert.pk)
        self.assertTrue(reread_alert.closed is None)

    def test_alert_can_be_marked_as_acknowledged_by_project_administrator(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(factories.AlertFactory.get_url(self.alert, 'acknowledge'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reread_alert = models.Alert.objects.get(pk=self.alert.pk)
        self.assertTrue(reread_alert.acknowledged)

    def test_acknowledged_alert_cannot_be_marked_as_acknowledged_again(self):
        self.alert.acknowledge()

        self.client.force_authenticate(self.admin)
        response = self.client.post(factories.AlertFactory.get_url(self.alert, 'acknowledge'))

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_admin_can_cancel_acknowledgment_of_acknowledged_alert(self):
        self.alert.acknowledge()

        self.client.force_authenticate(self.admin)
        response = self.client.post(factories.AlertFactory.get_url(self.alert, 'cancel_acknowledgment'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reread_alert = models.Alert.objects.get(pk=self.alert.pk)
        self.assertFalse(reread_alert.acknowledged)


class AlertUniquenessTest(test.APITransactionTestCase):

    def setUp(self):
        self.project = structure_factories.ProjectFactory()

    def get_logger(self):
        if not hasattr(loggers.alert_logger, 'test_alert_logger'):
            class TestAlertLogger(loggers.AlertLogger):
                class Meta:
                    alert_types = ('test_alert',)

            loggers.alert_logger.register('test_alert_logger', TestAlertLogger)

        return loggers.alert_logger.test_alert_logger

    def log_alert(self):
        return self.get_logger().info('Message', scope=self.project, alert_type='test_alert')

    def test_duplicate_alert_is_not_created(self):
        alert, created = self.log_alert()
        self.assertEqual(created, True)

        alert, created = self.log_alert()
        self.assertEqual(created, False)

    def test_if_race_conditions_detected_alert_skipped(self):
        with mock.patch('waldur_core.logging.loggers.models') as mock_models:
            mock_models.Alert.objects.create.side_effect = IntegrityError

            alert, created = self.log_alert()
            self.assertEqual(created, False)
