# Default schedule interval for SCIM entitlement reconciliation task (in hours).
# Kept in a dependency-free module so both Celery settings and SCIM tasks can
# import it without triggering Django model imports during settings load.
DEFAULT_SCIM_RECONCILIATION_SCHEDULE_HOURS = 1
