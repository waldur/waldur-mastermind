import uuid
from types import SimpleNamespace

from django.core import signing
from django.test import SimpleTestCase

from waldur_core.web_shell import tickets


class TicketTest(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(uuid=uuid.uuid4())

    def test_ticket_names_the_user(self):
        ticket = tickets.load(tickets.mint(self.user))
        self.assertEqual(ticket.user_uuid, self.user.uuid.hex)
        self.assertTrue(ticket.nonce)

    def test_each_ticket_has_its_own_nonce(self):
        first = tickets.load(tickets.mint(self.user))
        second = tickets.load(tickets.mint(self.user))
        self.assertNotEqual(first[1], second[1])

    def test_ticket_carries_a_digest_of_the_token_not_the_token(self):
        ticket = tickets.mint(self.user, token_key="secret-token-key")

        self.assertEqual(
            tickets.load(ticket).token_digest,
            tickets.token_digest("secret-token-key"),
        )
        payload = signing.loads(ticket, salt=tickets.SALT)
        self.assertNotIn("secret-token-key", payload.values())

    def test_ticket_minted_without_a_token_is_unbound(self):
        self.assertIsNone(tickets.load(tickets.mint(self.user)).token_digest)

    def test_expired_ticket_is_rejected(self):
        self.assertIsNone(tickets.load(tickets.mint(self.user), max_age=-1))

    def test_value_signed_for_another_purpose_is_rejected(self):
        forged = signing.dumps(
            {"u": self.user.uuid.hex, "n": "nonce"}, salt="another-purpose"
        )
        self.assertIsNone(tickets.load(forged))

    def test_ticket_without_nonce_is_rejected(self):
        ticket = signing.dumps({"u": self.user.uuid.hex}, salt=tickets.SALT)
        self.assertIsNone(tickets.load(ticket))

    def test_garbage_is_rejected(self):
        self.assertIsNone(tickets.load("not-a-ticket"))
