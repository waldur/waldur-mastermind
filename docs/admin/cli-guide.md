# CLI guide

## ai_assistant

AI Assistant management commands.

  Available subcommands:

```yaml
health       - Check AI Assistant infrastructure health
validate_scenarios - Validate scenario YAML files
test_evaluation   - Test evaluation with real AI Assistant responses
run_all       - Run all checks (health, validate, test)
```

  Examples:

```yaml
waldur ai_assistant health
waldur ai_assistant validate_scenarios
waldur ai_assistant test_evaluation
waldur ai_assistant test_evaluation --scenario greeting_no_tool
waldur ai_assistant run_all
```

```bash

usage: waldur ai_assistant
                           {health,validate_scenarios,test_evaluation,run_all} ...

positional arguments:
  {health,validate_scenarios,test_evaluation,run_all}
                        Available subcommands
    health              Check AI Assistant infrastructure health
    validate_scenarios  Validate scenario YAML files
    test_evaluation     Test evaluation with real AI Assistant responses
    run_all             Run all checks (health, validate, test)

```

## archive_offering

Archive an offering and terminate all its resources (including child offerings' resources), or clean up invoice items for already-terminated resources.

```bash

usage: waldur archive_offering [--dry-run]
                               {terminate,cleanup-invoices} offering_uuid

positional arguments:
  {terminate,cleanup-invoices}
                        terminate: archive offering(s) and terminate all non-
                        terminated resources. cleanup-invoices: remove current
                        month invoice items for terminated resources.
  offering_uuid         UUID of the parent offering to process.

options:
  --dry-run             List affected resources/items without making changes.

```

## audit_broker_config

Audit Celery / RabbitMQ broker configuration for common publisher-reliability misconfigurations.

## axes_list_attempts

List access attempts

## axes_reset

Reset all access attempts and lockouts

## axes_reset_failure_logs

Reset access failure log records older than given days.

```bash

usage: waldur axes_reset_failure_logs [--age AGE]

options:
  --age AGE  Maximum age for records to keep in days

```

## axes_reset_ip

Reset all access attempts and lockouts for given IP addresses

```bash

usage: waldur axes_reset_ip ip [ip ...]

positional arguments:
  ip

```

## axes_reset_ip_username

Reset all access attempts and lockouts for a given IP address and username

```bash

usage: waldur axes_reset_ip_username ip username

positional arguments:
  ip
  username

```

## axes_reset_logs

Reset access log records older than given days.

```bash

usage: waldur axes_reset_logs [--age AGE]

options:
  --age AGE  Maximum age for records to keep in days

```

## axes_reset_username

Reset all access attempts and lockouts for given usernames

```bash

usage: waldur axes_reset_username username [username ...]

positional arguments:
  username

```

## backfill_plan_periods

Backfill plan_period on ComponentUsage records where it is NULL. This fixes incorrect quarterly/annual/total usage calculations caused by ComponentUsage records created without a plan_period.

```bash

usage: waldur backfill_plan_periods [--dry-run]

options:
  --dry-run  Only show what would be done without making changes.

```

## clean_celery_results

Clean up old Celery task results from the database to prevent bloat.

```bash

usage: waldur clean_celery_results [--hours HOURS] [--dry-run]

options:
  --hours HOURS  Delete results older than this many hours (default: 24)
  --dry-run      Show how many results would be deleted without actually
                 deleting

```

## clean_settings_cache

Clean API configuration settings cache.

## cleanup_slurm_logs

Manually trigger cleanup of old SLURM policy evaluation logs. Uses the SLURM_POLICY_EVALUATION_LOG_RETENTION_DAYS constance setting.

## cleanup_stale_event_types

Cleanup stale event types in all hooks.

## cleanup_structure

Delete all Waldur structure data from the database.

  This command removes ALL data including:
  - Users, Customers, Service Providers, Projects
  - Marketplace: Categories, Offerings, Plans, Components, Resources, Orders
  - Permissions: Roles, User Roles, Role Permissions
  - Accounts: Project/Customer Service Accounts, Course Accounts
  - Billing: Invoices, Invoice Items, Component Usages
  - Checklists: Categories, Checklists, Questions, Completions, Answers
  - System: Events, Feeds, Offering Users
  - User Management: Invitations, Group Invitations, Permission Requests

  The cleanup follows reverse dependency order to prevent foreign key violations.
  Invoice item signals are temporarily disconnected to avoid race conditions.

  IMPORTANT: This is a destructive operation that deletes ALL data. Use --dry-run to preview changes.

  Usage:

```yaml
waldur cleanup_structure --dry-run
waldur cleanup_structure
waldur cleanup_structure --skip-users --skip-roles
waldur cleanup_structure --skip-rabbitmq-messages
```

```bash

usage: waldur cleanup_structure [--skip-users] [--skip-roles] [--dry-run]
                                [--skip-rabbitmq-messages] [--fast]

options:
  --skip-users          Skip deleting users.
  --skip-roles          Skip deleting roles and role permissions.
  --dry-run             Show what would be deleted without making changes.
  --skip-rabbitmq-messages
                        Skip sending RabbitMQ messages during cleanup
                        (recommended for large cleanups).
  --fast                Use fast raw SQL DELETE (bypasses all Django signals,
                        much faster for large datasets).

```

## cleanup_usage_poll_records

Delete ComponentUsagePollRecord entries.

```bash

usage: waldur cleanup_usage_poll_records
                                         [--older-than-months OLDER_THAN_MONTHS]

options:
  --older-than-months OLDER_THAN_MONTHS
                        Only delete records older than N months (0 = delete
                        all).

```

## copy_category

Copy structure of categories for the Marketplace

```bash

usage: waldur copy_category source_category_uuid target_category_uuid

positional arguments:
  source_category_uuid  UUID of a category to copy metadata from
  target_category_uuid  UUID of a category to copy metadata to

```

## create_provider

Create a service provider with a linked customer and load categories

```bash

usage: waldur create_provider [-n N] [-c C [C ...]]

options:
  -n N          Customer name
  -c C [C ...]  List of categories to load

```

## createstaffuser

Create a user with a specified username and password. User will be created as staff.

```bash

usage: waldur createstaffuser -u USERNAME -p PASSWORD -e EMAIL

options:
  -u, --username USERNAME
  -p, --password PASSWORD
  -e, --email EMAIL

```

## dedupe_tenant_offerings

Report and optionally resolve tenants that have more than one per-tenant OpenStack.Instance/Volume offering.

```bash

usage: waldur dedupe_tenant_offerings [--tenant TENANT_ID] [--apply] [--merge]

options:
  --tenant TENANT_ID  Restrict to a single OpenStack tenant (by numeric id).
                      Matches the tenant id printed in the self-heal ERROR
                      log.
  --apply             Delete empty duplicate offerings. Without this flag the
                      command only reports (dry-run).
  --merge             Also resolve duplicates that still own resources/orders
                      by re-pointing them onto the keeper before deletion.
                      Requires --apply.

```

## demo_presets

Manage demo data presets for Waldur.

  Available subcommands:

```yaml
list  - List all available presets
info  - Show detailed information about a preset
load  - Load a preset into the database
export - Export current database state as a preset
```

  Examples:

```yaml
waldur demo_presets list
waldur demo_presets info minimal_quickstart
waldur demo_presets load minimal_quickstart --dry-run
waldur demo_presets load hpc_ai_platform
waldur demo_presets export my_custom_preset --description "My setup"
```

```bash

usage: waldur demo_presets {list,info,load,export} ...

positional arguments:
  {list,info,load,export}
                        Available subcommands
    list                List all available demo presets
    info                Show detailed information about a preset
    load                Load a preset into the database
    export              Export current database state as a preset

```

## drop_leftover_openstack_projects

Drop leftover projects from remote OpenStack deployment.
  Leftovers are resources marked as terminated in Waldur but still present in the remote OpenStack.
  Such inconsistency may be caused by split brain problem in the distributed database.

```bash

usage: waldur drop_leftover_openstack_projects [--offering OFFERING]
                                               [--dry-run] [--fuzzy-matching]

options:
  --offering OFFERING  Target marketplace offering name where leftover
                       projects are located.
  --dry-run            Don't make any changes, instead show what projects
                       would be deleted.
  --fuzzy-matching     Try to detect leftovers by name.

```

## drop_stale_permissions

Delete permissions from DB which are no longer in code.

## dump_constance_settings

Dump all settings stored in django-constance to a YAML file.
  This includes all settings, even those with file/image values.

  Usage:

```yaml
waldur dump_constance_settings output.yaml
```

  The output format is compatible with override_constance_settings command.

  For image/file fields, you can optionally export the actual files to a directory
  using --export-media option.

```bash

usage: waldur dump_constance_settings [--include-secrets] [--include-defaults]
                                      [--export-media MEDIA_DIR]
                                      output_file

positional arguments:
  output_file           Output file path for YAML dump of constance settings

options:
  --include-secrets     Include sensitive values (passwords, tokens) in the
                        output
  --include-defaults    Include settings that are set to their default values
  --export-media MEDIA_DIR
                        Export media files (logos, images) to this directory

```

## dumpusers

Dumps information about users, their organizations and projects.

```bash

usage: waldur dumpusers [-o OUTPUT]

options:
  -o, --output OUTPUT  Specifies file to which the output is written. The
                       output will be printed to stdout by default.

```

## evaluate_slurm_policy

Manually trigger SLURM periodic usage policy evaluation. Can evaluate a specific resource against a specific policy, or all resources for a policy.

```bash

usage: waldur evaluate_slurm_policy -p POLICY_UUID [-r RESOURCE_UUID] [--sync]
                                    [--dry-run]

options:
  -p, --policy POLICY_UUID
                        UUID of the SlurmPeriodicUsagePolicy to evaluate.
  -r, --resource RESOURCE_UUID
                        UUID of a specific resource to evaluate. If omitted,
                        evaluates all resources in the policy's offering.
  --sync                Run evaluation synchronously (blocking) instead of
                        queuing Celery tasks.
  --dry-run             Only calculate and display usage percentages without
                        applying actions.

```

## export_ami_catalog

Export catalog of Amazon images.

## export_auth_social

Export OIDC auth configuration as YAML format

```bash

usage: waldur export_auth_social [-o OUTPUT]

options:
  -o, --output OUTPUT  Specifies file to which the output is written. The
                       output will be printed to stdout by default.

```

## export_model_metadata

Collect and export metadata about Django models

## export_offering

Export an offering from Waldur. Export data includes JSON file with an offering data and a thumbnail. Names of this files include offering ID.

```bash

usage: waldur export_offering -o OFFERING -p PATH

options:
  -o, --offering OFFERING
                        An offering UUID.
  -p, --path PATH       Path to the folder where the export data will be
                        saved.

```

## export_roles

Export roles configuration to YAML format.

  This command exports all system roles or optionally only specific roles.
  The output format is compatible with the import_roles command.

  Usage:

```yaml
waldur export_roles roles.yaml
waldur export_roles roles.yaml --system-only
waldur export_roles roles.yaml --include-inactive
```

```bash

usage: waldur export_roles [--system-only] [--include-inactive]
                           [--role-names [ROLE_NAMES ...]]
                           output_file

positional arguments:
  output_file           Output file path for YAML export of roles
                        configuration

options:
  --system-only         Export only system roles (default: all roles)
  --include-inactive    Include inactive roles in export (default: active
                        only)
  --role-names [ROLE_NAMES ...]
                        Export only specific roles by name

```

## export_structure

Export comprehensive Waldur structure data to JSON format.

  This command exports a complete Waldur system structure including:
  - Users, Customers, Service Providers, Projects
  - Marketplace: Categories, Offerings, Plans, Components, Resources, Orders
  - Permissions: Roles, User Roles, Role Permissions
  - Accounts: Project/Customer Service Accounts, Course Accounts
  - Billing: Invoices, Invoice Items, Component Usages, Resource Plan Periods
  - Checklists: Categories, Checklists, Questions, Completions, Answers
  - System: Authentication Tokens, Offering Users
  - User Management: Invitations, Group Invitations, Permission Requests

  The exported JSON file can be used for backup, migration, analysis, or import
  using the import_structure command. All UUIDs and relationships are preserved.

  Usage:

```yaml
waldur export_structure -o structure.json
waldur export_structure --output /path/to/structure.json
```

```bash

usage: waldur export_structure -o OUTPUT [--verbose] [--include-events]

options:
  -o, --output OUTPUT  Path to the output JSON file.
  --verbose            Enable verbose logging output
  --include-events     Include audit log events related to invoicing, credits
                       and policies.

```

## generate_appservice_registration

Generate a Matrix Application Service registration YAML for the homeserver.

```bash

usage: waldur generate_appservice_registration [--url URL]
                                               [--as-token AS_TOKEN]
                                               [--hs-token HS_TOKEN]

options:
  --url URL            Base URL of the Waldur instance.
  --as-token AS_TOKEN  Override as_token (default: read from constance).
  --hs-token HS_TOKEN  Override hs_token (default: read from constance).

```

## generate_mermaid

Generate a Mermaid Class Diagram for specified Django apps and models.

```bash

usage: waldur generate_mermaid [--output OUTPUT_FILE]
                               [--include-models INCLUDE_MODELS]
                               [--exclude-models EXCLUDE_MODELS]
                               [--exclude-field-types EXCLUDE_FIELD_TYPES]
                               [--verbose-names] [--no-inheritance]
                               [--direction {TB,BT,LR,RL}] [--disable-fields]
                               app_label [app_label ...]

positional arguments:
  app_label             Name of the application or applications.

options:
  --output, -o OUTPUT_FILE
                        Save the diagram to a file.
  --include-models, -i INCLUDE_MODELS
                        Models to include (comma-separated, wildcards
                        supported).
  --exclude-models, -e EXCLUDE_MODELS
                        Models to exclude (comma-separated, wildcards
                        supported).
  --exclude-field-types EXCLUDE_FIELD_TYPES
                        Field class names to exclude (e.g.,
                        'TranslationCharField,JsonField').
  --verbose-names       Use model and field verbose_names.
  --no-inheritance      Don't draw inheritance arrows.
  --direction, -d {TB,BT,LR,RL}
                        Direction of the diagram layout.
  --disable-fields      Don't show fields, only model names and relationships.

```

## import_ami_catalog

Import catalog of Amazon images.

```bash

usage: waldur import_ami_catalog [-y] FILE

positional arguments:
  FILE       AMI catalog file.

options:
  -y, --yes  The answer to any question which would be asked will be yes.

```

## import_auth_social

Import OIDC auth configuration in YAML format. The example of auth.yaml:

```yaml
- provider: "keycloak"   # OIDC identity provider in string format. Valid values are: "tara", "eduteams", "keycloak".
label: "Keycloak"    # Human-readable IdP name.
client_id: "waldur"   # A string used in OIDC requests for client identification.
client_secret: OIDC_CLIENT_SECRET
discovery_url: "http://localhost/auth/realms/YOUR_KEYCLOAK_REALM/.well-known/openid-configuration" # OIDC discovery endpoint.
management_url: ""   # Endpoint for user details management.
protected_fields:    # User fields that are imported from IdP.
- "full_name"
- "email"
```

```bash

usage: waldur import_auth_social auth_file

positional arguments:
  auth_file  Specifies location of auth configuration file.

```

## import_azure_image

Import Azure image

```bash

usage: waldur import_azure_image [--sku SKU] [--publisher PUBLISHER]
                                 [--offer OFFER]

options:
  --sku SKU
  --publisher PUBLISHER
  --offer OFFER

```

## import_marketplace_orders

Create marketplace order for each resource if it does not yet exist.

## import_offering

Import or update an offering in Waldur. You must define offering for updating or category and customer for creating.

```bash

usage: waldur import_offering -p PATH [-c CUSTOMER] [-ct CATEGORY]
                              [-o OFFERING]

options:
  -p, --path PATH       File path to offering data.
  -c, --customer CUSTOMER
                        Customer UUID.
  -ct, --category CATEGORY
                        Category UUID.
  -o, --offering OFFERING
                        Updated offering UUID.

```

## import_reppu_usages

Import component usages from Reppu for a specified year and month.

```bash

usage: waldur import_reppu_usages [-m MONTH] [-y YEAR]
                                  [--reppu-api-url REPPU_API_URL]
                                  [--reppu-api-token REPPU_API_TOKEN]
                                  [--dry-run | --no-dry-run]

options:
  -m, --month MONTH     Month for which data is imported.
  -y, --year YEAR       Year for which data is imported.
  --reppu-api-url REPPU_API_URL
                        Reppu API URL.
  --reppu-api-token REPPU_API_TOKEN
                        Reppu API Token.
  --dry-run, --no-dry-run
                        Dry run mode.

```

## import_roles

Import roles configuration in YAML format

```bash

usage: waldur import_roles roles_file

positional arguments:
  roles_file  Specifies location of roles configuration file.

```

## import_structure

Import comprehensive Waldur structure data from JSON format.

  This command imports a complete Waldur system structure including:
  - Users, Customers, Service Providers, Projects
  - Marketplace: Categories, Offerings, Plans, Components, Resources, Orders
  - Permissions: Roles, User Roles, Role Permissions
  - Accounts: Project/Customer Service Accounts, Course Accounts
  - Billing: Invoices, Invoice Items, Component Usages, Resource Plan Periods
  - Checklists: Categories, Checklists, Questions, Completions, Answers
  - System: Authentication Tokens, Offering Users
  - User Management: Invitations, Group Invitations, Permission Requests

  The import maintains dependency order and uses transaction isolation for safety.
  RabbitMQ messages are automatically disabled during import to prevent billing issues.

  Usage:

```yaml
waldur import_structure -i structure.json
waldur import_structure --input structure.json --update
waldur import_structure -i structure.json --skip-users --dry-run
waldur import_structure -i structure.json --skip-rabbitmq-messages --skip-roles
```

```bash

usage: waldur import_structure -i INPUT [--update] [--skip-users]
                               [--skip-roles] [--dry-run]
                               [--skip-rabbitmq-messages] [--skip-user-sync]

options:
  -i, --input INPUT     Path to the input JSON file.
  --update              Update existing objects instead of skipping them.
  --skip-users          Skip importing users.
  --skip-roles          Skip importing roles and role permissions.
  --dry-run             Show what would be imported without making changes.
  --skip-rabbitmq-messages
                        Skip sending RabbitMQ messages during import
                        (recommended for large imports).
  --skip-user-sync      Skip syncing user activation status after import.

```

## import_tenant_quotas

Import OpenStack tenant quotas to marketplace.

```bash

usage: waldur import_tenant_quotas [--dry-run]

options:
  --dry-run  Don't make any changes, instead show what objects would be
             created.

```

## init_component_usage_reporting

Backfills the ComponentUsageMonthly reporting table with historical data. Safe to run multiple times (idempotent).

```bash

usage: waldur init_component_usage_reporting [--months MONTHS] [--all-time]

options:
  --months MONTHS  Number of months to look back (default: 12). Use 0 for
                   current month only.
  --all-time       Calculate from the oldest invoice in the database to the
                   current month.

```

## load_categories

Loads a categories for the Marketplace

```bash

usage: waldur load_categories category [category ...]

positional arguments:
  category  List of categories to load

```

## load_eessi_catalog

Load EESSI software catalog data using the unified catalog loader

```bash

usage: waldur load_eessi_catalog [--json-file JSON_FILE]
                                 [--catalog-name CATALOG_NAME]
                                 [--catalog-version CATALOG_VERSION]
                                 [--api-url API_URL] [--include-extensions]
                                 [--no-extensions] [--dry-run]
                                 [--update-existing] [--no-sync]

options:
  --json-file JSON_FILE
                        Path to JSON file containing EESSI catalog data
  --catalog-name CATALOG_NAME
                        Name of the software catalog (default: EESSI)
  --catalog-version CATALOG_VERSION
                        EESSI catalog version (auto-detect if not provided)
  --api-url API_URL     Base URL for EESSI API data
  --include-extensions  Include extension packages (Python, R packages, etc.)
  --no-extensions       Exclude extension packages
  --dry-run             Show what would be done without making changes
  --update-existing     Update existing catalog data
  --no-sync             Preserve existing records not in source data

```

## load_features

Import features in JSON format

```bash

usage: waldur load_features [--dry-run] features_file

positional arguments:
  features_file  Specifies location of features file.

options:
  --dry-run      Don't make any changes, instead show what objects would be
                 created.

```

## load_notifications

Sync notifications and their templates from a JSON/YAML config file to the DB.

```bash

usage: waldur load_notifications notifications_file

positional arguments:
  notifications_file  Path to a JSON or YAML file mapping notification keys to
                      their enabled status (bool).

```

## load_spack_catalog

Load Spack software catalog data using the unified catalog loader

```bash

usage: waldur load_spack_catalog [--catalog-name CATALOG_NAME]
                                 [--catalog-version CATALOG_VERSION]
                                 [--data-url DATA_URL] [--dry-run]
                                 [--update-existing]

options:
  --catalog-name CATALOG_NAME
                        Name of the software catalog (default: Spack)
  --catalog-version CATALOG_VERSION
                        Spack catalog version (auto-detect if not provided)
  --data-url DATA_URL   URL for Spack repology.json data
  --dry-run             Show what would be done without making changes
  --update-existing     Update existing catalog data (default: true)

```

## load_user_agreements

Imports privacy policy and terms of service into DB

```bash

usage: waldur load_user_agreements [-tos TOS] [-pp PP] [-l LANGUAGE] [-f]

options:
  -tos, --tos TOS       Path to a Terms of service file
  -pp, --pp PP          Path to a Privacy policy file
  -l, --language LANGUAGE
                        ISO 639-1 language code (e.g., 'en', 'de', 'et').
                        Leave empty for the default version.
  -f, --force           Force loading agreements even if they are already
                        defined in DB.

```

## migrate_rabbitmq_queues

Migrate RabbitMQ queues from classic to quorum type

```bash

usage: waldur migrate_rabbitmq_queues [--dry-run] [--vhost VHOST]
                                      [--check-only] [--auto-migrate]
                                      [--force]

options:
  --dry-run       Show what would be done without making changes
  --vhost VHOST   RabbitMQ virtual host to migrate (default: /)
  --check-only    Only check if migration is needed (exit code 0=no migration
                  needed, 1=migration needed)
  --auto-migrate  Automatically proceed with migration without interactive
                  prompts
  --force         Force migration even when queues have pending messages
                  (DANGEROUS)

```

## move_project

Move Waldur project to a different organization.

```bash

usage: waldur move_project -p PROJECT_UUID -c CUSTOMER_UUID
                           [--preserve-user-permissions]

options:
  -p, --project PROJECT_UUID
                        UUID of a project to move.
  -c, --customer CUSTOMER_UUID
                        Target organization UUID
  --preserve-user-permissions
                        Preserve user permissions

```

## move_resource

Move a marketplace resource to a different project.

```bash

usage: waldur move_resource -p PROJECT_UUID -r RESOURCE_UUID

options:
  -p, --project PROJECT_UUID
                        Target project UUID
  -r, --resource RESOURCE_UUID
                        UUID of a marketplace resource to move.

```

## organization_access_subnets

Dumps information about organization access subnets, merging adjacent or overlapping networks.

```bash

usage: waldur organization_access_subnets [-o OUTPUT]

options:
  -o, --output OUTPUT  Specifies file to which the merged subnets will be
                       written. The output will be printed to stdout by
                       default.

```

## override_constance_settings

Override settings stored in django-constance. The example of .yaml file:

  ```yaml
   - WALDUR_SUPPORT_ENABLED: true # Enables support plugin
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE: 'zammad' # Specifies zammad as service desk plugin
    ZAMMAD_API_URL: "https://zammad.example.com/api/" # Specifies zammad API URL
    ZAMMAD_TOKEN: "1282361723491" # Specifies zammad token
    ZAMMAD_GROUP: "default-group" # Specifies zammad group
    ZAMMAD_ARTICLE_TYPE: "email" # Specifies zammad article type
    ZAMMAD_COMMENT_COOLDOWN_DURATION: 7 # Specifies zammad comment cooldown duration
  ```

```bash

usage: waldur override_constance_settings constance_settings_file

positional arguments:
  constance_settings_file
                        Specifies location of file in YAML format containing
                        new settings

```

## override_roles

Override roles configuration in YAML format. The example of roles-override.yaml:

```yaml
- role: CUSTOMER.OWNER
description: "Custom owner role"
is_active: True
add_permissions:
- OFFERING.CREATE
- OFFERING.DELETE
- OFFERING.UPDATE
drop_permissions:
- OFFERING.UPDATE_THUMBNAIL
- OFFERING.UPDATE_ATTRIBUTES
```

```bash

usage: waldur override_roles roles_file

positional arguments:
  roles_file  Specifies location of roles configuration file.

```

## override_templates

Override dbtemplates content from a YAML file. Use --clean to remove DB templates not present in the file.

```bash

usage: waldur override_templates [-c] templates_file

positional arguments:
  templates_file  Path to a YAML file mapping template names to their content.

options:
  -c, --clean     Remove DB templates whose names are not present in the file
                  (full sync mode).

```

## pgmigrate

Load data with disabled signals.

```bash

usage: waldur pgmigrate [--path PATH]

options:
  --path, -p PATH  Path to dumped database.

```

## print_events_enums

Prints all event types as typescript enums.

## print_features_description

Prints all Waldur feature description as typescript code.

## print_features_docs

Prints all Waldur feature toggles in markdown format.

## print_features_enums

Prints all Waldur feature toggles as typescript enums.

## print_mixins

Prints all mixin classes in the codebase in markdown format.

```bash

usage: waldur print_mixins [--output-file OUTPUT_FILE]

options:
  --output-file OUTPUT_FILE
                        Output file path (optional, defaults to stdout)

```

## print_notifications

Prints Mastermind notifications with a description and templates

## print_permissions_description

Prints all Waldur permissions description as typescript code.

## print_registered_handlers

Prints all registered signal handlers in markdown format.

```bash

usage: waldur print_registered_handlers [--output-file OUTPUT_FILE]
                                        [--handler-type {signals,custom_signals,all}]

options:
  --output-file OUTPUT_FILE
                        Output file path (optional, defaults to stdout)
  --handler-type {signals,custom_signals,all}
                        Type of handlers to collect (default: all)

```

## print_scheduled_jobs

Prints all scheduled background jobs in markdown format.

```bash

usage: waldur print_scheduled_jobs [--output-file OUTPUT_FILE]

options:
  --output-file OUTPUT_FILE
                        Output file path (optional, defaults to stdout)

```

## print_settings_description

Prints all Waldur feature description as typescript code.

## probe_broker_latency

Publish N no-op messages and report publish-confirm RTT percentiles. Safe on production; messages have no effect.

```bash

usage: waldur probe_broker_latency [--samples SAMPLES] [--countdown COUNTDOWN]
                                   [--p99-threshold-ms P99_THRESHOLD_MS]
                                   [--policy-class POLICY_CLASS] [--json]

options:
  --samples SAMPLES     Number of publishes to attempt (default: 100).
  --countdown COUNTDOWN
                        Celery countdown in seconds. The probe payload lands
                        in a celery_delayed_* bucket sized for this delay and
                        never executes within the probe's lifetime (default:
                        86400).
  --p99-threshold-ms P99_THRESHOLD_MS
                        If set, exit non-zero when p99 publish-confirm RTT
                        exceeds this many milliseconds. Useful for CI
                        alerting.
  --policy-class POLICY_CLASS
                        Dotted path of a policy class for the no-op payload
                        (default: waldur_mastermind.policy.models.OfferingEsti
                        matedCostPolicy). Any class that
                        evaluate_policies_async can import will work; the task
                        body short-circuits because scope_id=-1 matches no
                        rows.
  --json                Emit results as a single JSON object on stdout.

```

## pull_openstack_usage

Trigger OpenStack usage billing poll synchronously.

## pull_openstack_volume_metadata

Pull OpenStack volumes metadata to marketplace.

```bash

usage: waldur pull_openstack_volume_metadata [--dry-run]

options:
  --dry-run  Don't make any changes, instead show what objects would be
             created.

```

## pull_support_priorities

Pull priorities from support backend.

## pull_support_users

Pull users from support backend.

## push_tenant_quotas

Push OpenStack tenant quotas from marketplace to backend.

```bash

usage: waldur push_tenant_quotas [--dry-run]

options:
  --dry-run  Don't make any changes, instead show what objects would be
             created.

```

## rebuild_billing

Create or update price estimates based on invoices.

## removestalect

Remove Django event log records with stale content types.

## scim_pull_user

Pull user attributes from a remote SCIM 2.0 directory.

```bash

usage: waldur scim_pull_user (--username USERNAME | --all) [--rate RATE]
                             [--source SOURCE]

options:
  --username USERNAME  Pull a single user identified by their Waldur username.
  --all                Pull every active user. Rate-limited (see --rate).
  --rate RATE          Maximum requests per second when using --all (default:
                       5).
  --source SOURCE      Override the source label written to attribute_sources.
                       Defaults to the SCIM_PULL_SOURCE_NAME Constance
                       setting.

```

## set_constance_image

A custom command to set Constance image configs with CLI

```bash

usage: waldur set_constance_image KEY PATH

positional arguments:
  KEY   Constance settings key
  PATH  Path to a logo

```

## set_login_logo_language

Set or remove language-specific login logos

```bash

usage: waldur set_login_logo_language -l LANGUAGE [-f FILE] [-r]

options:
  -l, --language LANGUAGE
                        ISO 639-1 language code (e.g., 'de', 'et', 'fr')
  -f, --file FILE       Path to the logo image file
  -r, --remove          Remove the language-specific logo

```

## slurm_policy_status

Display status of SLURM periodic usage policies: current resource states, recent evaluation logs, and command history.

```bash

usage: waldur slurm_policy_status [-p POLICY_UUID] [-r RESOURCE_UUID]
                                  [--logs LOGS] [--commands COMMANDS]

options:
  -p, --policy POLICY_UUID
                        UUID of a specific policy. If omitted, shows all SLURM
                        policies.
  -r, --resource RESOURCE_UUID
                        Filter output to a specific resource UUID.
  --logs LOGS           Number of recent evaluation logs to display (default:
                        10).
  --commands COMMANDS   Number of recent command history entries to display
                        (default: 5).

```

## status

Check status of Waldur MasterMind configured services

## switching_backend_server

Backend data update if a server was switched.

## sync_arrow_resources

Sync Arrow IAAS subscriptions to Waldur Resources

```bash

usage: waldur sync_arrow_resources [--period-from PERIOD_FROM]
                                   [--period-to PERIOD_TO]
                                   [--customer-uuid CUSTOMER_UUID]
                                   [--project-uuid PROJECT_UUID] [--dry-run]
                                   [--create-offering] [--force-import]

options:
  --period-from PERIOD_FROM
                        Start period in YYYY-MM format (default: 6 months ago,
                        Arrow max)
  --period-to PERIOD_TO
                        End period in YYYY-MM format (default: current month)
  --customer-uuid CUSTOMER_UUID
                        Waldur Customer UUID to create resources under
  --project-uuid PROJECT_UUID
                        Waldur Project UUID to create resources under
  --dry-run             Show what would be done without making changes
  --create-offering     Create Arrow Azure offering if it doesn't exist
  --force-import        Auto-create Waldur Customers and Projects from Arrow
                        data. Each Arrow customer becomes a Waldur Customer
                        with an 'Arrow Azure Subscriptions' project.

```

## sync_saml2_providers

Synchronize SAML2 identity providers.

## validate_openstack_billing

Audit OpenStack instance flavor-derived billing against Placement allocations. Read-only: reports drift, changes nothing.

```bash

usage: waldur validate_openstack_billing [--month MONTH]
                                         [--service-settings SERVICE_SETTINGS]
                                         [--flag-untracked] [--quiet]

options:
  --month MONTH         Reporting period as YYYY-MM. Defaults to the current
                        month. Instances created after the end of the month
                        are excluded.
  --service-settings SERVICE_SETTINGS
                        Limit the audit to a single OpenStack ServiceSettings
                        UUID.
  --flag-untracked      Also report Placement resource classes (e.g. VGPU)
                        that have no matching OfferingComponent on the plan —
                        i.e. silent under-billing.
  --quiet               Only print drift rows and the summary, suppress
                        progress output.

```

## validate_openstack_services

Validate access to all OpenStack services used in Waldur for configured offerings

```bash

usage: waldur validate_openstack_services [--service-uuid SERVICE_UUID]
                                          [--dry-run] [--verbose]
                                          [--test-writes]
                                          [--tenant-uuid TENANT_UUID]
                                          [--offering-uuid OFFERING_UUID]
                                          [--quiet]

options:
  --service-uuid SERVICE_UUID
                        UUID of specific OpenStack service to validate
                        (optional)
  --dry-run             Show what would be validated without actual connection
                        attempts
  --verbose             Enable verbose output
  --test-writes         Test write operations (create/update/delete) -
                        WARNING: Creates and deletes test resources
  --tenant-uuid TENANT_UUID
                        UUID of specific tenant to use for write tests
                        (mutually exclusive with --offering-uuid)
  --offering-uuid OFFERING_UUID
                        UUID of OpenStack offering to test against (creates
                        temporary tenant)
  --quiet               Suppress SSL warnings and other verbose output

```
