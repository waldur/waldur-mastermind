#!/usr/bin/env python3
"""
Test that all migrations apply cleanly on a fresh database.

Spins up a temporary PostgreSQL container, runs all migrations from scratch,
reports the count and timing, and cleans up.

Usage:
    # Run with defaults (auto-starts/stops a Docker PostgreSQL container)
    uv run python scripts/test_fresh_migrations.py

    # Use an existing local PostgreSQL (skips Docker)
    uv run python scripts/test_fresh_migrations.py --host /tmp --user waldur --no-password

    # Use an existing remote PostgreSQL
    uv run python scripts/test_fresh_migrations.py --host localhost --port 5432 --user myuser --password mypass

    # Keep the container running after test (for debugging)
    uv run python scripts/test_fresh_migrations.py --keep

    # Custom container/database name
    uv run python scripts/test_fresh_migrations.py --container-name my-pg --db-name my_test_db
"""

import argparse
import os
import subprocess
import sys
import time

CONTAINER_NAME_DEFAULT = "waldur-migration-test-pg"
DB_NAME_DEFAULT = "waldur_migration_test"
PG_USER_DEFAULT = "postgres"
PG_PASSWORD_DEFAULT = "postgres"
PG_IMAGE = "postgres:16-alpine"
PG_PORT = 15433  # Non-standard port to avoid conflicts


def run(cmd, capture=False, check=True, **kwargs):
    """Run a command and return the result."""
    if isinstance(cmd, str):
        cmd = cmd.split()
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
        **kwargs,
    )
    return result


def container_exists(name):
    """Check if a Docker container exists (running or stopped)."""
    result = run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture=True,
        check=False,
    )
    return name in result.stdout.strip()


def container_running(name):
    """Check if a Docker container is running."""
    result = run(
        ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture=True,
        check=False,
    )
    return name in result.stdout.strip()


def docker_available():
    """Check if Docker daemon is running."""
    result = run(["docker", "info"], capture=True, check=False)
    return result.returncode == 0


def start_postgres(container_name, port, pg_user, pg_password):
    """Start a temporary PostgreSQL container. Returns True if we started it."""
    if not docker_available():
        print(
            "Error: Docker is not running. Either start Docker or use --host to connect to an existing PostgreSQL."
        )
        sys.exit(1)

    if container_running(container_name):
        print(f"Container '{container_name}' is already running.")
        return False

    if container_exists(container_name):
        print(f"Removing stopped container '{container_name}'...")
        run(["docker", "rm", container_name])

    print(f"Starting PostgreSQL container '{container_name}' on port {port}...")
    run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-e",
            f"POSTGRES_USER={pg_user}",
            "-e",
            f"POSTGRES_PASSWORD={pg_password}",
            "-p",
            f"{port}:5432",
            PG_IMAGE,
        ]
    )

    # Wait for PostgreSQL to be ready
    print("Waiting for PostgreSQL to be ready...", end="", flush=True)
    for i in range(30):
        result = run(
            ["docker", "exec", container_name, "pg_isready", "-U", pg_user],
            capture=True,
            check=False,
        )
        if result.returncode == 0:
            print(" ready.")
            return True
        print(".", end="", flush=True)
        time.sleep(1)

    print("\nError: PostgreSQL did not become ready in 30 seconds.")
    sys.exit(1)


def stop_postgres(container_name):
    """Stop and remove the PostgreSQL container."""
    print(f"\nCleaning up container '{container_name}'...")
    run(["docker", "stop", container_name], check=False)
    run(["docker", "rm", container_name], check=False)


def create_database(host, port, db_name, pg_user, pg_password):
    """Create a fresh database, dropping it first if it exists."""
    env = {
        **os.environ,
        "PGHOST": host,
        "PGPORT": str(port),
        "PGUSER": pg_user,
    }
    if pg_password:
        env["PGPASSWORD"] = pg_password

    # Drop if exists
    run(
        ["psql", "-c", f"DROP DATABASE IF EXISTS {db_name}"],
        capture=True,
        check=False,
        env=env,
    )

    # Create fresh
    result = run(
        ["psql", "-c", f"CREATE DATABASE {db_name}"],
        capture=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        print(f"Error creating database: {result.stderr}")
        sys.exit(1)

    print(f"Created fresh database '{db_name}'.")


def write_temp_settings(host, port, db_name, pg_user, pg_password):
    """Write a temporary Django settings file for migration testing."""
    settings_path = os.path.join(
        "src", "waldur_core", "server", "_migration_test_settings.py"
    )
    content = f"""# Temporary settings for migration testing - auto-generated, do not commit
from waldur_core.server.base_settings import *  # noqa

SECRET_KEY = "test-key"
DEBUG = True
MEDIA_ROOT = "/tmp/"  # noqa: S108

INSTALLED_APPS += (  # noqa: F405
    "waldur_core.quotas.tests",
    "waldur_core.structure.tests",
    "waldur_pid.tests",
)

CACHES = {{
    "default": {{
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-snowflake",
    }}
}}

ROOT_URLCONF = "waldur_core.structure.tests.urls"

DATABASES = {{
    "default": {{
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "{host}",
        "PORT": "{port}",
        "NAME": "{db_name}",
        "USER": "{pg_user}",
        "PASSWORD": "{pg_password or ""}",
    }},
}}

ALLOWED_HOSTS = ["localhost"]
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = []  # noqa: F405
"""
    with open(settings_path, "w") as f:
        f.write(content)
    return "waldur_core.server._migration_test_settings"


def cleanup_temp_settings():
    """Remove the temporary settings file."""
    settings_path = os.path.join(
        "src", "waldur_core", "server", "_migration_test_settings.py"
    )
    if os.path.exists(settings_path):
        os.remove(settings_path)


def find_manage_command():
    """Find the Django management command (waldur or manage.py)."""
    # Try 'waldur' command first (installed via package)
    result = subprocess.run(
        ["which", "waldur"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return [result.stdout.strip()]

    # Try uv run waldur
    result = subprocess.run(
        ["uv", "run", "which", "waldur"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        waldur_path = result.stdout.strip()
        if waldur_path:
            return [waldur_path]

    # Fallback to manage.py
    for path in ["src/manage.py", "manage.py"]:
        if os.path.exists(path):
            return [sys.executable, path]

    # Last resort: use uv run waldur
    return ["uv", "run", "waldur"]


def run_migrations(settings_module):
    """Run all migrations and return (success, output, duration, migration_count)."""
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": settings_module}
    manage_cmd = find_manage_command()

    # Count migrations first
    result = run(
        [*manage_cmd, "showmigrations", "--list"],
        capture=True,
        check=False,
        env=env,
    )
    total_migrations = result.stdout.count("[ ]")

    print(f"\nRunning {total_migrations} migrations on fresh database...")

    start_time = time.time()
    result = run(
        [*manage_cmd, "migrate", "--verbosity=1"],
        capture=True,
        check=False,
        env=env,
    )
    duration = time.time() - start_time

    success = result.returncode == 0
    output = result.stdout + result.stderr

    # Count applied migrations
    applied = output.count("Applying ")

    return success, output, duration, total_migrations, applied


def main():
    parser = argparse.ArgumentParser(
        description="Test fresh database migrations with a temporary PostgreSQL container"
    )
    parser.add_argument(
        "--host",
        help="PostgreSQL host (skip Docker container, use existing server)",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="PostgreSQL port (default: auto-assigned for Docker, 5432 for --host)",
    )
    parser.add_argument(
        "--db-name",
        default=DB_NAME_DEFAULT,
        help=f"Database name (default: {DB_NAME_DEFAULT})",
    )
    parser.add_argument(
        "--user",
        help=f"PostgreSQL user (default: {PG_USER_DEFAULT})",
    )
    parser.add_argument(
        "--password",
        help=f"PostgreSQL password (default: {PG_PASSWORD_DEFAULT})",
    )
    parser.add_argument(
        "--no-password",
        action="store_true",
        help="Connect without password (peer/trust auth)",
    )
    parser.add_argument(
        "--container-name",
        default=CONTAINER_NAME_DEFAULT,
        help=f"Docker container name (default: {CONTAINER_NAME_DEFAULT})",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the Docker container running after test",
    )
    args = parser.parse_args()

    use_docker = args.host is None
    host = args.host or "localhost"
    port = args.port or (PG_PORT if use_docker else 5432)
    pg_user = args.user or PG_USER_DEFAULT
    pg_password = None if args.no_password else (args.password or PG_PASSWORD_DEFAULT)
    we_started_container = False

    try:
        # Start PostgreSQL if needed
        if use_docker:
            we_started_container = start_postgres(
                args.container_name, port, pg_user, pg_password or PG_PASSWORD_DEFAULT
            )

        # Create fresh database
        create_database(host, port, args.db_name, pg_user, pg_password)

        # Write temporary settings
        settings_module = write_temp_settings(
            host, port, args.db_name, pg_user, pg_password
        )

        # Run migrations
        success, output, duration, total, applied = run_migrations(settings_module)

        # Report
        print("\n" + "=" * 60)
        if success:
            print("RESULT: ALL MIGRATIONS APPLIED SUCCESSFULLY")
        else:
            print("RESULT: MIGRATION FAILED")
        print("=" * 60)
        print(f"Total migrations:   {total}")
        print(f"Applied migrations: {applied}")
        print(f"Duration:           {duration:.1f}s")
        print()

        if not success:
            # Show last 30 lines of output for debugging
            lines = output.strip().split("\n")
            print("Last 30 lines of output:")
            print("-" * 60)
            for line in lines[-30:]:
                print(line)
            sys.exit(1)

    finally:
        cleanup_temp_settings()
        if use_docker and we_started_container and not args.keep:
            stop_postgres(args.container_name)
        elif use_docker and args.keep:
            print(f"\nContainer '{args.container_name}' left running (--keep).")
            print(
                f"Stop it with: docker stop {args.container_name} && docker rm {args.container_name}"
            )


if __name__ == "__main__":
    main()
