from unittest import mock

from django.urls import reverse
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.structure.tests.factories import ProjectFactory
from waldur_rancher import models, views

from . import factories, fixtures

PLANTED = "../../../clusters/c-victim"


def detail_url(obj, view_name):
    return "http://testserver" + reverse(view_name, kwargs={"uuid": obj.uuid.hex})


class BackendIdIsReadOnlyTest(test.APITestCase):
    """The Rancher id goes into Rancher API paths, so callers cannot set it."""

    def setUp(self):
        self.fixture = fixtures.RancherFixture()
        settings = self.fixture.settings
        self.rancher_project = factories.ProjectFactory(
            settings=settings, cluster=self.fixture.cluster, backend_id="p-own"
        )
        self.namespace = factories.NamespaceFactory(
            settings=settings, project=self.rancher_project
        )
        self.client.force_authenticate(self.fixture.admin)

        # None of these views has an update executor, nor ingresses and
        # services a create executor.
        for view in (
            views.ApplicationViewSet,
            views.IngressViewSet,
            views.ServiceViewSet,
        ):
            for name in ("update_executor", "create_executor"):
                patcher = mock.patch.object(view, name, create=True)
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

    def own_placement(self):
        # Required on PUT where the serializer does not protect them.
        return {
            "service_settings": factories.RancherServiceSettingsFactory.get_url(
                self.fixture.settings
            ),
            "project": ProjectFactory.get_url(self.fixture.project),
        }

    def planted(self):
        return {
            "backend_id": PLANTED,
            "error_message": "planted",
            "runtime_state": "planted",
        }

    def assert_not_planted(self, obj):
        obj.refresh_from_db()
        self.assertEqual(obj.backend_id, "default:web")
        self.assertEqual(obj.error_message, "")
        self.assertNotEqual(obj.runtime_state, "planted")

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_ingress_update_ignores_backend_id(self, client):
        ingress = models.Ingress.objects.create(
            name="web",
            cluster=self.fixture.cluster,
            rancher_project=self.rancher_project,
            namespace=self.namespace,
            **self.resource_fields(),
        )
        url = detail_url(ingress, "rancher-ingress-detail")
        response = self.client.put(
            url,
            {
                "name": "web",
                "rancher_project": detail_url(
                    self.rancher_project, "rancher-project-detail"
                ),
                "namespace": detail_url(self.namespace, "rancher-namespace-detail"),
                **self.own_placement(),
                **self.planted(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assert_not_planted(ingress)

        self.client.put(url + "yaml/", {"yaml": "x"}, format="json")
        client.put_ingress_yaml.assert_called_once_with("p-own", "default:web", "x")
        self.client.delete(url)
        client.delete_ingress.assert_called_once_with("p-own", "default:web")

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_service_update_ignores_backend_id(self, client):
        service = models.Service.objects.create(
            name="web", namespace=self.namespace, **self.resource_fields()
        )
        url = detail_url(service, "rancher-service-detail")
        response = self.client.patch(url, self.planted(), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assert_not_planted(service)

        self.client.put(url + "yaml/", {"yaml": "x"}, format="json")
        client.put_service_yaml.assert_called_once_with("p-own", "default:web", "x")
        self.client.delete(url)
        client.delete_service.assert_called_once_with("p-own", "default:web")

    def test_application_update_ignores_backend_id(self):
        template = factories.TemplateFactory(
            settings=self.fixture.settings,
            catalog=factories.CatalogFactory(settings=self.fixture.settings),
        )
        application = models.Application.objects.create(
            name="app",
            cluster=self.fixture.cluster,
            rancher_project=self.rancher_project,
            namespace=self.namespace,
            template=template,
            version="1.0",
            **self.resource_fields(),
        )
        response = self.client.put(
            reverse("rancher-app-detail", kwargs={"uuid": application.uuid.hex}),
            {
                "name": "app",
                "template": factories.TemplateFactory.get_url(template),
                "rancher_project": factories.ProjectFactory.get_url(
                    self.rancher_project
                ),
                "namespace": factories.NamespaceFactory.get_url(self.namespace),
                "version": "1.0",
                "answers": {},
                **self.own_placement(),
                **self.planted(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assert_not_planted(application)

    def test_ingress_create_ignores_backend_id(self):
        response = self.client.post(
            reverse("rancher-ingress-list"),
            {
                "name": "web",
                "service_settings": factories.RancherServiceSettingsFactory.get_url(
                    self.fixture.settings
                ),
                "project": ProjectFactory.get_url(self.fixture.project),
                "rancher_project": detail_url(
                    self.rancher_project, "rancher-project-detail"
                ),
                "namespace": detail_url(self.namespace, "rancher-namespace-detail"),
                **self.planted(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        ingress = models.Ingress.objects.get(uuid=response.data["uuid"])
        self.assertEqual(ingress.backend_id, "")
        self.assertEqual(ingress.error_message, "")
