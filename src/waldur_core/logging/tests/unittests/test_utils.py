import unittest

from waldur_core.logging.utils import parse_subscription_queue_name


class ParseSubscriptionQueueNameTest(unittest.TestCase):
    def test_valid_queue_name_with_resource_type(self):
        queue_name = "subscription_abc123def456_offering_789abc123456_resource"
        result = parse_subscription_queue_name(queue_name)

        self.assertIsNotNone(result)
        self.assertEqual(result["subscription_uuid"], "abc123def456")
        self.assertEqual(result["offering_uuid"], "789abc123456")
        self.assertEqual(result["object_type"], "resource")

    def test_valid_queue_name_with_order_type(self):
        queue_name = "subscription_aabbccdd_offering_11223344_order"
        result = parse_subscription_queue_name(queue_name)

        self.assertIsNotNone(result)
        self.assertEqual(result["subscription_uuid"], "aabbccdd")
        self.assertEqual(result["offering_uuid"], "11223344")
        self.assertEqual(result["object_type"], "order")

    def test_valid_queue_name_with_user_role_type(self):
        queue_name = "subscription_a1b2c3d4_offering_e5f6a7b8_user_role"
        result = parse_subscription_queue_name(queue_name)

        self.assertIsNotNone(result)
        self.assertEqual(result["subscription_uuid"], "a1b2c3d4")
        self.assertEqual(result["offering_uuid"], "e5f6a7b8")
        self.assertEqual(result["object_type"], "user_role")

    def test_valid_queue_name_with_long_hex_uuids(self):
        queue_name = "subscription_c4fa3bccc0284e14ad9821e42f0fbaa4_offering_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6_resource"
        result = parse_subscription_queue_name(queue_name)

        self.assertIsNotNone(result)
        self.assertEqual(
            result["subscription_uuid"], "c4fa3bccc0284e14ad9821e42f0fbaa4"
        )
        self.assertEqual(result["offering_uuid"], "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
        self.assertEqual(result["object_type"], "resource")

    def test_invalid_queue_name_wrong_prefix(self):
        queue_name = "queue_abc123_offering_def456_resource"
        result = parse_subscription_queue_name(queue_name)

        self.assertIsNone(result)

    def test_invalid_queue_name_missing_offering(self):
        queue_name = "subscription_abc123_def456_resource"
        result = parse_subscription_queue_name(queue_name)

        self.assertIsNone(result)

    def test_invalid_queue_name_empty_string(self):
        result = parse_subscription_queue_name("")

        self.assertIsNone(result)

    def test_invalid_queue_name_celery_queue(self):
        queue_name = "celery"
        result = parse_subscription_queue_name(queue_name)

        self.assertIsNone(result)

    def test_invalid_queue_name_random_string(self):
        queue_name = "some_random_queue_name"
        result = parse_subscription_queue_name(queue_name)

        self.assertIsNone(result)

    def test_invalid_queue_name_partial_match(self):
        # Should not match if pattern is incomplete
        queue_name = "subscription_abc123_offering_"
        result = parse_subscription_queue_name(queue_name)

        self.assertIsNone(result)
