import datetime
from unittest import mock

from django.utils import timezone
from django_fsm import TransitionNotAllowed
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.core import encryption
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace import models, serializers
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture

States = models.ResourceApiKey.States
PUBLISH = "waldur_mastermind.marketplace.utils.logging_tasks"
PREPARE = "waldur_mastermind.marketplace.utils.prepare_messages"
MESSAGES = [{"vhost": "v", "topic": "t", "payload": "{}"}]


def list_url(action=None):
    name = "marketplace-resource-api-key-list"
    if action:
        name = f"marketplace-resource-api-key-{action}"
    return reverse(name)


def detail_url(api_key, action=None):
    name = "marketplace-resource-api-key-detail"
    if action:
        name = f"marketplace-resource-api-key-{action}"
    return reverse(name, kwargs={"uuid": api_key.uuid.hex})


class ResourceApiKeyModelTest(test.APITestCase):
    def test_a_resource_owns_many_keys(self):
        resource = factories.ResourceFactory()
        models.ResourceApiKey.objects.create(resource=resource, client_id="cid-1")
        models.ResourceApiKey.objects.create(resource=resource, client_id="cid-2")
        self.assertEqual(resource.api_keys.count(), 2)

    def test_client_id_is_unique_per_resource(self):
        resource = factories.ResourceFactory()
        models.ResourceApiKey.objects.create(resource=resource, client_id="cid-1")
        with self.assertRaises(Exception):
            models.ResourceApiKey.objects.create(resource=resource, client_id="cid-1")

    def test_state_transitions(self):
        key = models.ResourceApiKey.objects.create(
            resource=factories.ResourceFactory(), client_id="cid-1"
        )
        self.assertEqual(key.state, States.CREATING)
        key.set_ok()
        self.assertEqual(key.state, States.OK)
        key.set_updating()
        self.assertEqual(key.state, States.UPDATING)
        key.set_ok()
        key.set_updating()
        key.set_erred()
        self.assertEqual(key.state, States.ERRED)

    def test_updating_not_allowed_from_creating(self):
        key = models.ResourceApiKey.objects.create(
            resource=factories.ResourceFactory(), client_id="cid-1"
        )
        with self.assertRaises(TransitionNotAllowed):
            key.set_updating()

    def test_erred_key_can_be_recovered(self):
        # A failed apply must not strand the key: rotate and the agent's set_ok are
        # both allowed from Erred so it can be retried from the portal.
        key = models.ResourceApiKey.objects.create(
            resource=factories.ResourceFactory(), client_id="cid-1"
        )
        key.set_erred()
        self.assertEqual(key.state, States.ERRED)

        key.set_updating()  # portal rotate retry
        self.assertEqual(key.state, States.UPDATING)
        key.set_ok()  # agent reports the new value
        self.assertEqual(key.state, States.OK)


class ConsumerApiKeyTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_RESOURCE_USERS)
        self.key = models.ResourceApiKey.objects.create(
            resource=self.resource,
            client_id="cid-1",
            key_ciphertext=encryption.encrypt_value("sk-secret-one"),
            state=States.OK,
        )

    def test_member_can_list_keys(self):
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(
            list_url(), {"resource_uuid": self.resource.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertNotIn("api_key", response.data[0])

    def test_outsider_sees_no_keys(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_member_can_reveal(self):
        self.client.force_authenticate(self.fixture.admin)
        with mock.patch(
            "waldur_mastermind.marketplace.views.log.log_resource_api_key_revealed"
        ) as log_reveal:
            response = self.client.get(detail_url(self.key, "reveal"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["api_key"], "sk-secret-one")
        # The audit event must identify which key was revealed, not just the
        # resource — a resource owns many keys.
        log_reveal.assert_called_once_with(self.key, self.fixture.admin)
        # The body carries a live secret; no cache may retain it.
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_undecryptable_key_is_a_clean_conflict(self):
        # A key stored under an encryption key that is no longer configured must
        # surface as a clear 409, not an opaque 500 — and must not be audit-logged
        # as revealed.
        self.key.key_ciphertext = "gAAAAnot-a-real-token"
        self.key.save()
        self.client.force_authenticate(self.fixture.admin)
        with mock.patch(
            "waldur_mastermind.marketplace.views.log.log_resource_api_key_revealed"
        ) as log_reveal:
            response = self.client.get(detail_url(self.key, "reveal"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("FIELD_ENCRYPTION_KEY", str(response.data))
        log_reveal.assert_not_called()

    def test_project_only_viewer_cannot_reveal(self):
        self.client.force_authenticate(self.fixture.admin)
        with mock.patch(
            "waldur_mastermind.marketplace.views.utils.is_resource_project_only_viewer",
            return_value=True,
        ):
            response = self.client.get(detail_url(self.key, "reveal"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_provider_user_cannot_reveal(self):
        # A provider-org member can reach the write actions (set_key etc.) but must
        # never read a consumer's live key. The shared queryset admits them; reveal
        # narrows back to consumer-side access.
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(detail_url(self.key, "reveal"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_reveal_only_from_ok(self):
        # A transitional key's stored value may not match the gateway; never hand
        # out a stale/dead value.
        self.key.set_updating()
        self.key.save()
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(detail_url(self.key, "reveal"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_owner_can_rotate(self):
        self.client.force_authenticate(self.fixture.owner)
        with mock.patch(PREPARE, return_value=MESSAGES), mock.patch(PUBLISH):
            response = self.client.post(detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.UPDATING)

    def test_rotate_only_from_ok(self):
        self.key.set_updating()
        self.key.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_manager_without_permission_cannot_rotate(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_revoke_is_not_offered(self):
        # The key count is fixed at provisioning; rotation re-mints in place, so
        # there is no consumer-facing way to remove a key.
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(f"{detail_url(self.key)}revoke/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_rotate_rejected_when_resource_terminating(self):
        # A key command must not race the resource's termination cleanup.
        self.resource.state = self.resource.States.TERMINATING
        self.resource.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.OK)


class ProviderApiKeyTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_RESOURCE_API_KEY)

    def test_agent_reports_a_created_key(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            list_url("report-created"),
            {
                "resource": self.resource.uuid.hex,
                "client_id": "cid-1",
                "api_key": "sk-fresh-key",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        key = models.ResourceApiKey.objects.get(resource=self.resource)
        self.assertEqual(key.state, States.OK)
        self.assertTrue(encryption.is_encrypted(key.key_ciphertext))
        self.assertEqual(encryption.decrypt_value(key.key_ciphertext), "sk-fresh-key")

    def test_two_keys_coexist(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        for cid in ("cid-1", "cid-2"):
            self.client.post(
                list_url("report-created"),
                {
                    "resource": self.resource.uuid.hex,
                    "client_id": cid,
                    "api_key": f"sk-{cid}",
                },
            )
        self.assertEqual(self.resource.api_keys.count(), 2)

    def test_consumer_cannot_report_created(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_RESOURCE_USERS)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            list_url("report-created"),
            {
                "resource": self.resource.uuid.hex,
                "client_id": "cid-1",
                "api_key": "sk-x",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_agent_reports_rotated_value(self):
        key = models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="cid-1", state=States.UPDATING
        )
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            detail_url(key, "set-key"), {"api_key": "sk-rotated"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        key.refresh_from_db()
        self.assertEqual(key.state, States.OK)
        self.assertEqual(encryption.decrypt_value(key.key_ciphertext), "sk-rotated")

    def test_rotation_can_move_the_client_id(self):
        # An S3 rotation mints a new access key, so the public identifier moves with
        # the secret; Envoy's stable slot omits client_id and is unaffected.
        key = models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="AKIAOLD", state=States.UPDATING
        )
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            detail_url(key, "set-key"),
            {"api_key": "s3secret", "client_id": "AKIANEW"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        key.refresh_from_db()
        self.assertEqual(key.client_id, "AKIANEW")
        self.assertEqual(key.state, States.OK)
        self.assertEqual(encryption.decrypt_value(key.key_ciphertext), "s3secret")

    def test_omitted_client_id_is_kept(self):
        key = models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="cid-1", state=States.UPDATING
        )
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(detail_url(key, "set-key"), {"api_key": "sk-new"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        key.refresh_from_db()
        self.assertEqual(key.client_id, "cid-1")

    def test_client_id_taken_by_a_sibling_is_rejected(self):
        key = models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="AKIAOLD", state=States.UPDATING
        )
        models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="AKIASIBLING", state=States.OK
        )
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            detail_url(key, "set-key"),
            {"api_key": "s3secret", "client_id": "AKIASIBLING"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        key.refresh_from_db()
        self.assertEqual(key.client_id, "AKIAOLD")
        self.assertEqual(key.state, States.UPDATING)

    def test_concurrent_client_id_claim_is_a_clean_validation_error(self):
        # The sibling-collision check runs outside the row lock, so a concurrent
        # writer can claim the client_id between check and save. Simulate that by
        # blinding the check: the unique-constraint violation must surface as the
        # same 400, not a 500.
        key = models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="AKIAOLD", state=States.UPDATING
        )
        models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="AKIANEW", state=States.OK
        )
        self.client.force_authenticate(self.fixture.offering_owner)
        with mock.patch(
            "waldur_mastermind.marketplace.views.models.ResourceApiKey.objects.filter"
        ) as filter_mock:
            filter_mock.return_value.exclude.return_value.exists.return_value = False
            response = self.client.post(
                detail_url(key, "set-key"),
                {"api_key": "s3secret", "client_id": "AKIANEW"},
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        key.refresh_from_db()
        self.assertEqual(key.client_id, "AKIAOLD")

    def test_agent_reports_erred(self):
        key = models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="cid-1", state=States.UPDATING
        )
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            detail_url(key, "set-erred"), {"error_message": "boom"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        key.refresh_from_db()
        self.assertEqual(key.state, States.ERRED)
        self.assertEqual(key.error_message, "boom")

    def test_destroy_is_not_offered(self):
        # Deleting a key row was only ever a revoke confirmation. With no revoke
        # there is nothing to confirm, and an ungated delete could drop a row whose
        # key still serves at the backend. Termination cleanup deletes rows directly.
        key = models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="cid-1", state=States.OK
        )
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.delete(detail_url(key))
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(models.ResourceApiKey.objects.filter(pk=key.pk).exists())

    def test_set_erred_rejected_from_ok(self):
        # A stale erred report must not clobber a key that a newer set_key landed OK.
        key = models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="cid-1", state=States.OK
        )
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            detail_url(key, "set-erred"), {"error_message": "late failure"}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        key.refresh_from_db()
        self.assertEqual(key.state, States.OK)

    def test_duplicate_report_created_upserts(self):
        # A retried or duplicated report must upsert on (resource, client_id), not
        # 500 on the unique constraint; the stored value reflects the last report.
        self.client.force_authenticate(self.fixture.offering_owner)
        for value in ("sk-first", "sk-second"):
            response = self.client.post(
                list_url("report-created"),
                {
                    "resource": self.resource.uuid.hex,
                    "client_id": "cid-1",
                    "api_key": value,
                },
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.resource.api_keys.count(), 1)
        key = self.resource.api_keys.get()
        self.assertEqual(encryption.decrypt_value(key.key_ciphertext), "sk-second")

    def test_report_created_rejected_for_dead_resource(self):
        # A late report against a terminating/terminated resource must not
        # recreate rows the termination cleanup deletes.
        for resource_state in (
            self.resource.States.TERMINATING,
            self.resource.States.TERMINATED,
        ):
            self.resource.state = resource_state
            self.resource.save()
            self.client.force_authenticate(self.fixture.offering_owner)
            response = self.client.post(
                list_url("report-created"),
                {
                    "resource": self.resource.uuid.hex,
                    "client_id": "cid-1",
                    "api_key": "sk-orphan",
                },
            )
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
            self.assertFalse(self.resource.api_keys.exists())


class ResourceHasApiKeysTest(test.APITestCase):
    """The portal decides to offer key management from the resource itself.

    Nothing in the resource payload identifies the backend — croit-s3 and the Envoy
    inference offering share an offering type — so the resource reports whether it
    owns keys.
    """

    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.client.force_authenticate(self.fixture.staff)

    def _get_resource(self):
        url = reverse(
            "marketplace-resource-detail", kwargs={"uuid": self.resource.uuid.hex}
        )
        return self.client.get(url)

    def test_false_when_the_resource_has_no_keys(self):
        response = self._get_resource()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["has_api_keys"])

    def test_true_when_the_resource_has_a_key(self):
        models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="AKIA1", state=States.OK
        )
        response = self._get_resource()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["has_api_keys"])

    def test_getter_falls_back_without_the_annotation(self):
        # The serializer is also instantiated on un-annotated instances (e.g.
        # marketplace_script tasks), where the queryset annotation is absent.
        models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="AKIA1", state=States.OK
        )
        resource = models.Resource.objects.get(pk=self.resource.pk)
        self.assertFalse(hasattr(resource, "has_api_keys_annotation"))
        self.assertTrue(serializers.ResourceSerializer().get_has_api_keys(resource))

    def test_provider_resource_list_is_annotated(self):
        # The provider viewset shares ResourceSerializer, so it needs the same
        # Exists annotation — otherwise every row in a provider's resource list
        # falls back to a per-instance api_keys.exists() query (N+1).
        from waldur_mastermind.marketplace import views

        models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="AKIA1", state=States.OK
        )
        view = views.ProviderResourceViewSet()
        view.request = mock.Mock(user=self.fixture.offering_owner)
        resource = view.get_queryset().get(pk=self.resource.pk)
        self.assertTrue(resource.has_api_keys_annotation)

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(reverse("marketplace-provider-resource-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_uuid = {row["uuid"]: row for row in response.data}
        self.assertTrue(by_uuid[self.resource.uuid.hex]["has_api_keys"])


class ResourceApiKeyFilterTest(test.APITestCase):
    """The agent's reconciliation pass finds stuck keys by listing, not by command."""

    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.stuck = models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="cid-1", state=States.UPDATING
        )
        self.settled = models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="cid-2", state=States.OK
        )
        self.client.force_authenticate(self.fixture.staff)

    def test_filter_by_state(self):
        response = self.client.get(list_url(), {"state": States.UPDATING})
        self.assertEqual([key["uuid"] for key in response.data], [self.stuck.uuid.hex])

    def test_filter_by_several_states(self):
        response = self.client.get(list_url(), {"state": [States.UPDATING, States.OK]})
        self.assertEqual(len(response.data), 2)

    def test_filter_by_offering(self):
        other = factories.ResourceFactory()
        models.ResourceApiKey.objects.create(
            resource=other, client_id="cid-3", state=States.UPDATING
        )
        response = self.client.get(
            list_url(), {"offering_uuid": self.resource.offering.uuid.hex}
        )
        self.assertEqual(
            {key["uuid"] for key in response.data},
            {self.stuck.uuid.hex, self.settled.uuid.hex},
        )

    def test_filter_by_modification_time(self):
        # The agent only wants keys stuck long enough to be a lost reply rather
        # than one still in flight.
        past = timezone.now() - datetime.timedelta(hours=1)
        models.ResourceApiKey.objects.filter(pk=self.stuck.pk).update(modified=past)
        cutoff = (timezone.now() - datetime.timedelta(minutes=30)).isoformat()

        response = self.client.get(list_url(), {"modified_before": cutoff})

        self.assertEqual([key["uuid"] for key in response.data], [self.stuck.uuid.hex])

    def test_status_carries_the_resource_backend_id(self):
        # A reconcile has no command carrying it, and rotate_resource_key needs it.
        self.resource.backend_id = "res-backend-1"
        self.resource.save()
        response = self.client.get(
            list_url(), {"resource_uuid": self.resource.uuid.hex}
        )
        self.assertEqual(
            {key["resource_backend_id"] for key in response.data}, {"res-backend-1"}
        )
