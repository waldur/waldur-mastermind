from unittest import mock

import python_freeipa.exceptions
from constance.test.unittest import override_config
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_freeipa import models
from waldur_freeipa.backend import FreeIPABackend
from waldur_freeipa.utils import QUOTA_NAME


@mock.patch("python_freeipa.Client")
class GroupTest(TestCase):
    def test_missing_group_for_customer_is_created(self, mock_client):
        customer = structure_factories.CustomerFactory(name="customer")
        mock_client().group_find.return_value = {"result": []}
        FreeIPABackend().synchronize_groups()
        mock_client().group_add.assert_called_once_with(
            "waldur_org_%s" % customer.uuid.hex,
            description="customer,-1.0",
        )

    def test_customer_quota_is_serialized_as_group_description(self, mock_client):
        customer = structure_factories.CustomerFactory(name="customer")
        customer.set_quota_limit(QUOTA_NAME, 100)
        mock_client().group_find.return_value = {"result": []}
        FreeIPABackend().synchronize_groups()
        mock_client().group_add.assert_has_calls(
            [
                mock.call(
                    "waldur_org_%s" % customer.uuid.hex,
                    description="customer,100",
                )
            ],
            any_order=True,
        )

    def test_missing_group_for_project_is_created(self, mock_client):
        project = structure_factories.ProjectFactory(name="project")
        mock_client().group_find.return_value = {"result": []}
        FreeIPABackend().synchronize_groups()
        mock_client().group_add.assert_has_calls(
            [
                mock.call(
                    "waldur_project_%s" % project.uuid.hex,
                    description="project,-1.0",
                )
            ],
            any_order=True,
        )

    def test_project_quota_is_serialized_as_group_description(self, mock_client):
        project = structure_factories.ProjectFactory(name="project")
        project.set_quota_limit(QUOTA_NAME, 100)
        mock_client().group_find.return_value = {"result": []}
        FreeIPABackend().synchronize_groups()
        mock_client().group_add.assert_has_calls(
            [
                mock.call(
                    "waldur_project_%s" % project.uuid.hex,
                    description="project,100",
                )
            ],
            any_order=True,
        )

    def test_group_description_is_updated_from_customer_name(self, mock_client):
        customer = structure_factories.CustomerFactory(name="customer")
        mock_client().group_find.return_value = {
            "result": [
                {
                    "cn": ["waldur_org_%s" % customer.uuid],
                    "description": ["New customer name"],
                }
            ]
        }
        FreeIPABackend().synchronize_groups()
        mock_client().group_mod.assert_called_once_with(
            "waldur_org_%s" % customer.uuid.hex,
            description="customer,-1.0",
        )

    def test_group_description_is_updated_from_project_name(self, mock_client):
        project = structure_factories.ProjectFactory(name="project")
        mock_client().group_find.return_value = {
            "result": [
                {
                    "cn": ["waldur_project_%s" % project.uuid],
                    "description": ["New project name"],
                }
            ]
        }
        FreeIPABackend().synchronize_groups()
        mock_client().group_mod.assert_called_once_with(
            "waldur_project_%s" % project.uuid.hex,
            description="project,-1.0",
        )

    def test_missing_users_are_added_to_customer_group(self, mock_client):
        fixture = structure_fixtures.CustomerFixture()
        customer = fixture.customer
        owner = fixture.owner
        models.Profile.objects.create(user=owner, username=owner.username)
        mock_client().group_find.return_value = {"result": []}
        FreeIPABackend().synchronize_groups()
        mock_client().group_add_member.assert_called_once_with(
            "waldur_org_%s" % customer.uuid.hex,
            users=[owner.username],
            skip_errors=True,
        )

    def test_stale_users_are_removed_from_customer_group(self, mock_client):
        customer = structure_factories.CustomerFactory()
        mock_client().group_find.return_value = {
            "result": [
                {
                    "cn": ["waldur_org_%s" % customer.uuid],
                    "member_user": ["waldur_stale_user"],
                }
            ]
        }
        FreeIPABackend().synchronize_groups()
        mock_client().group_remove_member.assert_called_once_with(
            "waldur_org_%s" % customer.uuid.hex,
            users=["waldur_stale_user"],
            skip_errors=True,
        )

    def test_stale_groups_are_removed(self, mock_client):
        mock_client().group_find.return_value = {
            "result": [
                {
                    "cn": ["waldur_stale_customer"],
                }
            ]
        }
        FreeIPABackend().synchronize_groups()
        mock_client().group_del.assert_called_once_with("waldur_stale_customer")

    def test_stale_group_cleanup_continues_if_group_is_already_gone(self, mock_client):
        mock_client().group_find.return_value = {
            "result": [
                {"cn": ["waldur_stale_customer_1"]},
                {"cn": ["waldur_stale_customer_2"]},
            ]
        }
        mock_client()._request.side_effect = python_freeipa.exceptions.NotFound(
            message="group not found"
        )
        FreeIPABackend().synchronize_groups()

        # Missing group does not interrupt processing of the remaining ones.
        self.assertEqual(mock_client()._request.call_count, 2)
        mock_client().group_del.assert_not_called()

    def test_missing_children_are_added_to_customer_group(self, mock_client):
        fixture = structure_fixtures.ProjectFixture()
        customer = fixture.customer
        project = fixture.project
        mock_client().group_find.return_value = {"result": []}
        FreeIPABackend().synchronize_groups()
        mock_client().group_add_member.assert_called_once_with(
            "waldur_org_%s" % customer.uuid.hex,
            groups=["waldur_project_%s" % project.uuid],
            skip_errors=True,
        )

    def test_stale_children_are_removed_from_customer_group(self, mock_client):
        customer = structure_factories.CustomerFactory()
        mock_client().group_find.return_value = {
            "result": [
                {
                    "cn": ["waldur_org_%s" % customer.uuid],
                    "member_group": ["waldur_stale_child"],
                }
            ]
        }
        FreeIPABackend().synchronize_groups()
        mock_client().group_remove_member.assert_called_once_with(
            "waldur_org_%s" % customer.uuid.hex,
            groups=["waldur_stale_child"],
            skip_errors=True,
        )


@mock.patch("python_freeipa.Client")
class EmptyPrefixTest(TestCase):
    """
    An empty prefix matches every entity in the directory, so it would mark
    all the pre-existing groups as stale and delete them.
    """

    directory_groups = {
        "result": [
            {"cn": ["admins"], "member_user": ["admin"]},
            {"cn": ["ipausers"], "member_user": ["alice"]},
            {"cn": ["hpc_staff"], "member_user": ["bob"]},
        ]
    }

    def assert_directory_is_intact(self, mock_client):
        mock_client().group_del.assert_not_called()
        mock_client()._request.assert_not_called()
        mock_client().group_add.assert_not_called()
        mock_client().group_mod.assert_not_called()
        mock_client().group_add_member.assert_not_called()
        mock_client().group_remove_member.assert_not_called()

    @override_config(FREEIPA_GROUPNAME_PREFIX="")
    def test_groups_are_not_deleted_if_group_prefix_is_empty(self, mock_client):
        structure_factories.CustomerFactory(name="customer")
        mock_client().group_find.return_value = self.directory_groups

        FreeIPABackend().synchronize_groups()

        self.assert_directory_is_intact(mock_client)

    @override_config(FREEIPA_USERNAME_PREFIX="")
    def test_groups_are_not_deleted_if_username_prefix_is_empty(self, mock_client):
        structure_factories.CustomerFactory(name="customer")
        mock_client().group_find.return_value = self.directory_groups

        FreeIPABackend().synchronize_groups()

        self.assert_directory_is_intact(mock_client)

    def test_groups_are_deleted_if_prefixes_are_set(self, mock_client):
        mock_client().group_find.return_value = {
            "result": [
                {"cn": ["admins"]},
                {"cn": ["waldur_stale_customer"]},
            ]
        }

        FreeIPABackend().synchronize_groups()

        mock_client().group_del.assert_called_once_with("waldur_stale_customer")
