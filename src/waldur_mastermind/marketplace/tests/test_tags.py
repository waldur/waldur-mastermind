from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import OfferingStates

from . import factories, fixtures


@ddt
class TagListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.tag = factories.TagFactory()
        self.list_url = factories.TagFactory.get_list_url()

    @data("staff", "owner", "admin", "manager")
    def test_tag_should_be_visible_to_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)


@ddt
class TagPermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.other_fixture = fixtures.MarketplaceFixture()

        # Create tag by first service provider
        self.tag = factories.TagFactory(
            name="provider1-tag",
            created_by=self.fixture.offering_owner,
        )

    def test_staff_can_create_tag(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.TagFactory.get_list_url(), {"name": "staff-tag"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_service_provider_can_create_tag(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            factories.TagFactory.get_list_url(), {"name": "sp-tag"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        tag = models.Tag.objects.get(name="sp-tag")
        self.assertEqual(tag.created_by, self.fixture.offering_owner)

    def test_regular_user_cannot_create_tag(self):
        regular_user = structure_factories.UserFactory()
        self.client.force_authenticate(regular_user)
        response = self.client.post(
            factories.TagFactory.get_list_url(), {"name": "user-tag"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_update_any_tag(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.TagFactory.get_url(self.tag), {"name": "updated-by-staff"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_can_update_own_tag(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.patch(
            factories.TagFactory.get_url(self.tag), {"name": "updated-by-owner"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_other_provider_cannot_update_tag(self):
        self.client.force_authenticate(self.other_fixture.offering_owner)
        response = self.client.patch(
            factories.TagFactory.get_url(self.tag), {"name": "hijacked"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_delete_any_tag(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(factories.TagFactory.get_url(self.tag))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_owner_can_delete_own_tag(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.delete(factories.TagFactory.get_url(self.tag))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_other_provider_cannot_delete_tag(self):
        self.client.force_authenticate(self.other_fixture.offering_owner)
        response = self.client.delete(factories.TagFactory.get_url(self.tag))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class TagOfferingCountTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.other_fixture = fixtures.MarketplaceFixture()

        self.tag = factories.TagFactory()

        # Own offerings (all states should be counted for owner)
        self.own_draft = factories.OfferingFactory(
            customer=self.fixture.offering_customer,
            state=OfferingStates.DRAFT,
        )
        self.own_draft.tags.add(self.tag)

        self.own_active = factories.OfferingFactory(
            customer=self.fixture.offering_customer,
            state=OfferingStates.ACTIVE,
        )
        self.own_active.tags.add(self.tag)

        # Other provider's offerings
        self.other_draft = factories.OfferingFactory(
            customer=self.other_fixture.offering_customer,
            state=OfferingStates.DRAFT,  # Should NOT be counted for non-staff
        )
        self.other_draft.tags.add(self.tag)

        self.other_active = factories.OfferingFactory(
            customer=self.other_fixture.offering_customer,
            state=OfferingStates.ACTIVE,  # Should be counted
        )
        self.other_active.tags.add(self.tag)

        self.other_paused = factories.OfferingFactory(
            customer=self.other_fixture.offering_customer,
            state=OfferingStates.PAUSED,  # Should be counted
        )
        self.other_paused.tags.add(self.tag)

    def test_staff_sees_all_offerings_in_count(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.TagFactory.get_url(self.tag))
        self.assertEqual(response.data["offering_count"], 5)  # All 5 offerings

    def test_service_provider_sees_filtered_count(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(factories.TagFactory.get_url(self.tag))
        # Own: draft + active = 2
        # Other: active + paused = 2 (draft excluded)
        self.assertEqual(response.data["offering_count"], 4)


class OfferingTagFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.tag_hpc = factories.TagFactory(name="hpc")
        self.tag_gpu = factories.TagFactory(name="gpu")

        self.offering_hpc = factories.OfferingFactory(
            shared=True, state=OfferingStates.ACTIVE
        )
        self.offering_hpc.tags.add(self.tag_hpc)

        self.offering_gpu = factories.OfferingFactory(
            shared=True, state=OfferingStates.ACTIVE
        )
        self.offering_gpu.tags.add(self.tag_gpu)

        self.offering_both = factories.OfferingFactory(
            shared=True, state=OfferingStates.ACTIVE
        )
        self.offering_both.tags.add(self.tag_hpc)
        self.offering_both.tags.add(self.tag_gpu)

    def test_filter_by_tag_uuid(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url, {"tag": self.tag_hpc.uuid.hex})
        self.assertEqual(len(response.data), 2)  # offering_hpc and offering_both
        uuids = [o["uuid"] for o in response.data]
        self.assertIn(self.offering_hpc.uuid.hex, uuids)
        self.assertIn(self.offering_both.uuid.hex, uuids)

    def test_filter_by_tag_name(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url, {"tag_name": "hpc"})
        self.assertEqual(len(response.data), 2)  # offering_hpc and offering_both

    def test_filter_by_multiple_tag_uuids_or_logic(self):
        """Multiple tag UUIDs use OR logic - returns offerings with ANY of the tags."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(
            url, {"tag": [self.tag_hpc.uuid.hex, self.tag_gpu.uuid.hex]}
        )
        self.assertEqual(len(response.data), 3)  # All 3 offerings

    def test_filter_by_multiple_tag_names_or_logic(self):
        """Multiple tag names use OR logic - returns offerings with ANY of the tags."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url, {"tag_name": ["hpc", "gpu"]})
        self.assertEqual(len(response.data), 3)  # All 3 offerings

    def test_filter_by_tag_uuids_and_logic(self):
        """tags_and filter uses AND logic - returns offerings with ALL specified tags."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(
            url, {"tags_and": f"{self.tag_hpc.uuid.hex},{self.tag_gpu.uuid.hex}"}
        )
        self.assertEqual(len(response.data), 1)  # Only offering_both has both tags
        self.assertEqual(response.data[0]["uuid"], self.offering_both.uuid.hex)

    def test_filter_by_tag_names_and_logic(self):
        """tag_names_and filter uses AND logic - returns offerings with ALL specified tags."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url, {"tag_names_and": "hpc,gpu"})
        self.assertEqual(len(response.data), 1)  # Only offering_both has both tags
        self.assertEqual(response.data[0]["uuid"], self.offering_both.uuid.hex)

    def test_filter_by_single_tag_uuid_and_logic(self):
        """tags_and with single UUID works like regular tag filter."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url, {"tags_and": self.tag_hpc.uuid.hex})
        self.assertEqual(len(response.data), 2)  # offering_hpc and offering_both

    def test_filter_by_nonexistent_tag_and_logic(self):
        """tags_and with non-matching tags returns empty."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()
        # Create a third tag that no offering has
        tag_storage = factories.TagFactory(name="storage")
        response = self.client.get(
            url, {"tags_and": f"{self.tag_hpc.uuid.hex},{tag_storage.uuid.hex}"}
        )
        self.assertEqual(len(response.data), 0)  # No offering has both tags


class OfferingTagUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.tag1 = factories.TagFactory(name="tag1")
        self.tag2 = factories.TagFactory(name="tag2")

    def test_owner_can_update_offering_tags(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        url = factories.OfferingFactory.get_url(self.fixture.offering) + "update_tags/"
        response = self.client.post(
            url,
            {
                "tags": [
                    self.tag1.uuid.hex,
                    self.tag2.uuid.hex,
                ]
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.offering.refresh_from_db()
        self.assertEqual(self.fixture.offering.tags.count(), 2)

    def test_owner_can_delete_offering_tags(self):
        self.fixture.offering.tags.add(self.tag1, self.tag2)
        self.client.force_authenticate(self.fixture.offering_owner)
        url = factories.OfferingFactory.get_url(self.fixture.offering) + "delete_tags/"
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.fixture.offering.refresh_from_db()
        self.assertEqual(self.fixture.offering.tags.count(), 0)

    def test_tags_included_in_offering_response(self):
        self.fixture.offering.tags.add(self.tag1)
        self.client.force_authenticate(self.fixture.offering_owner)
        url = factories.OfferingFactory.get_url(self.fixture.offering)
        response = self.client.get(url)
        self.assertEqual(len(response.data["tags"]), 1)
        self.assertEqual(response.data["tags"][0]["name"], "tag1")
