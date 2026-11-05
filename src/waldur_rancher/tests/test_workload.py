from unittest import mock

from django.urls import reverse
from rest_framework import status, test

from waldur_rancher import models

from . import factories, fixtures


def detail_url(obj, view_name):
    return "http://testserver" + reverse(view_name, kwargs={"uuid": obj.uuid.hex})


class WorkloadUpdateTest(test.APITestCase):
    """A workload stays in the cluster, Rancher project and namespace it runs in."""

    def setUp(self):
        self.fixture = fixtures.RancherFixture()
        rancher_project = factories.ProjectFactory(
            settings=self.fixture.settings,
            cluster=self.fixture.cluster,
            backend_id="p-own",
        )
        namespace = factories.NamespaceFactory(
            settings=self.fixture.settings, project=rancher_project
        )
        self.workload = models.Workload.objects.create(
            name="nginx",
            settings=self.fixture.settings,
            cluster=self.fixture.cluster,
            project=rancher_project,
            namespace=namespace,
            scale=1,
            backend_id="deployment:default:nginx",
        )

        # Another organization's cluster on the same Rancher server.
        other = fixtures.RancherFixture()
        other.cluster.settings = self.fixture.settings
        other.cluster.save()
        self.other_project = factories.ProjectFactory(
            settings=self.fixture.settings, cluster=other.cluster, backend_id="p-other"
        )
        self.other_namespace = factories.NamespaceFactory(
            settings=self.fixture.settings, project=self.other_project
        )
        self.other_cluster = other.cluster

        self.url = detail_url(self.workload, "rancher-workload-detail")
        self.elsewhere = {
            "cluster": detail_url(self.other_cluster, "rancher-cluster-detail"),
            "project": detail_url(self.other_project, "rancher-project-detail"),
            "namespace": detail_url(self.other_namespace, "rancher-namespace-detail"),
        }
        self.client.force_authenticate(self.fixture.admin)

    def assert_in_place(self):
        self.workload.refresh_from_db()
        self.assertEqual(self.workload.cluster, self.fixture.cluster)
        self.assertEqual(self.workload.project.backend_id, "p-own")
        self.assertNotEqual(self.workload.namespace, self.other_namespace)

    def test_patch_cannot_move_the_workload(self):
        for field, value in self.elsewhere.items():
            response = self.client.patch(self.url, {field: value}, format="json")
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.assert_in_place()

    def test_put_cannot_move_the_workload(self):
        response = self.client.put(
            self.url, {"name": "nginx", "scale": 2, **self.elsewhere}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assert_in_place()
        self.assertEqual(self.workload.scale, 2)

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_redeploy_after_an_attempted_move_acts_on_its_own_project(self, client):
        self.client.patch(
            self.url, {"project": self.elsewhere["project"]}, format="json"
        )
        response = self.client.post(self.url + "redeploy/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        client.redeploy_workload.assert_called_once_with(
            "p-own", "deployment:default:nginx"
        )
