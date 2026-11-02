from unittest import mock

from ddt import data, ddt
from django.urls import reverse
from rest_framework import status, test

from waldur_core.core import executors as core_executors
from waldur_rancher import models

from . import factories, fixtures


@ddt
class HPACreateTest(test.APITestCase):
    """An HPA is created under a workload, so the workload's project decides who may."""

    def setUp(self):
        self.fixture = fixtures.RancherFixture()
        rancher_project = factories.ProjectFactory(
            settings=self.fixture.settings, cluster=self.fixture.cluster
        )
        namespace = factories.NamespaceFactory(
            settings=self.fixture.settings, project=rancher_project
        )
        self.workload = models.Workload.objects.create(
            name="workload",
            settings=self.fixture.settings,
            cluster=self.fixture.cluster,
            project=rancher_project,
            namespace=namespace,
            scale=1,
        )
        self.url = reverse("rancher-hpa-list")
        self.payload = {
            "name": "hpa",
            "workload": "http://testserver"
            + reverse(
                "rancher-workload-detail", kwargs={"uuid": self.workload.uuid.hex}
            ),
            "min_replicas": 1,
            "max_replicas": 2,
            "metrics": [],
        }
        patcher = mock.patch.object(core_executors.BaseExecutor, "execute")
        self.addCleanup(patcher.stop)
        patcher.start()

    @data("staff", "owner", "admin", "manager")
    def test_who_manages_the_workloads_project_can_create(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data("member", "customer_support", "global_support")
    def test_who_only_sees_the_project_cannot_create(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.HPA.objects.exists())

    def test_create_without_a_workload_is_refused(self):
        # The workload is what the check is made against; leaving it out must
        # not skip the check.
        self.client.force_authenticate(self.fixture.member)
        payload = {
            key: value for key, value in self.payload.items() if key != "workload"
        }
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("workload", response.data)
