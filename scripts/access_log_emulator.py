#!/usr/bin/env python
"""
Access Log Data Growth Emulator

This script simulates intensive usage of the UserDataAccessLog to measure:
1. Database storage growth (table size, index size)
2. Insert performance under load
3. Query performance as data grows
4. Row count and average row size

Usage:
    DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings_local \
        python scripts/access_log_emulator.py [--users N] [--logs-per-user N] [--batch-size N]
"""

import argparse
import gc
import json
import random
import time
from datetime import timedelta

import django

django.setup()

from django.db import connection, reset_queries
from django.utils import timezone

from waldur_core.core.models import User
from waldur_core.logging.models import UserDataAccessLog

# Sample data for realistic log generation
ACCESSOR_TYPES = [
    UserDataAccessLog.AccessorType.STAFF,
    UserDataAccessLog.AccessorType.SUPPORT,
    UserDataAccessLog.AccessorType.ORGANIZATION_MEMBER,
    UserDataAccessLog.AccessorType.SERVICE_PROVIDER,
    UserDataAccessLog.AccessorType.SELF,
]

ACCESSOR_TYPE_WEIGHTS = [5, 10, 40, 35, 10]  # Distribution of access types

ACCESSED_FIELDS_SAMPLES = [
    ["username", "email"],
    ["username", "full_name", "email"],
    ["username", "full_name", "email", "phone_number", "organization"],
    ["username", "email", "affiliations"],
    ["username", "full_name", "email", "nationality", "organization_type"],
    [
        "username",
        "full_name",
        "email",
        "phone_number",
        "organization",
        "job_title",
        "nationality",
    ],
    ["username"],
    ["email"],
    ["username", "full_name", "email", "gender", "birth_date", "civil_number"],
]

ENDPOINTS = [
    "/api/users/",
    "/api/users/{uuid}/",
    "/api/marketplace-offering-users/",
    "/api/marketplace-offering-users/{uuid}/",
    "/api/project-permissions/",
    "/api/customer-permissions/",
]


def generate_ip_address():
    """Generate a random IP address."""
    return f"{random.randint(1, 255)}.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


def generate_context():
    """Generate realistic context data."""
    endpoint = random.choice(ENDPOINTS)
    context = {
        "endpoint": endpoint,
        "method": "GET",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    }
    if "offering" in endpoint:
        context["offering_uuid"] = f"offer-{random.randint(1000, 9999)}"
    return context


def get_table_stats():
    """Get database table statistics for UserDataAccessLog."""
    with connection.cursor() as cursor:
        # Get table and index sizes (PostgreSQL specific)
        cursor.execute("""
            SELECT
                pg_size_pretty(pg_total_relation_size('logging_userdataaccesslog')) as total_size,
                pg_size_pretty(pg_relation_size('logging_userdataaccesslog')) as table_size,
                pg_size_pretty(pg_indexes_size('logging_userdataaccesslog')) as index_size,
                pg_total_relation_size('logging_userdataaccesslog') as total_bytes,
                pg_relation_size('logging_userdataaccesslog') as table_bytes
        """)
        size_row = cursor.fetchone()

        # Get row count
        cursor.execute("SELECT COUNT(*) FROM logging_userdataaccesslog")
        row_count = cursor.fetchone()[0]

        # Get average row size estimate
        avg_row_size = size_row[4] / row_count if row_count > 0 else 0

        return {
            "total_size": size_row[0],
            "table_size": size_row[1],
            "index_size": size_row[2],
            "total_bytes": size_row[3],
            "table_bytes": size_row[4],
            "row_count": row_count,
            "avg_row_bytes": avg_row_size,
        }


def get_query_performance(users):
    """Measure query performance for common access patterns."""
    if not users:
        return {}

    results = {}

    # Test 1: Get all logs for a user (common GDPR query)
    user = random.choice(users)
    reset_queries()
    start = time.perf_counter()
    list(
        UserDataAccessLog.objects.filter(target_user=user).values_list("id", flat=True)[
            :100
        ]
    )
    results["query_user_logs_100"] = {
        "time_ms": (time.perf_counter() - start) * 1000,
        "description": "Get 100 logs for a specific user",
    }

    # Test 2: Get recent logs (dashboard query)
    start = time.perf_counter()
    list(
        UserDataAccessLog.objects.order_by("-timestamp").values_list("id", flat=True)[
            :100
        ]
    )
    results["query_recent_100"] = {
        "time_ms": (time.perf_counter() - start) * 1000,
        "description": "Get 100 most recent logs",
    }

    # Test 3: Count by accessor type (analytics query)
    start = time.perf_counter()
    from django.db.models import Count

    list(UserDataAccessLog.objects.values("accessor_type").annotate(count=Count("id")))
    results["query_count_by_type"] = {
        "time_ms": (time.perf_counter() - start) * 1000,
        "description": "Count logs grouped by accessor type",
    }

    # Test 4: Filter by date range (audit query)
    week_ago = timezone.now() - timedelta(days=7)
    start = time.perf_counter()
    count = UserDataAccessLog.objects.filter(timestamp__gte=week_ago).count()
    results["query_last_week_count"] = {
        "time_ms": (time.perf_counter() - start) * 1000,
        "description": f"Count logs from last 7 days ({count} rows)",
    }

    return results


def create_test_users(count):
    """Create test users for the emulation."""
    print(f"Creating {count} test users...")
    users = []

    # Check for existing test users
    existing = User.objects.filter(username__startswith="accesslog_test_user_").count()
    if existing > 0:
        print(f"  Found {existing} existing test users, reusing them...")
        users = list(
            User.objects.filter(username__startswith="accesslog_test_user_")[:count]
        )
        if len(users) >= count:
            return users[:count]

    # Create additional users if needed
    for i in range(len(users), count):
        user = User.objects.create_user(
            username=f"accesslog_test_user_{i}_{int(time.time())}",
            email=f"accesslog_test_{i}_{int(time.time())}@example.com",
            first_name=f"Test{i}",
            last_name="User",
        )
        users.append(user)
        if (i + 1) % 100 == 0:
            print(f"  Created {i + 1} users...")

    print(f"  Total test users: {len(users)}")
    return users


def generate_access_logs(users, logs_per_user, batch_size=1000):
    """Generate access logs in batches."""
    total_logs = len(users) * logs_per_user
    print(f"Generating {total_logs:,} access logs ({logs_per_user} per user)...")

    logs_created = 0
    batch = []
    insert_times = []

    start_time = time.perf_counter()

    for target_user in users:
        for _ in range(logs_per_user):
            # Select accessor (can be self or another user)
            accessor_type = random.choices(ACCESSOR_TYPES, ACCESSOR_TYPE_WEIGHTS)[0]
            if accessor_type == UserDataAccessLog.AccessorType.SELF:
                accessor = target_user
            else:
                accessor = random.choice(users)

            log = UserDataAccessLog(
                target_user=target_user,
                accessor=accessor,
                ip_address=generate_ip_address(),
                accessor_type=accessor_type,
                accessed_fields=random.choice(ACCESSED_FIELDS_SAMPLES),
                context=generate_context(),
            )
            batch.append(log)

            if len(batch) >= batch_size:
                batch_start = time.perf_counter()
                UserDataAccessLog.objects.bulk_create(batch)
                batch_time = time.perf_counter() - batch_start
                insert_times.append(batch_time)

                logs_created += len(batch)
                batch = []

                if logs_created % 10000 == 0:
                    elapsed = time.perf_counter() - start_time
                    rate = logs_created / elapsed
                    print(f"  Created {logs_created:,} logs ({rate:.0f} logs/sec)")

    # Insert remaining batch
    if batch:
        batch_start = time.perf_counter()
        UserDataAccessLog.objects.bulk_create(batch)
        batch_time = time.perf_counter() - batch_start
        insert_times.append(batch_time)
        logs_created += len(batch)

    total_time = time.perf_counter() - start_time

    return {
        "logs_created": logs_created,
        "total_time_sec": total_time,
        "logs_per_second": logs_created / total_time,
        "avg_batch_time_ms": (sum(insert_times) / len(insert_times)) * 1000
        if insert_times
        else 0,
        "batch_size": batch_size,
    }


def run_emulation(num_users=100, logs_per_user=100, batch_size=1000, cleanup=False):
    """Run the complete emulation and collect metrics."""
    print("=" * 70)
    print("ACCESS LOG DATA GROWTH EMULATOR")
    print("=" * 70)
    print("Configuration:")
    print(f"  Users: {num_users}")
    print(f"  Logs per user: {logs_per_user}")
    print(f"  Total logs to create: {num_users * logs_per_user:,}")
    print(f"  Batch size: {batch_size}")
    print("=" * 70)

    results = {
        "config": {
            "num_users": num_users,
            "logs_per_user": logs_per_user,
            "batch_size": batch_size,
            "total_logs_target": num_users * logs_per_user,
        },
        "before": {},
        "after": {},
        "insert_stats": {},
        "query_performance": {},
        "growth": {},
    }

    # Get initial stats
    print("\n[1/5] Collecting initial database stats...")
    results["before"] = get_table_stats()
    print(f"  Initial row count: {results['before']['row_count']:,}")
    print(f"  Initial table size: {results['before']['total_size']}")

    # Create test users
    print("\n[2/5] Creating test users...")
    users = create_test_users(num_users)

    # Generate access logs
    print("\n[3/5] Generating access logs...")
    results["insert_stats"] = generate_access_logs(users, logs_per_user, batch_size)
    print(f"  Created {results['insert_stats']['logs_created']:,} logs")
    print(f"  Insert rate: {results['insert_stats']['logs_per_second']:.0f} logs/sec")

    # Force garbage collection and vacuum
    gc.collect()

    # Get final stats
    print("\n[4/5] Collecting final database stats...")
    results["after"] = get_table_stats()
    print(f"  Final row count: {results['after']['row_count']:,}")
    print(f"  Final table size: {results['after']['total_size']}")

    # Calculate growth
    results["growth"] = {
        "rows_added": results["after"]["row_count"] - results["before"]["row_count"],
        "bytes_added": results["after"]["total_bytes"]
        - results["before"]["total_bytes"],
        "bytes_per_row": (
            results["after"]["total_bytes"] - results["before"]["total_bytes"]
        )
        / max(1, results["after"]["row_count"] - results["before"]["row_count"]),
    }

    # Test query performance
    print("\n[5/5] Testing query performance...")
    results["query_performance"] = get_query_performance(users)
    for name, perf in results["query_performance"].items():
        print(f"  {perf['description']}: {perf['time_ms']:.2f} ms")

    # Cleanup if requested
    if cleanup:
        print("\n[Cleanup] Removing test data...")
        UserDataAccessLog.objects.filter(
            target_user__username__startswith="accesslog_test_user_"
        ).delete()
        User.objects.filter(username__startswith="accesslog_test_user_").delete()
        print("  Test data removed")

    return results


def print_report(results):
    """Print a formatted report of the emulation results."""
    print("\n" + "=" * 70)
    print("EMULATION RESULTS REPORT")
    print("=" * 70)

    print("\n📊 DATABASE GROWTH ANALYSIS")
    print("-" * 40)
    print(f"Rows added:        {results['growth']['rows_added']:,}")
    print(
        f"Storage growth:    {results['growth']['bytes_added']:,} bytes ({results['growth']['bytes_added'] / 1024 / 1024:.2f} MB)"
    )
    print(f"Avg bytes per row: {results['growth']['bytes_per_row']:.0f}")

    print("\n📈 PROJECTIONS (based on avg row size)")
    print("-" * 40)
    bytes_per_row = results["growth"]["bytes_per_row"]
    projections = [
        (100_000, "100K logs"),
        (1_000_000, "1M logs"),
        (10_000_000, "10M logs"),
        (100_000_000, "100M logs"),
    ]
    for count, label in projections:
        size_mb = (count * bytes_per_row) / 1024 / 1024
        size_gb = size_mb / 1024
        if size_gb >= 1:
            print(f"  {label:12s}: {size_gb:.2f} GB")
        else:
            print(f"  {label:12s}: {size_mb:.0f} MB")

    print("\n⚡ INSERT PERFORMANCE")
    print("-" * 40)
    print(f"Total logs created:  {results['insert_stats']['logs_created']:,}")
    print(
        f"Total time:          {results['insert_stats']['total_time_sec']:.2f} seconds"
    )
    print(
        f"Insert rate:         {results['insert_stats']['logs_per_second']:.0f} logs/second"
    )
    print(
        f"Avg batch time:      {results['insert_stats']['avg_batch_time_ms']:.2f} ms (batch={results['insert_stats']['batch_size']})"
    )

    print("\n🔍 QUERY PERFORMANCE")
    print("-" * 40)
    for name, perf in results["query_performance"].items():
        print(f"  {perf['description']}")
        print(f"    → {perf['time_ms']:.2f} ms")

    print("\n📋 TABLE STATISTICS")
    print("-" * 40)
    print("Before:")
    print(f"  Rows:       {results['before']['row_count']:,}")
    print(f"  Table size: {results['before']['table_size']}")
    print(f"  Index size: {results['before']['index_size']}")
    print(f"  Total size: {results['before']['total_size']}")
    print("After:")
    print(f"  Rows:       {results['after']['row_count']:,}")
    print(f"  Table size: {results['after']['table_size']}")
    print(f"  Index size: {results['after']['index_size']}")
    print(f"  Total size: {results['after']['total_size']}")

    # Estimate daily growth scenarios
    print("\n📅 DAILY GROWTH SCENARIOS")
    print("-" * 40)
    scenarios = [
        (1000, "Small org (1K accesses/day)"),
        (10000, "Medium org (10K accesses/day)"),
        (100000, "Large org (100K accesses/day)"),
        (1000000, "Very large (1M accesses/day)"),
    ]
    for daily_accesses, label in scenarios:
        daily_mb = (daily_accesses * bytes_per_row) / 1024 / 1024
        monthly_gb = (daily_mb * 30) / 1024
        yearly_gb = (daily_mb * 365) / 1024
        print(f"  {label}")
        print(
            f"    Daily: {daily_mb:.1f} MB | Monthly: {monthly_gb:.2f} GB | Yearly: {yearly_gb:.1f} GB"
        )

    print("\n" + "=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Access Log Data Growth Emulator")
    parser.add_argument(
        "--users", type=int, default=100, help="Number of test users to create"
    )
    parser.add_argument(
        "--logs-per-user", type=int, default=100, help="Number of logs per user"
    )
    parser.add_argument(
        "--batch-size", type=int, default=1000, help="Batch size for bulk inserts"
    )
    parser.add_argument(
        "--cleanup", action="store_true", help="Remove test data after emulation"
    )
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args()

    results = run_emulation(
        num_users=args.users,
        logs_per_user=args.logs_per_user,
        batch_size=args.batch_size,
        cleanup=args.cleanup,
    )

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_report(results)


if __name__ == "__main__":
    main()
