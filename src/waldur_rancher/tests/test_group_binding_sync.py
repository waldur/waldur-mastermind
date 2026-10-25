from unittest import mock

from django.test import TestCase

from waldur_core.core.enums import CoreStates
from waldur_rancher import enums, models, tasks

from . import factories

GROUP_PREFIX = "keycloakoidc_group://"


class FakeRancherBindings:
    """Group role bindings of one Rancher server, as its v3 API reports them."""

    def __init__(self):
        self.bindings = []

    def _find(self, scope_id, group_id, role):
        return [
            {"name": name, "groupPrincipalId": group, "roleTemplateId": role_id}
            for scope, group, role_id, name in self.bindings
            if scope == scope_id
            and group_id in (None, group)
            and role in (None, role_id)
        ]

    def _create(self, group_id, scope_id, role):
        self.bindings.append(
            (scope_id, group_id, role, f"binding-{len(self.bindings)}")
        )

    def _delete(self, scope_id, binding_name):
        self.bindings = [
            binding
            for binding in self.bindings
            if (binding[0], binding[3]) != (scope_id, binding_name)
        ]

    def get_cluster_group_role(self, group_id=None, cluster_id=None, role=None):
        return self._find(cluster_id, group_id, role)

    def get_project_group_role(self, group_id=None, project_id=None, role=None):
        return self._find(project_id, group_id, role)

    def create_cluster_group_role(self, group_id, cluster_id, role):
        self._create(group_id, cluster_id, role)

    def create_project_group_role(self, group_id, project_id, role):
        self._create(group_id, project_id, role)

    def delete_cluster_group_role(self, cluster_id, binding_name):
        self._delete(cluster_id, binding_name)

    def delete_project_group_role(self, project_id, binding_name):
        self._delete(project_id, binding_name)

    def groups_bound_to(self, scope_id):
        return {group for scope, group, _, _ in self.bindings if scope == scope_id}


@mock.patch("waldur_rancher.backend.RancherBackend.client")
class GroupBindingSyncTest(TestCase):
    def setUp(self):
        self.settings = factories.RancherServiceSettingsFactory()
        self.cluster_a = self.make_cluster("cluster-a")
        self.cluster_b = self.make_cluster("cluster-b")
        self.project_a = factories.ProjectFactory(
            settings=self.settings, cluster=self.cluster_a, backend_id="project-a"
        )
        self.project_b = factories.ProjectFactory(
            settings=self.settings, cluster=self.cluster_a, backend_id="project-b"
        )
        self.cluster_role = factories.RoleTemplateFactory(
            settings=self.settings,
            scope_type=enums.RoleScopeType.CLUSTER,
            name="cluster-member",
        )
        self.project_role = factories.RoleTemplateFactory(
            settings=self.settings,
            scope_type=enums.RoleScopeType.PROJECT,
            name="project-member",
        )
        self.rancher = FakeRancherBindings()

    def make_cluster(self, backend_id):
        return factories.ClusterFactory(
            settings=self.settings,
            service_settings=self.settings,
            state=CoreStates.OK,
            backend_id=backend_id,
        )

    def use_fake_rancher(self, mock_client):
        for name in (
            "get_cluster_group_role",
            "get_project_group_role",
            "create_cluster_group_role",
            "create_project_group_role",
            "delete_cluster_group_role",
            "delete_project_group_role",
        ):
            getattr(mock_client, name).side_effect = getattr(self.rancher, name)

    def test_cluster_group_is_bound_only_to_its_own_cluster(self, mock_client):
        self.use_fake_rancher(mock_client)
        models.KeycloakGroup.objects.create(
            name="group-a", scope_uuid=self.cluster_a.uuid, role=self.cluster_role
        )

        tasks.sync_rancher_group_bindings()

        self.assertEqual(
            self.rancher.groups_bound_to("cluster-a"), {GROUP_PREFIX + "group-a"}
        )
        self.assertEqual(self.rancher.groups_bound_to("cluster-b"), set())

    def test_binding_of_another_clusters_group_is_revoked(self, mock_client):
        self.use_fake_rancher(mock_client)
        models.KeycloakGroup.objects.create(
            name="group-a", scope_uuid=self.cluster_a.uuid, role=self.cluster_role
        )
        self.rancher.bindings.append(
            ("cluster-b", GROUP_PREFIX + "group-a", "cluster-member", "leftover")
        )

        tasks.sync_rancher_group_bindings()

        self.assertEqual(self.rancher.groups_bound_to("cluster-b"), set())

    def test_project_group_is_bound_only_to_its_own_project(self, mock_client):
        self.use_fake_rancher(mock_client)
        models.KeycloakGroup.objects.create(
            name="group-p", scope_uuid=self.project_a.uuid, role=self.project_role
        )

        tasks.sync_rancher_group_bindings()

        self.assertEqual(
            self.rancher.groups_bound_to("project-a"), {GROUP_PREFIX + "group-p"}
        )
        self.assertEqual(self.rancher.groups_bound_to("project-b"), set())

    def test_binding_of_another_projects_group_is_revoked(self, mock_client):
        self.use_fake_rancher(mock_client)
        models.KeycloakGroup.objects.create(
            name="group-p", scope_uuid=self.project_a.uuid, role=self.project_role
        )
        self.rancher.bindings.append(
            ("project-b", GROUP_PREFIX + "group-p", "project-member", "leftover")
        )

        tasks.sync_rancher_group_bindings()

        self.assertEqual(self.rancher.groups_bound_to("project-b"), set())
