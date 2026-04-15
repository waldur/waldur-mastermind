#!/usr/bin/env python
"""
Simulate RabbitMQ subscription queue load for testing the RabbitMQ Stats API.

This script creates:
1. Test users with EventSubscriptions (which creates RabbitMQ vhosts/users)
2. Test offerings
3. Publishes messages to subscription queues simulating site agent traffic

Usage:
    python scripts/simulate_rabbitmq_load.py --create    # Create subscriptions and publish messages
    python scripts/simulate_rabbitmq_load.py --cleanup   # Remove test data
    python scripts/simulate_rabbitmq_load.py --status    # Show current queue status

Requirements:
    - RabbitMQ running with STOMP plugin enabled
    - Django settings configured with RABBITMQ settings
"""

import argparse
import json
import logging
import random
import sys
import uuid

import django

django.setup()

from django.contrib.auth import get_user_model

from waldur_core.logging import backend
from waldur_core.logging import models as logging_models
from waldur_core.logging.utils import publish_stomp_messages
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

User = get_user_model()
logger = logging.getLogger(__name__)

# Test data prefix for easy identification and cleanup
TEST_PREFIX = "rmq-test-"


def create_test_user(name_suffix):
    """Create a test user for subscriptions."""
    username = f"{TEST_PREFIX}user-{name_suffix}"

    # Check if user already exists
    user = User.objects.filter(username=username).first()
    if user:
        print(f"  User already exists: {username}")
        return user

    user = structure_factories.UserFactory(
        username=username,
        email=f"{username}@test.example.com",
        first_name="Test",
        last_name=f"Agent {name_suffix.upper()}",
    )
    print(f"  Created user: {username} (uuid: {user.uuid.hex})")
    return user


def create_event_subscription(user, description):
    """Create an EventSubscription which triggers RabbitMQ vhost/user creation."""
    # Check if subscription already exists
    existing = logging_models.EventSubscription.objects.filter(
        user=user, description__startswith=TEST_PREFIX
    ).first()

    if existing:
        print(f"  Subscription already exists for {user.username}: {existing.uuid.hex}")
        return existing

    rmq_backend = backend.RabbitMQManagementBackend()

    # Create vhost (named after user UUID)
    vhost_name = user.uuid.hex
    if not rmq_backend.create_rabbitmq_virtual_host(vhost_name):
        print(f"  WARNING: Failed to create vhost {vhost_name}")

    # Create subscription
    subscription = logging_models.EventSubscription.objects.create(
        user=user,
        description=f"{TEST_PREFIX}{description}",
        observable_objects=[],
    )

    # Create RabbitMQ user for this subscription
    if not rmq_backend.create_rabbitmq_user(subscription.uuid.hex, user.auth_token.key):
        print(f"  WARNING: Failed to create RMQ user {subscription.uuid.hex}")

    # Assign permissions
    permissions = {"configure": ".*", "write": ".*", "read": ".*"}
    if not rmq_backend.assign_rabbitmq_vhost_permissions(
        subscription.uuid.hex, vhost_name, permissions
    ):
        print("  WARNING: Failed to assign permissions")

    print(f"  Created subscription: {subscription.uuid.hex} for {user.username}")
    return subscription


def create_test_offering(customer, name_suffix):
    """Create a test offering."""
    name = f"{TEST_PREFIX}offering-{name_suffix}"

    # Check if offering exists
    existing = marketplace_models.Offering.objects.filter(name=name).first()
    if existing:
        print(f"  Offering already exists: {name}")
        return existing

    offering = marketplace_factories.OfferingFactory(
        customer=customer,
        name=name,
        state=marketplace_models.Offering.States.ACTIVE,
    )
    print(f"  Created offering: {name} (uuid: {offering.uuid.hex})")
    return offering


def publish_test_messages(user, subscription, offering, object_type, count):
    """Publish test messages to a subscription queue."""
    vhost = user.uuid.hex
    topic = f"subscription_{subscription.uuid.hex}_offering_{offering.uuid.hex}_{object_type}"

    messages = []
    for i in range(count):
        payload = json.dumps(
            {
                "type": object_type,
                "action": random.choice(["created", "updated", "deleted"]),
                "resource_uuid": uuid.uuid4().hex,
                "timestamp": f"2024-01-{random.randint(1, 28):02d}T{random.randint(0, 23):02d}:{random.randint(0, 59):02d}:00Z",
                "test_message_index": i,
            }
        )
        messages.append(
            {
                "vhost": vhost,
                "topic": topic,
                "payload": payload,
            }
        )

    print(f"  Publishing {count} messages to {topic[:60]}...")
    publish_stomp_messages(messages)
    return count


def create_test_scenario():
    """Create a complete test scenario with multiple agents and message loads."""
    print("\n" + "=" * 70)
    print("CREATING TEST SCENARIO FOR RABBITMQ STATS API")
    print("=" * 70)

    # Create a customer for offerings
    print("\n[1/4] Creating test customer...")
    customer = structure_factories.CustomerFactory(
        name=f"{TEST_PREFIX}provider",
        abbreviation="RMQTEST",
    )
    print(f"  Created customer: {customer.name}")

    # Create test offerings
    print("\n[2/4] Creating test offerings...")
    offerings = [
        create_test_offering(customer, "compute"),
        create_test_offering(customer, "storage"),
        create_test_offering(customer, "kubernetes"),
    ]

    # Define test agents with different behaviors
    agents = [
        {
            "name": "cscs",
            "description": "CSCS Site Agent - OFFLINE (high backlog)",
            "message_counts": {"resource": 5000, "order": 500, "user_role": 100},
        },
        {
            "name": "ethz",
            "description": "ETH Zurich Agent - HEALTHY (low traffic)",
            "message_counts": {"resource": 50, "order": 10},
        },
        {
            "name": "puhuri",
            "description": "Puhuri Agent - DEGRADED (moderate backlog)",
            "message_counts": {"resource": 1000, "order": 200, "offering_user": 50},
        },
        {
            "name": "dev",
            "description": "Dev Test Agent - ABANDONED (very high backlog)",
            "message_counts": {
                "resource": 10000,
                "order": 1000,
                "service_account": 500,
            },
        },
    ]

    # Create users and subscriptions
    print("\n[3/4] Creating users and subscriptions...")
    subscriptions = []
    for agent in agents:
        user = create_test_user(agent["name"])
        subscription = create_event_subscription(user, agent["description"])
        subscriptions.append(
            {
                "user": user,
                "subscription": subscription,
                "message_counts": agent["message_counts"],
            }
        )

    # Publish messages
    print("\n[4/4] Publishing test messages to queues...")
    total_messages = 0

    for sub_data in subscriptions:
        user = sub_data["user"]
        subscription = sub_data["subscription"]

        # Each subscription publishes to multiple offerings
        for offering in random.sample(offerings, random.randint(1, len(offerings))):
            for obj_type, count in sub_data["message_counts"].items():
                # Vary the count a bit
                actual_count = int(count * random.uniform(0.8, 1.2))
                published = publish_test_messages(
                    user, subscription, offering, obj_type, actual_count
                )
                total_messages += published

    print("\n" + "=" * 70)
    print("TEST SCENARIO CREATED SUCCESSFULLY")
    print(f"  Total subscriptions: {len(subscriptions)}")
    print(f"  Total offerings: {len(offerings)}")
    print(f"  Total messages published: {total_messages:,}")
    print("=" * 70)
    print("\nYou can now test the API:")
    print(
        "  curl -H 'Authorization: Token <staff-token>' http://localhost:8000/api/rabbitmq-stats/"
    )
    print()


def show_status():
    """Show current RabbitMQ queue status."""
    print("\n" + "=" * 70)
    print("CURRENT RABBITMQ SUBSCRIPTION QUEUE STATUS")
    print("=" * 70)

    rmq_backend = backend.RabbitMQManagementBackend()

    try:
        vhost_stats = rmq_backend.list_all_subscription_queues()
    except Exception as e:
        print(f"\nERROR: Could not connect to RabbitMQ: {e}")
        print("Make sure RabbitMQ is running and RABBITMQ settings are configured.")
        return

    if not vhost_stats:
        print("\nNo subscription queues found.")
        return

    total_messages = 0
    total_queues = 0

    for vhost_data in vhost_stats:
        vhost = vhost_data["vhost"]
        queues = vhost_data["queues"]
        vhost_messages = vhost_data["total_messages"]

        # Try to find the user
        user = User.objects.filter(uuid=vhost).first()
        username = user.username if user else "(unknown user)"

        print(f"\n  Vhost: {vhost[:20]}... ({username})")
        print(f"  Total messages: {vhost_messages:,}")
        print(f"  Queues ({len(queues)}):")

        for queue in sorted(queues, key=lambda q: -q["messages"])[:5]:
            status = "🟢" if queue["consumers"] > 0 else "🔴"
            print(
                f"    {status} {queue['name'][-50:]:50} msgs={queue['messages']:>8,} consumers={queue['consumers']}"
            )

        if len(queues) > 5:
            print(f"    ... and {len(queues) - 5} more queues")

        total_messages += vhost_messages
        total_queues += len(queues)

    print("\n" + "-" * 70)
    print(f"  TOTAL: {total_queues} queues, {total_messages:,} messages")
    print("=" * 70 + "\n")


def cleanup_test_data():
    """Remove all test data created by this script."""
    print("\n" + "=" * 70)
    print("CLEANING UP TEST DATA")
    print("=" * 70)

    rmq_backend = backend.RabbitMQManagementBackend()

    # Find and delete test subscriptions
    print("\n[1/3] Removing test subscriptions...")
    test_subscriptions = logging_models.EventSubscription.objects.filter(
        description__startswith=TEST_PREFIX
    )
    for sub in test_subscriptions:
        print(f"  Deleting RMQ user: {sub.uuid.hex}")
        rmq_backend.delete_rabbitmq_user(sub.uuid.hex)
    count = test_subscriptions.count()
    test_subscriptions.delete()
    print(f"  Deleted {count} subscriptions")

    # Find and delete test users (and their vhosts)
    print("\n[2/3] Removing test users and vhosts...")
    test_users = User.objects.filter(username__startswith=TEST_PREFIX)
    for user in test_users:
        print(f"  Deleting vhost: {user.uuid.hex}")
        rmq_backend.delete_rabbitmq_virtual_host(user.uuid.hex)
    count = test_users.count()
    test_users.delete()
    print(f"  Deleted {count} users")

    # Delete test offerings and customer
    print("\n[3/3] Removing test offerings and customer...")
    marketplace_models.Offering.objects.filter(name__startswith=TEST_PREFIX).delete()
    from waldur_core.structure.models import Customer

    Customer.objects.filter(name__startswith=TEST_PREFIX).delete()
    print("  Done")

    print("\n" + "=" * 70)
    print("CLEANUP COMPLETE")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Simulate RabbitMQ subscription queue load for testing"
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="Create test subscriptions and publish messages",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove all test data",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current queue status",
    )

    args = parser.parse_args()

    if not any([args.create, args.cleanup, args.status]):
        parser.print_help()
        sys.exit(1)

    if args.create:
        create_test_scenario()

    if args.status:
        show_status()

    if args.cleanup:
        cleanup_test_data()


if __name__ == "__main__":
    main()
