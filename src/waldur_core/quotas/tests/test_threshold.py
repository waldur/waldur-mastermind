from django.contrib.contenttypes.models import ContentType
from rest_framework import test, status

from waldur_core.logging.models import Alert
from waldur_core.logging.tasks import check_threshold
from waldur_core.quotas.tests.factories import QuotaFactory
from waldur_core.structure.tests.factories import ProjectFactory, UserFactory


class QuotaThresholdAlertTest(test.APITestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.quota = self.project.quotas.get(name='nc_resource_count')

    def test_if_quota_usage_is_over_threshold_alert_is_created(self):
        self.quota.threshold = 100
        self.quota.usage = 200
        self.quota.save()

        check_threshold()

        self.assertTrue(Alert.objects.filter(
            content_type=ContentType.objects.get_for_model(self.project),
            object_id=self.project.id,
            alert_type='threshold_exceeded').exists())

    def test_if_quota_usage_is_above_threshold_alert_is_not_created(self):
        self.quota.threshold = 100
        self.quota.usage = 20
        self.quota.save()

        check_threshold()

        self.assertFalse(Alert.objects.filter(
            content_type=ContentType.objects.get_for_model(self.project),
            object_id=self.project.id,
            alert_type='threshold_exceeded').exists())

    def test_user_can_update_threshold(self):
        self.client.force_authenticate(UserFactory(is_staff=True))

        response = self.client.put(QuotaFactory.get_url(self.quota), {
            'threshold': 1000,
        })
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)
        self.assertEqual(1000, response.data['threshold'], response.data)
