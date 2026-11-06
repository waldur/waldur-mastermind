from unittest import mock

from django.urls import reverse
from rest_framework import status, test

from waldur_core.logging import tasks
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

from . import factories

EVENTS_URL = "http://testserver" + reverse("event-list")
STATS_URL = "http://testserver" + reverse("events-stats-list")

ACTOR_DETAILS = {
    "ip_address": "198.51.100.7",
    "user_agent": "Mozilla/5.0 (X11; Linux x86_64)",
    "auth_method": "pat",
    "pat_uuid": "a" * 32,
    "pat_name": "ci token",
    "request_id": "3f1c0e5e-2a4f-4d0b-9a57-5b1f2b8c9d10",
    "user_impersonator_uuid": "b" * 32,
    "user_impersonator_full_name": "admin / Support Person",
    "user_impersonator_username": "admin",
}
ACTOR_IDENTITY = {
    "user_uuid": "c" * 32,
    "user_username": "manager",
    "user_full_name": "Call Manager",
}


PROBE = "probe_event"


def read(client, user, scope_url, event_type=PROBE, **params):
    client.force_authenticate(user)
    response = client.get(
        EVENTS_URL, {"scope": scope_url, "event_type": event_type, **params}
    )
    assert response.status_code == status.HTTP_200_OK, response.data
    return response.data


class ActorDetailsTest(test.APITestCase):
    """The actor's request details reach staff and support only."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer_url = structure_factories.CustomerFactory.get_url(
            self.fixture.customer
        )
        self.event = factories.EventFactory(
            event_type=PROBE,
            context={
                **ACTOR_DETAILS,
                **ACTOR_IDENTITY,
                "customer_name": "Acme",
            },
        )
        factories.FeedFactory(scope=self.fixture.customer, event=self.event)

    def test_ordinary_reader_gets_the_event_without_actor_details(self):
        [event] = read(self.client, self.fixture.owner, self.customer_url)

        for key in ACTOR_DETAILS:
            self.assertNotIn(key, event["context"])
        for key, value in ACTOR_IDENTITY.items():
            self.assertEqual(event["context"][key], value)
        self.assertEqual(event["context"]["customer_name"], "Acme")

    def test_staff_and_support_get_the_actor_details(self):
        for user in (
            self.fixture.staff,
            structure_factories.UserFactory(is_support=True),
        ):
            [event] = read(self.client, user, self.customer_url)
            for key, value in ACTOR_DETAILS.items():
                self.assertEqual(event["context"][key], value)

    def test_ordinary_reader_cannot_filter_by_how_the_actor_authenticated(self):
        for params in ({"auth_method": "pat"}, {"pat_uuid": "a" * 32}):
            self.assertEqual(
                read(self.client, self.fixture.owner, self.customer_url, **params),
                [],
            )
            self.assertEqual(
                len(read(self.client, self.fixture.staff, self.customer_url, **params)),
                1,
            )

    @mock.patch("waldur_core.logging.models.validate_outbound_url")
    @mock.patch("waldur_core.logging.models.requests.post")
    def test_web_hook_of_an_ordinary_owner_carries_no_actor_details(
        self, post, _validate
    ):
        hook = factories.WebHookFactory(
            user=self.fixture.owner, destination_url="http://example.com/"
        )

        hook.process(self.event)

        sent = post.call_args.kwargs["json"]["context"]
        for key in ACTOR_DETAILS:
            self.assertNotIn(key, sent)
        self.assertEqual(sent["user_username"], "manager")

    @mock.patch("waldur_core.logging.models.validate_outbound_url")
    @mock.patch("waldur_core.logging.models.requests.post")
    def test_web_hook_of_staff_carries_the_actor_details(self, post, _validate):
        hook = factories.WebHookFactory(
            user=self.fixture.staff, destination_url="http://example.com/"
        )

        hook.process(self.event)

        sent = post.call_args.kwargs["json"]["context"]
        self.assertEqual(sent["ip_address"], ACTOR_DETAILS["ip_address"])
        self.assertEqual(sent["user_agent"], ACTOR_DETAILS["user_agent"])


class UserFeedTest(test.APITestCase):
    """A person's own trail is read by that person, staff and support."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.member = self.fixture.admin
        self.member_url = structure_factories.UserFactory.get_url(self.member)
        self.login = factories.EventFactory(
            event_type=PROBE,
            context={**ACTOR_DETAILS, "user_uuid": self.member.uuid.hex},
        )
        factories.FeedFactory(scope=self.member, event=self.login)

    def count(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            EVENTS_URL + "count/", {"scope": self.member_url, "event_type": PROBE}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["count"]

    def stats(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            STATS_URL, {"scope": self.member_url, "event_type": PROBE}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return sum(row["count"] for row in response.data)

    def hook_matches(self, user):
        hook = factories.WebHookFactory(user=user, event_types=[PROBE])
        return tasks.check_event(self.login, hook)

    def test_people_who_see_the_user_do_not_read_their_trail(self):
        # Both can see the member in user lists: the owner through the
        # organization, the manager through the project.
        for reader in (self.fixture.owner, self.fixture.manager):
            self.client.force_authenticate(reader)
            self.assertEqual(
                self.client.get(self.member_url).status_code, status.HTTP_200_OK
            )

            self.assertEqual(read(self.client, reader, self.member_url), [])
            self.assertEqual(self.count(reader), 0)
            self.assertEqual(self.stats(reader), 0)
            self.assertFalse(self.hook_matches(reader))

    def test_the_user_staff_and_support_read_it(self):
        support = structure_factories.UserFactory(is_support=True)
        for reader in (self.member, self.fixture.staff, support):
            self.assertEqual(len(read(self.client, reader, self.member_url)), 1)
            self.assertEqual(self.count(reader), 1)
            self.assertEqual(self.stats(reader), 1)
            self.assertTrue(self.hook_matches(reader))

    def test_the_user_reads_their_own_trail_without_actor_details(self):
        [event] = read(self.client, self.member, self.member_url)

        for key in ACTOR_DETAILS:
            self.assertNotIn(key, event["context"])


class StaffActionOnOwnFeedTest(test.APITestCase):
    """A user reading their own trail does not learn where staff acted from."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.user = self.fixture.admin
        self.user_url = structure_factories.UserFactory.get_url(self.user)

    def change_password_as_staff(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            structure_factories.UserFactory.get_url(self.user, "change_password"),
            {"new_password": "password"},
            REMOTE_ADDR="203.0.113.9",
            HTTP_USER_AGENT="StaffBrowser/1.0",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def events(self, reader):
        return read(
            self.client, reader, self.user_url, "user_password_updated_by_staff"
        )

    def test_user_sees_the_event_without_the_staff_network_details(self):
        self.change_password_as_staff()

        [staff_view] = self.events(self.fixture.staff)
        self.assertEqual(staff_view["context"]["ip_address"], "203.0.113.9")
        self.assertEqual(staff_view["context"]["user_agent"], "StaffBrowser/1.0")

        [own_view] = self.events(self.user)
        self.assertNotIn("ip_address", own_view["context"])
        self.assertNotIn("user_agent", own_view["context"])
        self.assertEqual(own_view["context"]["affected_user_uuid"], self.user.uuid.hex)

    def test_user_created_by_staff_sees_the_event_without_staff_network_details(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)
        account = structure_factories.UserFactory.build()
        response = self.client.post(
            structure_factories.UserFactory.get_list_url(),
            {
                "username": account.username,
                "email": account.email,
                "full_name": account.full_name,
            },
            REMOTE_ADDR="203.0.113.9",
            HTTP_USER_AGENT="StaffBrowser/1.0",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created = structure_factories.UserFactory._meta.model.objects.get(
            username=account.username
        )
        created_url = structure_factories.UserFactory.get_url(created)

        [staff_view] = read(
            self.client,
            self.fixture.staff,
            created_url,
            "user_has_been_created_by_staff",
        )
        self.assertEqual(staff_view["context"]["ip_address"], "203.0.113.9")

        [own_view] = read(
            self.client, created, created_url, "user_has_been_created_by_staff"
        )
        self.assertNotIn("ip_address", own_view["context"])
        self.assertNotIn("user_agent", own_view["context"])
