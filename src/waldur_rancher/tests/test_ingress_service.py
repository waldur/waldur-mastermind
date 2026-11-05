from unittest import mock

from django.urls import reverse
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_rancher import models, views

from . import factories, fixtures


def detail_url(obj, view_name):
    return "http://testserver" + reverse(view_name, kwargs={"uuid": obj.uuid.hex})


class StaysInPlaceTest(test.APITestCase):
    """An ingress or a service stays in the Rancher project and namespace it
    runs in, so its yaml and delete calls cannot reach another tenant's."""

    def setUp(self):
        self.fixture = fixtures.RancherFixture()
        settings = self.fixture.settings
        self.rancher_project = factories.ProjectFactory(
            settings=settings, cluster=self.fixture.cluster, backend_id="p-own"
        )
        self.namespace = factories.NamespaceFactory(
            settings=settings, project=self.rancher_project
        )

        # Another organization's cluster on the same Rancher server.
        other = fixtures.RancherFixture()
        other.cluster.settings = settings
        other.cluster.save()
        self.other_project = factories.ProjectFactory(
            settings=settings, cluster=other.cluster, backend_id="p-other"
        )
        self.other_namespace = factories.NamespaceFactory(
            settings=settings, project=self.other_project
        )
        self.client.force_authenticate(self.fixture.admin)

        # Neither view has an update executor.
        for view in (views.IngressViewSet, views.ServiceViewSet):
            patcher = mock.patch.object(view, "update_executor", create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def resource_fields(self):
        return {
            "settings": self.fixture.settings,
            "service_settings": self.fixture.settings,
            "project": self.fixture.project,
            "state": CoreStates.OK,
            "backend_id": "default:web",
        }

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_ingress_cannot_be_moved(self, client):
        ingress = models.Ingress.objects.create(
            name="web",
            cluster=self.fixture.cluster,
            rancher_project=self.rancher_project,
            namespace=self.namespace,
            **self.resource_fields(),
        )
        url = detail_url(ingress, "rancher-ingress-detail")
        for method in (self.client.patch, self.client.put):
            response = method(
                url,
                {
                    "name": "web",
                    "rancher_project": detail_url(
                        self.other_project, "rancher-project-detail"
                    ),
                    "namespace": detail_url(
                        self.other_namespace, "rancher-namespace-detail"
                    ),
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            ingress.refresh_from_db()
            self.assertEqual(ingress.rancher_project, self.rancher_project)
            self.assertEqual(ingress.namespace, self.namespace)

        response = self.client.put(url + "yaml/", {"yaml": "x"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        client.put_ingress_yaml.assert_called_once_with("p-own", "default:web", "x")
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        client.delete_ingress.assert_called_once_with("p-own", "default:web")

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_service_cannot_be_moved(self, client):
        service = models.Service.objects.create(
            name="web", namespace=self.namespace, **self.resource_fields()
        )
        url = detail_url(service, "rancher-service-detail")
        response = self.client.patch(
            url,
            {"namespace": detail_url(self.other_namespace, "rancher-namespace-detail")},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        service.refresh_from_db()
        self.assertEqual(service.namespace, self.namespace)

        response = self.client.put(url + "yaml/", {"yaml": "x"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        client.put_service_yaml.assert_called_once_with("p-own", "default:web", "x")
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        client.delete_service.assert_called_once_with("p-own", "default:web")
