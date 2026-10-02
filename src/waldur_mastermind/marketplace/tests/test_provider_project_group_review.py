"""Provider project groups: allocation safety, deletion, import and rendering."""

import json
import os
import tempfile
from unittest import mock

from django.core.management import call_command
from django.db import transaction
from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.reverse import reverse

from waldur_core.permissions.fixtures import OfferingRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, posix_ids, project_groups, utils
from waldur_mastermind.marketplace.enums import OfferingStates, OfferingUserStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_provider_project_groups import (
    LIST_URL,
    ProjectGroupsTestCase,
)


class RecyclingTest(ProjectGroupsTestCase):
    def release(self, gid, recyclable, **principal):
        return models.PosixIdentity.objects.create(
            pool=self.pool,
            gid=gid,
            released_at="2026-01-01T00:00:00Z",
            recyclable=recyclable,
            **principal,
        )

    def test_a_withheld_release_keeps_a_value_out_of_recycling(self):
        departed = structure_factories.UserFactory()
        self.release(30005, True, user=departed, uid=None)
        # Later the same value was moved off and withheld (override, re-point).
        self.release(30005, False, user=structure_factories.UserFactory(), uid=None)
        value, from_counter = posix_ids._candidate_value(self.pool, posix_ids.GID)
        self.assertEqual((value, from_counter), (30000, True))

    def test_recyclable_release_alone_is_still_recycled_for_users(self):
        self.release(30005, True, user=structure_factories.UserFactory(), uid=None)
        self.assertEqual(
            posix_ids._candidate_value(self.pool, posix_ids.GID), (30005, False)
        )

    def test_project_groups_never_take_a_departed_users_gid(self):
        self.pool.min_group_gid = self.pool.max_group_gid = None
        self.pool.next_group_gid = None
        self.pool.save()
        self.release(30005, True, user=structure_factories.UserFactory(), uid=None)
        self.make_resource()
        self.assertEqual(self.group_of().gid, 30000)

    def test_override_after_recycling_never_reissues_the_old_gid(self):
        self.pool.min_group_gid = self.pool.max_group_gid = None
        self.pool.next_group_gid = None
        self.pool.save()
        self.make_resource()
        group = self.group_of()
        self.set_gid(group, gid=30100)
        self.assertEqual(
            posix_ids._candidate_value(self.pool, posix_ids.GID), (30001, True)
        )
        other = structure_factories.ProjectFactory()
        self.make_resource(project=other)
        self.assertNotEqual(self.group_of(other).gid, 30000)


class ProviderDeletionTest(ProjectGroupsTestCase):
    def test_deleting_the_provider_takes_pool_and_groups(self):
        for _ in range(2):
            self.make_resource(project=structure_factories.ProjectFactory())
        self.assertEqual(models.ServiceProviderProjectGroup.objects.count(), 2)
        self.provider.delete()
        self.assertFalse(models.PosixIdPool.objects.filter(pk=self.pool.pk).exists())
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())

    def test_deleting_the_pool_alone_is_one_conflict(self):
        for _ in range(2):
            self.make_resource(project=structure_factories.ProjectFactory())
        with self.assertRaises(ProtectedError) as caught, transaction.atomic():
            self.pool.delete()
        self.assertEqual(len(caught.exception.protected_objects), 1)
        with self.assertRaises(ProtectedError), transaction.atomic():
            models.PosixIdPool.objects.filter(pk=self.pool.pk).delete()


class PoolValidationTest(ProjectGroupsTestCase):
    def test_range_holding_another_pools_active_value_is_refused(self):
        offering_pool = factories.PosixIdPoolFactory(
            offering=self.other_offering,
            min_uid=None,
            max_uid=None,
            next_uid=None,
            min_gid=40000,
            max_gid=40999,
            next_gid=40000,
        )
        models.PosixIdentity.objects.create(
            pool=offering_pool, user=structure_factories.UserFactory(), gid=50005
        )
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            factories.PosixIdPoolFactory.get_url(self.pool),
            {"max_group_gid": 20200, "min_gid": 50000, "max_gid": 50999},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("50005", str(response.data))

    def test_offering_pool_cannot_have_a_group_range(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            reverse("marketplace-posix-id-pool-list"),
            {
                "offering": self.offering.uuid.hex,
                "min_gid": 60000,
                "max_gid": 60999,
                "min_group_gid": 61000,
                "max_group_gid": 61999,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PinningEdgeTest(ProjectGroupsTestCase):
    def test_import_rejects_duplicate_projects_and_gids(self):
        project = structure_factories.ProjectFactory()
        other = structure_factories.ProjectFactory()
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            LIST_URL + "import_groups/",
            {
                "service_provider": self.provider.uuid.hex,
                "groups": [
                    {"project": project.uuid.hex, "gid": 20001},
                    {"project": project.uuid.hex, "gid": 20002},
                    {"project": other.uuid.hex, "gid": 20001},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(sorted(response.data["groups"]), [1, 2])
        self.assertIn("project", response.data["groups"][1])
        self.assertIn("gid", response.data["groups"][2])

    def test_set_gid_without_a_ledger_row_releases_the_old_gid(self):
        group = models.ServiceProviderProjectGroup.objects.create(
            service_provider=self.provider,
            project=self.project,
            name="legacy",
            gid=20007,
        )
        response = self.set_gid(group, gid=20008)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        released = models.PosixIdentity.objects.get(pool=self.pool, gid=20007)
        self.assertIsNotNone(released.released_at)
        self.assertFalse(released.recyclable)
        event = models.PosixIdentity.objects.filter(
            pool=self.pool, gid=20008, released_at__isnull=True
        )
        self.assertTrue(event.exists())

    def test_allocation_does_not_overwrite_a_concurrent_pin(self):
        group, _ = project_groups.get_or_create_group(self.provider, self.project)
        models.ServiceProviderProjectGroup.objects.filter(pk=group.pk).update(gid=20150)
        self.assertEqual(project_groups.allocate_gid(group), 20150)

    def test_callback_failures_are_logged_not_raised(self):
        with mock.patch.object(
            project_groups, "ensure_group", side_effect=RuntimeError("boom")
        ):
            resource = self.make_resource()
        self.assertEqual(resource.state, resource.States.CREATING)


class VisibilityEdgeTest(ProjectGroupsTestCase):
    def test_manager_of_an_archived_offering_sees_nothing(self):
        self.make_resource()
        archived = self.make_offering(state=OfferingStates.ARCHIVED)
        manager = structure_factories.UserFactory()
        archived.add_user(manager, OfferingRole.MANAGER)
        self.assertEqual(self.list_groups(user=manager), [])

    def test_huge_gid_filter_values_are_rejected_or_ignored(self):
        self.make_resource()
        self.client.force_authenticate(self.staff)
        response = self.client.get(LIST_URL, {"gid": "99999999999999999999999"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.get(LIST_URL, {"query": "99999999999999999999999"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_adoptable_projects_with_a_bad_uuid(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(
            LIST_URL + "adoptable_projects/", {"service_provider_uuid": "nope"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class GlauthEdgeTest(ProjectGroupsTestCase):
    def add_account(self, username, **kwargs):
        user = structure_factories.UserFactory()
        self.project.add_user(user, ProjectRole.MEMBER)
        factories.OfferingUserFactory(
            offering=self.offering,
            user=user,
            username=username,
            state=OfferingUserStates.OK,
            backend_metadata={
                "uidnumber": 100001,
                "primarygroup": 100001,
                "loginShell": "/bin/bash",
                "homeDir": f"/home/{username}",
            },
            **kwargs,
        )
        return user

    def provider_groups(self):
        tree = utils.build_glauth_tree(self.offering)
        return [g for g in tree["groups"] if g["kind"] == "provider_project"]

    def test_restricted_and_inactive_members_are_left_out(self):
        self.make_resource()
        self.add_account("bob")
        self.add_account("carol", is_restricted=True)
        inactive = self.add_account("dave")
        inactive.is_active = False
        inactive.save()
        [group] = self.provider_groups()
        self.assertEqual(group["members"], ["bob"])

    def test_name_clash_with_a_personal_group_is_skipped(self):
        self.make_resource()
        self.add_account(self.project.slug)
        self.assertEqual(self.provider_groups(), [])


class StructureRoundTripTest(ProjectGroupsTestCase):
    def test_pools_and_groups_survive_export_and_import(self):
        self.make_resource()
        self.adopt(project=structure_factories.ProjectFactory().uuid.hex, gid=20050)
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        try:
            call_command("export_structure", "-o", path)
            with open(path) as f:
                data = json.load(f)
            [pool] = [
                p for p in data["posix_id_pools"] if p["uuid"] == self.pool.uuid.hex
            ]
            self.assertEqual(
                (pool["min_group_gid"], pool["max_group_gid"]), (20001, 20200)
            )
            exported = {g["gid"] for g in data["service_provider_project_groups"]}
            self.assertEqual(exported, {20001, 20050})

            models.ServiceProviderProjectGroup.objects.all().delete()
            models.PosixIdentity.objects.all().delete()
            minimal = {
                "posix_id_pools": data["posix_id_pools"],
                "service_provider_project_groups": data[
                    "service_provider_project_groups"
                ],
            }
            with open(path, "w") as f:
                json.dump(minimal, f)
            call_command("import_structure", "-i", path)
        finally:
            os.remove(path)
        self.assertEqual(
            sorted(
                models.ServiceProviderProjectGroup.objects.values_list("gid", flat=True)
            ),
            [20001, 20050],
        )
        self.assertEqual(
            sorted(
                models.PosixIdentity.objects.filter(
                    pool=self.pool, released_at__isnull=True
                ).values_list("gid", flat=True)
            ),
            [20001, 20050],
        )
