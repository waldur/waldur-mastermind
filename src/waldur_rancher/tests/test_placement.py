from unittest import mock

from django.urls import reverse
from rest_framework import status, test

from waldur_core.core import executors as core_executors
from waldur_core.core.enums import CoreStates
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests.factories import ProjectFactory
from waldur_core.structure.tests.fixtures import add_user_to_project
from waldur_rancher import models

from . import factories, fixtures


def detail_url(obj, view_name):
    return "http://testserver" + reverse(view_name, kwargs={"uuid": obj.uuid.hex})


class PlacementIsKeptOnUpdateTest(test.APITestCase):
    """A cluster keeps its VM project and an HPA its workload on update."""

    def setUp(self):
        self.fixture = fixtures.RancherFixture()
        self.other = fixtures.RancherFixture()
        # The cluster administrator is only a member in the other organization.
        add_user_to_project(self.fixture.admin, self.other.project, ProjectRole.MEMBER)
        self.client.force_authenticate(self.fixture.admin)

        patcher = mock.patch.object(core_executors.BaseExecutor, "execute")
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_workload(self, fixture, name):
        rancher_project = factories.ProjectFactory(
            settings=fixture.settings, cluster=fixture.cluster
        )
        return models.Workload.objects.create(
            name=name,
            settings=fixture.settings,
            cluster=fixture.cluster,
            project=rancher_project,
            namespace=factories.NamespaceFactory(
                settings=fixture.settings, project=rancher_project
            ),
            scale=1,
        )

    def test_cluster_keeps_its_vm_project(self):
        cluster = self.fixture.cluster
        cluster.vm_project = self.fixture.project
        cluster.save()
        response = self.client.patch(
            factories.ClusterFactory.get_url(cluster),
            {"vm_project": ProjectFactory.get_url(self.other.project)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        cluster.refresh_from_db()
        self.assertEqual(cluster.vm_project, self.fixture.project)

    def test_hpa_keeps_its_workload(self):
        workload = self.make_workload(self.fixture, "own")
        foreign = self.make_workload(self.other, "foreign")
        hpa = models.HPA.objects.create(
            name="hpa",
            settings=self.fixture.settings,
            cluster=workload.cluster,
            project=workload.project,
            namespace=workload.namespace,
            workload=workload,
            min_replicas=1,
            max_replicas=2,
            metrics=[],
            state=CoreStates.OK,
        )
        url = detail_url(hpa, "rancher-hpa-detail")
        for method in (self.client.put, self.client.patch):
            response = method(
                url,
                {
                    "name": "renamed",
                    "workload": detail_url(foreign, "rancher-workload-detail"),
                    "min_replicas": 1,
                    "max_replicas": 3,
                    "metrics": [],
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            hpa.refresh_from_db()
            self.assertEqual(hpa.workload, workload)
            self.assertEqual(hpa.name, "renamed")
            self.assertEqual(hpa.max_replicas, 3)
