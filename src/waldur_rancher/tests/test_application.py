from unittest import mock

from ddt import data, ddt
from django.urls import reverse
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests.factories import ProjectFactory
from waldur_core.structure.tests.fixtures import add_user_to_project
from waldur_rancher import models, views

from . import factories, fixtures, utils


@ddt
class ApplicationCreateTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_create_is_disabled_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post("/api/rancher-apps/")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    @data("staff", "owner", "admin", "manager")
    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_who_manages_the_project_can_create(self, user, mock_client):
        mock_client.create_application.return_value = {"data": {}}
        response = self.post_application(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data("member", "customer_support", "global_support")
    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_who_only_sees_the_project_cannot_create(self, user, mock_client):
        response = self.post_application(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_client.create_application.assert_not_called()

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_refused_create_leaves_no_namespace_behind(self, mock_client):
        # Validation creates a namespace named in the request; a refused
        # create must not keep it.
        response = self.post_application(
            self.fixture.member, namespace_name="left-behind"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.Namespace.objects.filter(name="left-behind").exists())

    def post_application(self, user, namespace_name=None):
        self.client.force_authenticate(user)

        catalog = factories.CatalogFactory(settings=self.fixture.settings)
        project = factories.ProjectFactory(
            settings=self.fixture.settings, cluster=self.fixture.cluster
        )
        template = factories.TemplateFactory(
            settings=self.fixture.settings, catalog=catalog
        )
        namespace = factories.NamespaceFactory(
            settings=self.fixture.settings, project=project
        )

        return self.client.post(
            "/api/rancher-apps/",
            {
                "service_settings": factories.RancherServiceSettingsFactory.get_url(
                    self.fixture.settings
                ),
                "project": ProjectFactory.get_url(self.fixture.project),
                "name": "Test Catalog",
                "template": factories.TemplateFactory.get_url(template),
                "rancher_project": factories.ProjectFactory.get_url(project),
                **(
                    {"namespace_name": namespace_name}
                    if namespace_name
                    else {"namespace": factories.NamespaceFactory.get_url(namespace)}
                ),
                "version": "1.0",
                "answers": {},
            },
        )


@ddt
class ApplicationTargetProjectTest(test.APITestCase):
    """An application runs in its Rancher project's cluster, so the caller must
    be able to change that cluster, not only the Waldur project it names."""

    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        settings = self.fixture.settings
        self.template = factories.TemplateFactory(
            settings=settings, catalog=factories.CatalogFactory(settings=settings)
        )
        self.own_project = factories.ProjectFactory(
            settings=settings, cluster=self.fixture.cluster
        )
        self.own_namespace = factories.NamespaceFactory(
            settings=settings, project=self.own_project
        )

        # Another organization's cluster on the same Rancher server.
        other = fixtures.RancherFixture()
        other.cluster.settings = settings
        other.cluster.save()
        self.other = other
        self.other_project = factories.ProjectFactory(
            settings=settings, cluster=other.cluster
        )
        self.other_namespace = factories.NamespaceFactory(
            settings=settings, project=self.other_project
        )

    def payload(self, rancher_project, **namespace):
        return {
            "service_settings": factories.RancherServiceSettingsFactory.get_url(
                self.fixture.settings
            ),
            "project": ProjectFactory.get_url(self.fixture.project),
            "name": "app",
            "template": factories.TemplateFactory.get_url(self.template),
            "rancher_project": factories.ProjectFactory.get_url(rancher_project),
            "version": "1.0",
            "answers": {},
            **namespace,
        }

    def elsewhere(self):
        return (
            {"namespace": factories.NamespaceFactory.get_url(self.other_namespace)},
            {"namespace_name": "planted"},
        )

    @data("owner", "admin", "manager")
    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_cannot_create_in_another_organizations_cluster(self, user, client):
        self.client.force_authenticate(getattr(self.fixture, user))
        for namespace in self.elsewhere():
            response = self.client.post(
                "/api/rancher-apps/",
                self.payload(self.other_project, **namespace),
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.Application.objects.exists())
        self.assertFalse(models.Namespace.objects.filter(name="planted").exists())
        client.create_application.assert_not_called()

    def test_cannot_create_in_a_rancher_project_outside_any_cluster(self):
        detached = factories.ProjectFactory(settings=self.fixture.settings)
        namespace = factories.NamespaceFactory(
            settings=self.fixture.settings, project=detached
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            "/api/rancher-apps/",
            self.payload(
                detached, namespace=factories.NamespaceFactory.get_url(namespace)
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.Application.objects.exists())

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_staff_can_create_in_any_cluster(self, client):
        client.create_application.return_value = {"data": {}}
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            "/api/rancher-apps/",
            self.payload(
                self.other_project,
                namespace=factories.NamespaceFactory.get_url(self.other_namespace),
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def own_application_url(self):
        application = models.Application.objects.create(
            name="app",
            settings=self.fixture.settings,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            cluster=self.fixture.cluster,
            rancher_project=self.own_project,
            namespace=self.own_namespace,
            template=self.template,
            version="1.0",
            state=CoreStates.OK,
        )
        self.application = application
        return reverse("rancher-app-detail", kwargs={"uuid": application.uuid.hex})

    # The view has no update executor; updates only change the row.
    @mock.patch.object(views.ApplicationViewSet, "update_executor", create=True)
    def test_update_cannot_move_an_application(self, executor):
        url = self.own_application_url()
        # The owner is only a member of the other organization's project.
        add_user_to_project(self.fixture.owner, self.other.project, ProjectRole.MEMBER)
        self.client.force_authenticate(self.fixture.owner)
        other_template = factories.TemplateFactory(
            settings=self.fixture.settings,
            catalog=factories.CatalogFactory(settings=self.fixture.settings),
        )
        for method in (self.client.put, self.client.patch):
            for namespace in self.elsewhere():
                response = method(
                    url,
                    {
                        **self.payload(self.other_project, **namespace),
                        "project": ProjectFactory.get_url(self.other.project),
                        "template": factories.TemplateFactory.get_url(other_template),
                        "name": "renamed",
                    },
                    format="json",
                )
                self.assertEqual(
                    response.status_code, status.HTTP_200_OK, response.data
                )
                self.application.refresh_from_db()
                self.assertEqual(self.application.project, self.fixture.project)
                self.assertEqual(self.application.rancher_project, self.own_project)
                self.assertEqual(self.application.namespace, self.own_namespace)
                self.assertEqual(self.application.template, self.template)
                self.assertEqual(self.application.name, "renamed")
        self.assertFalse(models.Namespace.objects.filter(name="planted").exists())
