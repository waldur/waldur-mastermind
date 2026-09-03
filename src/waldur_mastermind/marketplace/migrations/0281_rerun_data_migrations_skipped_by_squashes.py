"""Re-run the data migrations that a replacement squash skipped.

The squashes regenerated for 8.1.3-rc.7 (``0226_squashed_0263``, ``0264_squashed_0279``
and ``core.0033_squashed_0045`` among others) carried no RunPython at all, on the
assumption that a ``replaces``-squash only ever runs on an empty database. Django
applies a replacing migration whenever *none* of its replaced migrations is applied
(``MigrationLoader.build_graph``) - that is, on any database upgrading from before
the range - so those deployments skipped every backfill and cleanup in the range
while ``django_migrations`` records the originals as applied.

Which databases those are is read from ``django_migrations``:

* ``MigrationExecutor.record_migration`` records a squash's replaced names and
  then the squash itself in one loop, so the rows form one contiguous block of
  ids, the squash last, with milliseconds between neighbours. Originals applied
  one by one are recorded as they run, and the squash row is added by
  ``check_replacements`` at the end of the run, after everything else.
* A database first installed on rc.7/rc.8 shows the same block (``migrate`` and
  ``migrate_fresh`` alike) but recorded it together with its very first migration.
  It never had anything to skip, and by now carries live data the backfills must
  not touch, so a block recorded within a day of the database's oldest migration
  row is left alone. Upgrades are always further apart than that: the first
  migration of every range here shipped weeks before the squash did.

For each squash applied that way this migration calls the original code again, in
the original order, for the backfills whose inputs still exist. Three cannot be
recovered once the squash ran, because the same squash dropped their source
columns; they are reported and need a pre-upgrade backup:

* 0245 - volume discount ``threshold``/``rate`` -> ``discount_formula``
* 0259 - per-resource access subnets -> offering scopes on ``structure.AccessSubnet``
* 0271 - ``ComponentUsage.recurring`` -> ``missing_usage_policy``

The replaced-migration lists are frozen copies: the squash files themselves are
replaced when their range is squashed again, this migration is not.
"""

import logging
from datetime import timedelta
from importlib import import_module

from django.db import migrations
from django.db.migrations import RunPython, RunSQL
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder

logger = logging.getLogger(__name__)

# Neighbouring rows of one record_migration() loop are a database round-trip
# apart; originals are at least a state render and their own DDL apart.
NEIGHBOUR_GAP = timedelta(seconds=1)
# A squash block recorded this soon after the database's oldest migration row
# belongs to a fresh install, which has nothing to repair.
FRESH_INSTALL_WINDOW = timedelta(days=1)


def _marketplace(name):
    return import_module(__name__.rpartition(".")[0] + "." + name)


def _core(name):
    return import_module("waldur_core.core.migrations." + name)


def _say(message):
    print(message)
    logger.warning(message)


def applied_as_replacement(connection, squash, replaces) -> bool:
    """True when ``squash`` was applied in place of ``replaces`` on an upgrade."""
    rows = MigrationRecorder(connection).migration_qs
    keys = list(replaces) + [squash]
    recorded = {
        (row.app, row.name): row
        for row in rows.filter(
            app__in={app for app, _ in keys}, name__in={name for _, name in keys}
        )
    }
    if any(key not in recorded for key in keys):
        return False
    block = sorted((recorded[key] for key in keys), key=lambda row: row.id)
    if block[-1].name != squash[1] or block[-1].id - block[0].id != len(keys) - 1:
        return False
    if any(
        later.applied - earlier.applied > NEIGHBOUR_GAP
        for earlier, later in zip(block, block[1:])
    ):
        return False
    oldest = rows.order_by("applied").values_list("applied", flat=True).first()
    return block[0].applied - oldest > FRESH_INSTALL_WINDOW


# --- steps that need a guard or a twist the original did not ------------------


def _backfill_posix_pools(apps, schema_editor):
    """0247's backfill creates one pool per offering and cannot be re-run once a
    pool exists (OneToOne); name the offerings that still carry legacy config."""
    PosixIdPool = apps.get_model("marketplace", "PosixIdPool")
    Offering = apps.get_model("marketplace", "Offering")
    if not PosixIdPool.objects.exists():
        _marketplace("_posix_pool_backfill").backfill_posix_pools(apps, schema_editor)
        return
    legacy = _marketplace("_posix_pool_backfill").LEGACY_KEYS
    pending = [
        str(offering.uuid)
        for offering in Offering.objects.filter(
            customer__isnull=False, posix_pool__isnull=True
        ).iterator()
        if any(key in (offering.plugin_options or {}) for key in legacy)
    ]
    if pending:
        _say(
            "      POSIX ID pools already exist, so the legacy backfill of 0247 was "
            "not re-run. Offerings that still carry initial_* plugin options and "
            "have no pool: " + ", ".join(pending)
        )


def _derive_service_access_mode(apps, schema_editor):
    from waldur_core.core import service_access

    if service_access.get_service_access_mode() != service_access.BOTH:
        _say("      SERVICE_ACCESS_MODE was set after the upgrade and is kept.")
        return
    _marketplace("0258_derive_service_access_mode").derive_mode(apps, schema_editor)


def _drop_slurm_tables(apps, schema_editor):
    sql = _marketplace("0265_drop_slurm_tables").DROP_TABLES
    for statement in schema_editor.connection.ops.prepare_sql_script(sql):
        schema_editor.execute(statement, params=None)


def _enable_invitation_notifications(apps, schema_editor):
    """0042 inherits the enabled state of the shared invitation notification.

    On the databases repaired here ``load_notifications`` has since created the
    two new keys, disabled by default, so 0042's get_or_create would change
    nothing. Copy the state the split should have inherited onto rows that are
    still at that default.
    """
    original = _core("0042_call_and_proposal_invitation_notifications")
    Notification = apps.get_model("core", "Notification")
    source = Notification.objects.filter(key=original.SOURCE_KEY).first()
    if source and source.enabled:
        Notification.objects.filter(key__in=original.NEW_KEYS, enabled=False).update(
            enabled=True
        )
    original.create_invitation_notifications(apps, schema_editor)


def _lost(migration, what):
    def report(apps, schema_editor):
        _say(f"      LOST: {what}; restore from a pre-upgrade backup.")

    return (migration, report)


def _call(module, name, function):
    def step(apps, schema_editor):
        getattr(module(name), function)(apps, schema_editor)

    return step


# --- what each squash skipped, in the originals' order ---------------------------

MARKETPLACE_0226 = [
    ("marketplace", "0226_alter_offeringcomponent_limit_period"),
    ("marketplace", "0227_fix_maintenanceannouncementofferingtemplate_related_name"),
    ("marketplace", "0228_component_usage_poll_record"),
    ("marketplace", "0229_remove_offeringuserrole_offering_and_more"),
    ("marketplace", "0230_delete_offeringuserrole_delete_resourceuser_and_more"),
    ("marketplace", "0231_offeringprofile_offering_profile"),
    ("marketplace", "0232_cleanup_orphan_role_availabilities"),
    ("marketplace", "0233_repair_limit_period_after_0226"),
    ("marketplace", "0234_resource_project_soft_delete"),
    ("marketplace", "0235_offeringgroup_offering_offering_group"),
    ("marketplace", "0236_project_order_auto_approval"),
    ("marketplace", "0237_resourcelimitchangerequest"),
    ("marketplace", "0238_alter_softwareversion_options_and_more"),
    ("marketplace", "0239_offeringuser_runtime_state"),
    ("marketplace", "0240_deactivate_orphaned_offering_roles"),
    ("marketplace", "0241_offering_role_group"),
    ("marketplace", "0242_resource_end_date_updated_at"),
    ("marketplace", "0243_offeringuserattributeconfig_expose_organization_vat_code"),
    ("marketplace", "0244_offeringuserattributeconfig_expose_organization_address"),
    ("marketplace", "0245_plancomponent_discount_formula"),
    ("marketplace", "0246_plancomponent_discount_aggregation"),
    ("marketplace", "0247_posixidpool_posixidentity_and_more"),
    ("marketplace", "0248_posixidpool_optional_namespaces"),
    ("marketplace", "0249_offeringuserattributeconfig_expose_primary_gid_and_more"),
    ("marketplace", "0250_resource_usage_limit_restriction"),
    ("marketplace", "0251_resourceaccesssubnet"),
    ("marketplace", "0252_offeringaccesssubnet"),
    ("marketplace", "0253_slurmofferingqos_slurmpartitionqos_and_more"),
    ("marketplace", "0254_slurm_partition_qos_offering_trigger"),
    ("marketplace", "0253_resourceapikey"),
    ("marketplace", "0254_resourcemembersyncstatus"),
    ("marketplace", "0255_null_stale_scope_content_types"),
    ("marketplace", "0256_resource_api_key_drop_terminating"),
    ("marketplace", "0257_drop_resource_api_key_fingerprint"),
    ("marketplace", "0258_derive_service_access_mode"),
    ("marketplace", "0259_access_subnet_offering_scopes"),
    ("marketplace", "0260_order_message_timestamps"),
    ("marketplace", "0261_resourceenddatechangerequest"),
    ("marketplace", "0262_category_description_km_category_title_km_and_more"),
    ("marketplace", "0263_remove_category_default_tenant_category"),
]
MARKETPLACE_0264 = [
    ("marketplace", "0264_nullify_slurm_resource_scopes"),
    ("marketplace", "0265_drop_slurm_tables"),
    ("marketplace", "0266_archive_slurm_offerings"),
    ("marketplace", "0267_terminate_slurm_resources"),
    ("marketplace", "0268_alter_offering_secret_options"),
    ("marketplace", "0269_encrypt_existing_secret_options"),
    ("marketplace", "0270_scrub_secret_options_from_reversion"),
    ("marketplace", "0271_missing_usage_policy"),
    ("marketplace", "0272_alter_customerserviceaccount_options_and_more"),
    ("marketplace", "0273_alter_componentusagemonthly_options_and_more"),
    (
        "marketplace",
        "0274_alter_category_options_alter_categorycolumn_options_and_more",
    ),
    ("marketplace", "0275_alter_accesssubnetofferingscope_options"),
    ("marketplace", "0276_posix_identity_principals"),
    ("marketplace", "0277_order_mp_order_attachment_idx_and_more"),
    ("marketplace", "0278_one_time_plan_component_amount"),
    ("marketplace", "0279_croatian_translation_fields"),
]
CORE_0033 = [
    ("core", "0033_tokenexchangecode"),
    ("core", "0034_user_can_use_personal_access_tokens"),
    ("core", "0035_user_is_admin_deactivated"),
    ("core", "0036_user_organization_vat_code"),
    ("core", "0037_user_organization_address"),
    ("core", "0038_remove_quota_notifications"),
    ("core", "0039_user_primary_gid_user_uid_number"),
    ("core", "0040_personalaccesstoken_allowed_networks"),
    ("core", "0041_backfill_user_initial_revisions"),
    ("core", "0042_call_and_proposal_invitation_notifications"),
    ("core", "0043_alter_notificationtemplate_options_and_more"),
    ("core", "0044_alter_dailytablesizehistory_options"),
    ("core", "0045_remove_access_request_notification"),
]

REPAIRS = [
    (
        ("marketplace", "0226_squashed_0263"),
        MARKETPLACE_0226,
        [
            (
                "0226_alter_offeringcomponent_limit_period",
                _call(
                    _marketplace,
                    "0226_alter_offeringcomponent_limit_period",
                    "backfill_limit_period",
                ),
            ),
            (
                "0232_cleanup_orphan_role_availabilities",
                _call(
                    _marketplace,
                    "0232_cleanup_orphan_role_availabilities",
                    "cleanup_orphan_role_availabilities",
                ),
            ),
            (
                "0233_repair_limit_period_after_0226",
                _call(
                    _marketplace,
                    "0233_repair_limit_period_after_0226",
                    "repair_limit_period",
                ),
            ),
            (
                "0240_deactivate_orphaned_offering_roles",
                _call(
                    _marketplace,
                    "0240_deactivate_orphaned_offering_roles",
                    "deactivate_orphaned_offering_roles",
                ),
            ),
            _lost(
                "0245_plancomponent_discount_formula",
                "volume discount threshold/rate were dropped before being converted "
                "to discount_formula",
            ),
            ("0247_posixidpool_posixidentity_and_more", _backfill_posix_pools),
            (
                "0247_posixidpool_posixidentity_and_more",
                _call(
                    _marketplace,
                    "_enable_posix_account_backfill",
                    "backfill_enable_posix_account",
                ),
            ),
            (
                "0253_slurmofferingqos_slurmpartitionqos_and_more",
                _call(_marketplace, "_backfill_offering_qos", "backfill_qos"),
            ),
            (
                "0255_null_stale_scope_content_types",
                _call(
                    _marketplace,
                    "0255_null_stale_scope_content_types",
                    "null_stale_scopes",
                ),
            ),
            (
                "0256_resource_api_key_drop_terminating",
                _call(
                    _marketplace,
                    "0256_resource_api_key_drop_terminating",
                    "strand_terminating_keys",
                ),
            ),
            ("0258_derive_service_access_mode", _derive_service_access_mode),
            _lost(
                "0259_access_subnet_offering_scopes",
                "per-resource access subnets were dropped before being collapsed "
                "into offering scopes",
            ),
        ],
    ),
    (
        ("marketplace", "0264_squashed_0279"),
        MARKETPLACE_0264,
        [
            (
                "0264_nullify_slurm_resource_scopes",
                _call(
                    _marketplace,
                    "0264_nullify_slurm_resource_scopes",
                    "nullify_slurm_scopes",
                ),
            ),
            ("0265_drop_slurm_tables", _drop_slurm_tables),
            (
                "0265_drop_slurm_tables",
                _call(
                    _marketplace, "0265_drop_slurm_tables", "drop_slurm_content_types"
                ),
            ),
            (
                "0266_archive_slurm_offerings",
                _call(
                    _marketplace,
                    "0266_archive_slurm_offerings",
                    "archive_slurm_offerings",
                ),
            ),
            (
                "0267_terminate_slurm_resources",
                _call(
                    _marketplace,
                    "0267_terminate_slurm_resources",
                    "terminate_slurm_resources",
                ),
            ),
            (
                "0269_encrypt_existing_secret_options",
                _call(
                    _marketplace,
                    "0269_encrypt_existing_secret_options",
                    "encrypt_existing_secret_options",
                ),
            ),
            (
                "0270_scrub_secret_options_from_reversion",
                _call(
                    _marketplace,
                    "0270_scrub_secret_options_from_reversion",
                    "scrub_secret_options",
                ),
            ),
            _lost(
                "0271_missing_usage_policy",
                "ComponentUsage.recurring was dropped before being converted to "
                "missing_usage_policy",
            ),
            (
                "0276_posix_identity_principals",
                _call(
                    _marketplace,
                    "_posix_identity_principals",
                    "backfill_posix_identity_principals",
                ),
            ),
            (
                "0278_one_time_plan_component_amount",
                _call(
                    _marketplace,
                    "0278_one_time_plan_component_amount",
                    "set_default_amount",
                ),
            ),
        ],
    ),
    (
        ("core", "0033_squashed_0045"),
        CORE_0033,
        [
            (
                "0038_remove_quota_notifications",
                _call(
                    _core,
                    "0038_remove_quota_notifications",
                    "remove_quota_notifications",
                ),
            ),
            (
                "0041_backfill_user_initial_revisions",
                _call(
                    _core,
                    "0041_backfill_user_initial_revisions",
                    "backfill_user_initial_revisions",
                ),
            ),
            (
                "0042_call_and_proposal_invitation_notifications",
                _enable_invitation_notifications,
            ),
            (
                "0045_remove_access_request_notification",
                _call(
                    _core,
                    "_remove_access_request_notification",
                    "remove_access_request_notification",
                ),
            ),
        ],
    ),
]


def rerun_skipped_data_migrations(apps, schema_editor):
    connection = schema_editor.connection
    for squash, replaces, steps in REPAIRS:
        if not applied_as_replacement(connection, squash, replaces):
            continue
        _say(
            f"\n    {squash[0]}.{squash[1]} was applied in place of its originals; "
            "re-running their data migrations:"
        )
        for name, step in steps:
            print(f"      {name}")
            step(apps, schema_editor)
    report_other_squashes(connection, {squash for squash, _, _ in REPAIRS})


def _has_data_operations(migration):
    return any(
        isinstance(op, RunSQL)
        or (isinstance(op, RunPython) and op.code is not RunPython.noop)
        for op in migration.operations
    )


def report_other_squashes(connection, covered):
    """Name the older ranges this migration does not repair.

    Their data migrations date from before 8.0.8; a deployment that old and
    upgrading straight to 8.1.3-rc.7/rc.8 needs a manual review of what they did.
    """
    loader = MigrationLoader(connection, replace_migrations=False)
    for key, squash in sorted(loader.replacements.items()):
        if key in covered or not applied_as_replacement(
            connection, key, squash.replaces
        ):
            continue
        skipped = [
            name
            for app, name in squash.replaces
            if (app, name) in loader.disk_migrations
            and _has_data_operations(loader.disk_migrations[(app, name)])
        ]
        if skipped:
            _say(
                f"\n    {key[0]}.{key[1]} was applied in place of its originals. "
                "If that happened on 8.1.3-rc.7 or rc.8, these data migrations "
                "were skipped and are not repaired here: " + ", ".join(skipped)
            )


class Migration(migrations.Migration):
    dependencies = [
        (
            "marketplace",
            "0280_category_description_mk_category_description_sq_and_more",
        ),
        ("analytics", "0001_squashed_0003"),
        ("billing", "0002_drop_limit_and_threshold"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("core", "0050_notificationtemplate_path_unique"),
        ("logging", "0028_split_openstack_resource_event_groups"),
        ("permissions", "0027_role_description_mk_role_description_sq"),
        ("policy", "0018_alter_slurmcommandhistory_options_and_more"),
        ("quotas", "0005_drop_zero_usage"),
        ("reversion", "0002_add_index_on_version_for_content_type_and_db"),
        ("support", "0027_alter_issuestatustransition_options"),
    ]

    operations = [
        migrations.RunPython(rerun_skipped_data_migrations, migrations.RunPython.noop),
    ]
