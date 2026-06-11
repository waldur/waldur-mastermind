import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import signals
from rest_framework.authtoken.models import Token

from waldur_core.checklist.models import (
    Answer,
    Checklist,
    ChecklistCompletion,
    Question,
)
from waldur_core.core.middleware import skip_side_effects
from waldur_core.core.models import User
from waldur_core.logging.models import (
    EmailHook,
    Event,
    EventSubscription,
    Feed,
    UserDataAccessLog,
    WebHook,
)
from waldur_core.permissions.models import Role, RolePermission, UserRole
from waldur_core.structure.models import (
    Customer,
    Project,
    ServiceSettings,
    UserAgreement,
)
from waldur_core.users.models import GroupInvitation, Invitation, PermissionRequest
from waldur_mastermind.invoices import handlers as invoice_handlers
from waldur_mastermind.invoices.models import (
    CustomerCredit,
    Invoice,
    InvoiceItem,
    ProjectCredit,
)
from waldur_mastermind.marketplace.models import (
    Category,
    CategoryGroup,
    ComponentUsage,
    CourseAccount,
    CustomerServiceAccount,
    MaintenanceAnnouncement,
    MaintenanceAnnouncementOffering,
    Offering,
    OfferingComponent,
    OfferingPartition,
    OfferingSoftwareCatalog,
    OfferingUser,
    OfferingUserGroup,
    Order,
    Plan,
    PlanComponent,
    ProjectServiceAccount,
    Resource,
    RobotAccount,
    ServiceProvider,
    SoftwareCatalog,
    SoftwarePackage,
    SoftwareTarget,
    SoftwareVersion,
)
from waldur_mastermind.policy.models import (
    CustomerEstimatedCostPolicy,
    ProjectEstimatedCostPolicy,
    SlurmPeriodicUsagePolicy,
)
from waldur_mastermind.proposal.models import (
    Call,
    CallManagingOrganisation,
    CallResourceTemplate,
    Proposal,
    RequestedOffering,
    RequestedResource,
    Review,
    Round,
)
from waldur_openstack.models import Flavor, Image, Instance, Tenant, Volume


class Command(BaseCommand):
    help = """
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
        waldur cleanup_structure --dry-run
        waldur cleanup_structure
        waldur cleanup_structure --skip-users --skip-roles
        waldur cleanup_structure --skip-rabbitmq-messages
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stats = {
            "feeds": {"deleted": 0, "errors": 0},
            "events": {"deleted": 0, "errors": 0},
            "answers": {"deleted": 0, "errors": 0},
            "checklist_completions": {"deleted": 0, "errors": 0},
            "questions": {"deleted": 0, "errors": 0},
            "checklists": {"deleted": 0, "errors": 0},
            "offering_users": {"deleted": 0, "errors": 0},
            "robot_accounts": {"deleted": 0, "errors": 0},
            "offering_user_groups": {"deleted": 0, "errors": 0},
            "invoice_items": {"deleted": 0, "errors": 0},
            "invoices": {"deleted": 0, "errors": 0},
            "customer_credits": {"deleted": 0, "errors": 0},
            "project_credits": {"deleted": 0, "errors": 0},
            "course_accounts": {"deleted": 0, "errors": 0},
            "customer_service_accounts": {"deleted": 0, "errors": 0},
            "project_service_accounts": {"deleted": 0, "errors": 0},
            "user_roles": {"deleted": 0, "errors": 0},
            "role_permissions": {"deleted": 0, "errors": 0},
            "roles": {"deleted": 0, "errors": 0},
            "orders": {"deleted": 0, "errors": 0},
            "component_usages": {"deleted": 0, "errors": 0},
            "resources": {"deleted": 0, "errors": 0},
            "plan_components": {"deleted": 0, "errors": 0},
            "offering_components": {"deleted": 0, "errors": 0},
            "plans": {"deleted": 0, "errors": 0},
            "offerings": {"deleted": 0, "errors": 0},
            "service_providers": {"deleted": 0, "errors": 0},
            "projects": {"deleted": 0, "errors": 0},
            "categories": {"deleted": 0, "errors": 0},
            "category_groups": {"deleted": 0, "errors": 0},
            "user_agreements": {"deleted": 0, "errors": 0},
            "customers": {"deleted": 0, "errors": 0},
            "auth_tokens": {"deleted": 0, "errors": 0},
            "user_data_access_logs": {"deleted": 0, "errors": 0},
            "webhooks": {"deleted": 0, "errors": 0},
            "email_hooks": {"deleted": 0, "errors": 0},
            "event_subscriptions": {"deleted": 0, "errors": 0},
            "users": {"deleted": 0, "errors": 0},
            "permission_requests": {"deleted": 0, "errors": 0},
            "invitations": {"deleted": 0, "errors": 0},
            "group_invitations": {"deleted": 0, "errors": 0},
            # Proposal/call management stats
            "reviews": {"deleted": 0, "errors": 0},
            "requested_resources": {"deleted": 0, "errors": 0},
            "proposals": {"deleted": 0, "errors": 0},
            "call_resource_templates": {"deleted": 0, "errors": 0},
            "rounds": {"deleted": 0, "errors": 0},
            "requested_offerings": {"deleted": 0, "errors": 0},
            "calls": {"deleted": 0, "errors": 0},
            "call_managing_organisations": {"deleted": 0, "errors": 0},
            # Maintenance announcement stats
            "maintenance_announcement_offerings": {"deleted": 0, "errors": 0},
            "maintenance_announcements": {"deleted": 0, "errors": 0},
            # Software catalog stats
            "offering_software_catalogs": {"deleted": 0, "errors": 0},
            "offering_partitions": {"deleted": 0, "errors": 0},
            "software_targets": {"deleted": 0, "errors": 0},
            "software_versions": {"deleted": 0, "errors": 0},
            "software_packages": {"deleted": 0, "errors": 0},
            "software_catalogs": {"deleted": 0, "errors": 0},
            # OpenStack backend model stats
            "openstack_volumes": {"deleted": 0, "errors": 0},
            "openstack_instances": {"deleted": 0, "errors": 0},
            "openstack_tenants": {"deleted": 0, "errors": 0},
            "openstack_images": {"deleted": 0, "errors": 0},
            "openstack_flavors": {"deleted": 0, "errors": 0},
            "openstack_service_settings": {"deleted": 0, "errors": 0},
            "project_estimated_cost_policies": {"deleted": 0, "errors": 0},
            "customer_estimated_cost_policies": {"deleted": 0, "errors": 0},
            "slurm_periodic_policies": {"deleted": 0, "errors": 0},
        }
        self.dry_run = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-users",
            action="store_true",
            help="Skip deleting users.",
        )
        parser.add_argument(
            "--skip-roles",
            action="store_true",
            help="Skip deleting roles and role permissions.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without making changes.",
        )
        parser.add_argument(
            "--skip-rabbitmq-messages",
            action="store_true",
            help="Skip sending RabbitMQ messages during cleanup (recommended for large cleanups).",
        )
        parser.add_argument(
            "--fast",
            action="store_true",
            help="Use fast raw SQL DELETE (bypasses all Django signals, much faster for large datasets).",
        )

    def handle(self, **options):
        self.dry_run = options["dry_run"]
        skip_users = options["skip_users"]
        skip_roles = options["skip_roles"]
        skip_side_effects_flag = options["skip_rabbitmq_messages"]
        fast_mode = options["fast"]

        if self.dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made")
            )

        if fast_mode:
            self.stdout.write(
                self.style.WARNING(
                    "FAST MODE - Using raw SQL DELETE (bypasses Django signals)"
                )
            )

        if skip_side_effects_flag:
            self.stdout.write(
                self.style.WARNING(
                    "SKIP RABBITMQ MODE - No RabbitMQ messages will be sent"
                )
            )

        self.stdout.write("Starting structure cleanup...")
        self.stdout.write(
            self.style.WARNING("WARNING: This will delete ALL data from the database!")
        )

        try:
            if fast_mode:
                self._perform_fast_cleanup(skip_users, skip_roles)
            elif skip_side_effects_flag:
                with skip_side_effects():
                    self._perform_cleanup(skip_users, skip_roles)
            else:
                self._perform_cleanup(skip_users, skip_roles)

        except Exception as e:
            if self.dry_run:
                self.stdout.write(
                    self.style.WARNING("Dry run completed - no changes made")
                )
            else:
                self.stdout.write(self.style.ERROR(f"Cleanup failed: {e}"))
                return

        # Print summary
        self.print_summary()

    def _safe_cleanup(self, cleanup_func):
        """Run a cleanup function within a savepoint.

        If the cleanup fails (e.g. due to permission errors on leftover
        test tables), the savepoint is rolled back so the rest of the
        transaction can continue.
        """
        sid = transaction.savepoint()
        try:
            cleanup_func()
            transaction.savepoint_commit(sid)
        except Exception:
            transaction.savepoint_rollback(sid)

    def _perform_cleanup(self, skip_users, skip_roles):
        """Perform the actual cleanup operations."""
        with transaction.atomic():
            # Delete in reverse dependency order (opposite of import)

            # Delete feeds and events first (logging)
            self._safe_cleanup(self.cleanup_feeds)
            self._safe_cleanup(self.cleanup_events)

            # Delete offering users, robot accounts and offering user groups
            self._safe_cleanup(self.cleanup_offering_users)
            self._safe_cleanup(self.cleanup_robot_accounts)
            self._safe_cleanup(self.cleanup_offering_user_groups)

            # Delete checklist data (answers -> completions -> questions -> checklists)
            self._safe_cleanup(self.cleanup_answers)
            self._safe_cleanup(self.cleanup_checklist_completions)
            self._safe_cleanup(self.cleanup_questions)
            self._safe_cleanup(self.cleanup_checklists)

            # Delete proposal/call management data (reverse dependency order)
            # reviews -> requested_resources -> proposals -> call_resource_templates -> rounds -> requested_offerings -> calls -> call_managing_organisations
            self._safe_cleanup(self.cleanup_reviews)
            self._safe_cleanup(self.cleanup_requested_resources)
            self._safe_cleanup(self.cleanup_proposals)
            self._safe_cleanup(self.cleanup_call_resource_templates)
            self._safe_cleanup(self.cleanup_rounds)
            self._safe_cleanup(self.cleanup_requested_offerings)
            self._safe_cleanup(self.cleanup_calls)
            self._safe_cleanup(self.cleanup_call_managing_organisations)

            # Delete credits (depends on customers, projects)
            self._safe_cleanup(self.cleanup_project_credits)
            self._safe_cleanup(self.cleanup_customer_credits)
            # Delete invoicing (depends on customers, resources, projects)
            self._safe_cleanup(self.cleanup_invoice_items)
            self._safe_cleanup(self.cleanup_invoices)

            # Delete account types
            self._safe_cleanup(self.cleanup_course_accounts)
            self._safe_cleanup(self.cleanup_customer_service_accounts)
            self._safe_cleanup(self.cleanup_project_service_accounts)

            # Delete user roles
            self._safe_cleanup(self.cleanup_user_roles)

            # Delete user management data (depends on users, customers, roles)
            self._safe_cleanup(self.cleanup_permission_requests)
            self._safe_cleanup(self.cleanup_invitations)
            self._safe_cleanup(self.cleanup_group_invitations)

            # Delete roles and permissions
            if not skip_roles:
                self._safe_cleanup(self.cleanup_role_permissions)
                self._safe_cleanup(self.cleanup_roles)

            # Delete orders (depends on resources, projects, users, plans)
            self._safe_cleanup(self.cleanup_orders)

            # Delete component usages (depends on resources and components)
            self._safe_cleanup(self.cleanup_component_usages)

            # Delete OpenStack backend models (before resources, reverse dependency order)
            self._safe_cleanup(self.cleanup_openstack_volumes)
            self._safe_cleanup(self.cleanup_openstack_instances)
            self._safe_cleanup(self.cleanup_openstack_tenants)
            self._safe_cleanup(self.cleanup_openstack_images)
            self._safe_cleanup(self.cleanup_openstack_flavors)
            self._safe_cleanup(self.cleanup_openstack_service_settings)

            # Delete resources (depends on offerings, plans, projects)
            self._safe_cleanup(self.cleanup_resources)

            # Delete marketplace components and plans
            self._safe_cleanup(self.cleanup_plan_components)
            self._safe_cleanup(self.cleanup_offering_components)
            self._safe_cleanup(self.cleanup_plans)

            # Delete maintenance announcements (depends on service_providers, offerings)
            self._safe_cleanup(self.cleanup_maintenance_announcement_offerings)
            self._safe_cleanup(self.cleanup_maintenance_announcements)

            # Delete software catalog links and content (reverse dependency order)
            self._safe_cleanup(self.cleanup_offering_software_catalogs)
            self._safe_cleanup(self.cleanup_offering_partitions)
            self._safe_cleanup(self.cleanup_software_targets)
            self._safe_cleanup(self.cleanup_software_versions)
            self._safe_cleanup(self.cleanup_software_packages)
            self._safe_cleanup(self.cleanup_software_catalogs)

            # Delete policies (depends on offerings, projects, customers)
            self._safe_cleanup(self.cleanup_slurm_periodic_policies)
            self._safe_cleanup(self.cleanup_project_estimated_cost_policies)
            self._safe_cleanup(self.cleanup_customer_estimated_cost_policies)

            # Delete offerings, service providers, projects, customers, categories
            self._safe_cleanup(self.cleanup_offerings)
            self._safe_cleanup(self.cleanup_service_providers)
            self._safe_cleanup(self.cleanup_projects)
            self._safe_cleanup(self.cleanup_categories)
            self._safe_cleanup(self.cleanup_category_groups)
            self._safe_cleanup(self.cleanup_user_agreements)
            self._safe_cleanup(self.cleanup_customers)

            # Delete users last
            if not skip_users:
                self._safe_cleanup(self.cleanup_auth_tokens)
                # Logging tables with FK to core_user must be deleted before users
                self._safe_cleanup(self.cleanup_user_data_access_logs)
                self._safe_cleanup(self.cleanup_webhooks)
                self._safe_cleanup(self.cleanup_email_hooks)
                self._safe_cleanup(self.cleanup_event_subscriptions)
                self._safe_cleanup(self.cleanup_users)

            if self.dry_run:
                # Rollback transaction in dry-run mode
                raise Exception("Dry run - rolling back transaction")

    def _perform_fast_cleanup(self, skip_users, skip_roles):
        """
        Perform fast cleanup using TRUNCATE CASCADE.

        This bypasses Django signals entirely for maximum speed.
        CASCADE handles foreign key dependencies automatically.
        """
        # Define tables to truncate (stat_key, table_name)
        tables = [
            # Logging first
            ("feeds", "logging_feed"),
            ("events", "logging_event"),
            # Offering users
            ("offering_users", "marketplace_offeringuser"),
            ("robot_accounts", "marketplace_robotaccount"),
            ("offering_user_groups", "marketplace_offeringusergroup"),
            # Checklists
            ("answers", "checklist_answer"),
            ("checklist_completions", "checklist_checklistcompletion"),
            ("questions", "checklist_question"),
            ("checklists", "checklist_checklist"),
            # Proposal/call management (reverse dependency order)
            ("reviews", "proposal_review"),
            ("requested_resources", "proposal_requestedresource"),
            ("proposals", "proposal_proposal"),
            ("call_resource_templates", "proposal_callresourcetemplate"),
            ("rounds", "proposal_round"),
            ("requested_offerings", "proposal_requestedoffering"),
            ("calls", "proposal_call"),
            ("call_managing_organisations", "proposal_callmanagingorganisation"),
            # Credits
            ("project_credits", "invoices_projectcredit"),
            ("customer_credits", "invoices_customercredit"),
            # Invoices
            ("invoice_items", "invoices_invoiceitem"),
            ("invoices", "invoices_invoice"),
            # Accounts
            ("course_accounts", "marketplace_courseaccount"),
            ("customer_service_accounts", "marketplace_customerserviceaccount"),
            ("project_service_accounts", "marketplace_projectserviceaccount"),
            # User roles
            ("user_roles", "permissions_userrole"),
            # User management
            ("permission_requests", "users_permissionrequest"),
            ("invitations", "users_invitation"),
            ("group_invitations", "users_groupinvitation"),
            # Orders
            ("orders", "marketplace_order"),
            # Component usages
            ("component_usages", "marketplace_componentusage"),
            # OpenStack backend models (before resources, reverse dependency order)
            ("openstack_volumes", "openstack_volume"),
            ("openstack_instances", "openstack_instance"),
            ("openstack_tenants", "openstack_tenant"),
            ("openstack_images", "openstack_image"),
            ("openstack_flavors", "openstack_flavor"),
            # Resources
            ("resources", "marketplace_resource"),
            # Marketplace components and plans
            ("plan_components", "marketplace_plancomponent"),
            ("offering_components", "marketplace_offeringcomponent"),
            ("plans", "marketplace_plan"),
            # Maintenance announcements
            (
                "maintenance_announcement_offerings",
                "marketplace_maintenanceannouncementoffering",
            ),
            ("maintenance_announcements", "marketplace_maintenanceannouncement"),
            # Software catalogs
            ("offering_software_catalogs", "marketplace_offeringsoftwarecatalog"),
            ("offering_partitions", "marketplace_offeringpartition"),
            ("software_targets", "marketplace_softwaretarget"),
            ("software_versions", "marketplace_softwareversion"),
            ("software_packages", "marketplace_softwarepackage"),
            ("software_catalogs", "marketplace_softwarecatalog"),
            # Offerings, service providers, projects, customers, categories
            ("offerings", "marketplace_offering"),
            ("service_providers", "marketplace_serviceprovider"),
            ("projects", "structure_project"),
            ("categories", "marketplace_category"),
            ("category_groups", "marketplace_categorygroup"),
            ("user_agreements", "structure_useragreement"),
            ("customers", "structure_customer"),
        ]

        # Add roles if not skipped
        if not skip_roles:
            tables.append(("role_permissions", "permissions_rolepermission"))
            tables.append(("roles", "permissions_role"))

        # Add users if not skipped
        if not skip_users:
            tables.append(("auth_tokens", "authtoken_token"))
            # Logging tables with FK to core_user must be deleted before users
            tables.append(("user_data_access_logs", "logging_userdataaccesslog"))
            tables.append(("webhooks", "logging_webhook"))
            tables.append(("email_hooks", "logging_emailhook"))
            tables.append(("event_subscriptions", "logging_eventsubscription"))
            tables.append(("users", "core_user"))

        with transaction.atomic():
            cursor = connection.cursor()

            for stat_key, table_name in tables:
                self.stdout.write(f"Deleting {stat_key}...")
                self._fast_delete_table(cursor, stat_key, table_name)

            if self.dry_run:
                raise Exception("Dry run - rolling back transaction")

    def _fast_delete_table(self, cursor, stat_key, table_name, max_retries=3):
        """
        Delete a table using TRUNCATE CASCADE with retries on deadlock.

        TRUNCATE acquires AccessExclusiveLock on the target table and all
        tables referenced via CASCADE, which can deadlock with concurrent
        processes (e.g. Celery workers, API requests). On deadlock, we retry
        TRUNCATE CASCADE after a delay rather than falling back to DELETE FROM,
        because DELETE FROM does not cascade and will fail on tables with
        unhandled foreign key dependencies.
        """
        for attempt in range(max_retries):
            sid = transaction.savepoint()
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608
                count = cursor.fetchone()[0]

                if not self.dry_run:
                    cursor.execute(
                        f"TRUNCATE TABLE {table_name} CASCADE"  # noqa: S608
                    )

                transaction.savepoint_commit(sid)
                self.stats[stat_key]["deleted"] = count
                return
            except Exception as e:
                transaction.savepoint_rollback(sid)
                if "deadlock detected" in str(e) and attempt < max_retries - 1:
                    wait = 2**attempt
                    self.stdout.write(
                        self.style.WARNING(
                            f"Deadlock on {stat_key}, retrying TRUNCATE CASCADE in {wait}s "
                            f"(attempt {attempt + 2}/{max_retries})..."
                        )
                    )
                    time.sleep(wait)
                else:
                    self.stdout.write(
                        self.style.WARNING(f"Failed to delete {stat_key}: {e}")
                    )
                    self.stats[stat_key]["errors"] += 1
                    return

    def cleanup_feeds(self):
        """Delete all feed data."""
        self.stdout.write("Deleting feeds...")
        try:
            if not self.dry_run:
                count = Feed.objects.count()
                Feed.objects.all().delete()
                self.stats["feeds"]["deleted"] = count
            else:
                self.stats["feeds"]["deleted"] = Feed.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete feeds: {e}"))
            self.stats["feeds"]["errors"] += 1

    def cleanup_events(self):
        """Delete all event data."""
        self.stdout.write("Deleting events...")
        try:
            if not self.dry_run:
                count = Event.objects.count()
                Event.objects.all().delete()
                self.stats["events"]["deleted"] = count
            else:
                self.stats["events"]["deleted"] = Event.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete events: {e}"))
            self.stats["events"]["errors"] += 1

    def cleanup_auth_tokens(self):
        """Delete all auth tokens."""
        self.stdout.write("Deleting auth tokens...")
        try:
            if not self.dry_run:
                count = Token.objects.count()
                Token.objects.all().delete()
                self.stats["auth_tokens"]["deleted"] = count
            else:
                self.stats["auth_tokens"]["deleted"] = Token.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete auth tokens: {e}"))
            self.stats["auth_tokens"]["errors"] += 1

    def cleanup_user_data_access_logs(self):
        """Delete all user data access logs."""
        self.stdout.write("Deleting user data access logs...")
        try:
            if not self.dry_run:
                count = UserDataAccessLog.objects.count()
                UserDataAccessLog.objects.all().delete()
                self.stats["user_data_access_logs"]["deleted"] = count
            else:
                self.stats["user_data_access_logs"]["deleted"] = (
                    UserDataAccessLog.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete user data access logs: {e}")
            )
            self.stats["user_data_access_logs"]["errors"] += 1

    def cleanup_webhooks(self):
        """Delete all webhook configurations."""
        self.stdout.write("Deleting webhooks...")
        try:
            if not self.dry_run:
                count = WebHook.objects.count()
                WebHook.objects.all().delete()
                self.stats["webhooks"]["deleted"] = count
            else:
                self.stats["webhooks"]["deleted"] = WebHook.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete webhooks: {e}"))
            self.stats["webhooks"]["errors"] += 1

    def cleanup_email_hooks(self):
        """Delete all email hook configurations."""
        self.stdout.write("Deleting email hooks...")
        try:
            if not self.dry_run:
                count = EmailHook.objects.count()
                EmailHook.objects.all().delete()
                self.stats["email_hooks"]["deleted"] = count
            else:
                self.stats["email_hooks"]["deleted"] = EmailHook.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete email hooks: {e}"))
            self.stats["email_hooks"]["errors"] += 1

    def cleanup_event_subscriptions(self):
        """Delete all event subscriptions."""
        self.stdout.write("Deleting event subscriptions...")
        try:
            if not self.dry_run:
                count = EventSubscription.objects.count()
                EventSubscription.objects.all().delete()
                self.stats["event_subscriptions"]["deleted"] = count
            else:
                self.stats["event_subscriptions"]["deleted"] = (
                    EventSubscription.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete event subscriptions: {e}")
            )
            self.stats["event_subscriptions"]["errors"] += 1

    def cleanup_users(self):
        """Delete all user data."""
        self.stdout.write("Deleting users...")
        try:
            if not self.dry_run:
                # Delete all users including system_robot
                count = User.all_objects.count()
                User.all_objects.all().delete()
                self.stats["users"]["deleted"] = count
            else:
                self.stats["users"]["deleted"] = User.all_objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete users: {e}"))
            self.stats["users"]["errors"] += 1

    def cleanup_customers(self):
        """Delete all customer/organization data."""
        self.stdout.write("Deleting customers...")
        try:
            if not self.dry_run:
                count = Customer.objects.count()
                Customer.objects.all().delete()
                self.stats["customers"]["deleted"] = count
            else:
                self.stats["customers"]["deleted"] = Customer.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete customers: {e}"))
            self.stats["customers"]["errors"] += 1

    def cleanup_projects(self):
        """Delete all project data."""
        self.stdout.write("Deleting projects...")
        try:
            if not self.dry_run:
                # Use objects manager to include soft-deleted projects
                count = Project.objects.count()
                Project.objects.all().delete()
                self.stats["projects"]["deleted"] = count
            else:
                self.stats["projects"]["deleted"] = Project.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete projects: {e}"))
            self.stats["projects"]["errors"] += 1

    def cleanup_categories(self):
        """Delete all marketplace category data."""
        self.stdout.write("Deleting categories...")
        try:
            if not self.dry_run:
                count = Category.objects.count()
                Category.objects.all().delete()
                self.stats["categories"]["deleted"] = count
            else:
                self.stats["categories"]["deleted"] = Category.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete categories: {e}"))
            self.stats["categories"]["errors"] += 1

    def cleanup_category_groups(self):
        """Delete all marketplace category group data."""
        self.stdout.write("Deleting category groups...")
        try:
            if not self.dry_run:
                count = CategoryGroup.objects.count()
                CategoryGroup.objects.all().delete()
                self.stats["category_groups"]["deleted"] = count
            else:
                self.stats["category_groups"]["deleted"] = CategoryGroup.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete category groups: {e}")
            )
            self.stats["category_groups"]["errors"] += 1

    def cleanup_user_agreements(self):
        """Delete all user agreement data."""
        self.stdout.write("Deleting user agreements...")
        try:
            if not self.dry_run:
                count = UserAgreement.objects.count()
                UserAgreement.objects.all().delete()
                self.stats["user_agreements"]["deleted"] = count
            else:
                self.stats["user_agreements"]["deleted"] = UserAgreement.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete user agreements: {e}")
            )
            self.stats["user_agreements"]["errors"] += 1

    def cleanup_offerings(self):
        """Delete all marketplace offering data."""
        self.stdout.write("Deleting offerings...")
        try:
            if not self.dry_run:
                count = Offering.objects.count()
                Offering.objects.all().delete()
                self.stats["offerings"]["deleted"] = count
            else:
                self.stats["offerings"]["deleted"] = Offering.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete offerings: {e}"))
            self.stats["offerings"]["errors"] += 1

    def cleanup_service_providers(self):
        """Delete all service provider data."""
        self.stdout.write("Deleting service providers...")
        try:
            if not self.dry_run:
                count = ServiceProvider.objects.count()
                ServiceProvider.objects.all().delete()
                self.stats["service_providers"]["deleted"] = count
            else:
                self.stats["service_providers"]["deleted"] = (
                    ServiceProvider.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete service providers: {e}")
            )
            self.stats["service_providers"]["errors"] += 1

    def cleanup_roles(self):
        """Delete all role definitions."""
        self.stdout.write("Deleting roles...")
        try:
            if not self.dry_run:
                count = Role.objects.count()
                Role.objects.all().delete()
                self.stats["roles"]["deleted"] = count
            else:
                self.stats["roles"]["deleted"] = Role.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete roles: {e}"))
            self.stats["roles"]["errors"] += 1

    def cleanup_role_permissions(self):
        """Delete all role permission mappings."""
        self.stdout.write("Deleting role permissions...")
        try:
            if not self.dry_run:
                count = RolePermission.objects.count()
                RolePermission.objects.all().delete()
                self.stats["role_permissions"]["deleted"] = count
            else:
                self.stats["role_permissions"]["deleted"] = (
                    RolePermission.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete role permissions: {e}")
            )
            self.stats["role_permissions"]["errors"] += 1

    def cleanup_user_roles(self):
        """Delete all user role assignments."""
        self.stdout.write("Deleting user roles...")
        try:
            if not self.dry_run:
                count = UserRole.objects.count()
                UserRole.objects.all().delete()
                self.stats["user_roles"]["deleted"] = count
            else:
                self.stats["user_roles"]["deleted"] = UserRole.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete user roles: {e}"))
            self.stats["user_roles"]["errors"] += 1

    def cleanup_project_service_accounts(self):
        """Delete all project service account data."""
        self.stdout.write("Deleting project service accounts...")
        try:
            if not self.dry_run:
                count = ProjectServiceAccount.objects.count()
                ProjectServiceAccount.objects.all().delete()
                self.stats["project_service_accounts"]["deleted"] = count
            else:
                self.stats["project_service_accounts"]["deleted"] = (
                    ProjectServiceAccount.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete project service accounts: {e}")
            )
            self.stats["project_service_accounts"]["errors"] += 1

    def cleanup_customer_service_accounts(self):
        """Delete all customer service account data."""
        self.stdout.write("Deleting customer service accounts...")
        try:
            if not self.dry_run:
                count = CustomerServiceAccount.objects.count()
                CustomerServiceAccount.objects.all().delete()
                self.stats["customer_service_accounts"]["deleted"] = count
            else:
                self.stats["customer_service_accounts"]["deleted"] = (
                    CustomerServiceAccount.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete customer service accounts: {e}")
            )
            self.stats["customer_service_accounts"]["errors"] += 1

    def cleanup_course_accounts(self):
        """Delete all course account data."""
        self.stdout.write("Deleting course accounts...")
        try:
            if not self.dry_run:
                count = CourseAccount.objects.count()
                CourseAccount.objects.all().delete()
                self.stats["course_accounts"]["deleted"] = count
            else:
                self.stats["course_accounts"]["deleted"] = CourseAccount.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete course accounts: {e}")
            )
            self.stats["course_accounts"]["errors"] += 1

    def cleanup_plans(self):
        """Delete all plan data."""
        self.stdout.write("Deleting plans...")
        try:
            if not self.dry_run:
                count = Plan.objects.count()
                Plan.objects.all().delete()
                self.stats["plans"]["deleted"] = count
            else:
                self.stats["plans"]["deleted"] = Plan.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete plans: {e}"))
            self.stats["plans"]["errors"] += 1

    def cleanup_offering_components(self):
        """Delete all offering component data."""
        self.stdout.write("Deleting offering components...")
        try:
            if not self.dry_run:
                count = OfferingComponent.objects.count()
                OfferingComponent.objects.all().delete()
                self.stats["offering_components"]["deleted"] = count
            else:
                self.stats["offering_components"]["deleted"] = (
                    OfferingComponent.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete offering components: {e}")
            )
            self.stats["offering_components"]["errors"] += 1

    def cleanup_plan_components(self):
        """Delete all plan component data."""
        self.stdout.write("Deleting plan components...")
        try:
            if not self.dry_run:
                count = PlanComponent.objects.count()
                PlanComponent.objects.all().delete()
                self.stats["plan_components"]["deleted"] = count
            else:
                self.stats["plan_components"]["deleted"] = PlanComponent.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete plan components: {e}")
            )
            self.stats["plan_components"]["errors"] += 1

    def cleanup_resources(self):
        """Delete all marketplace resource data."""
        self.stdout.write("Deleting resources...")
        try:
            if not self.dry_run:
                count = Resource.objects.count()
                Resource.objects.all().delete()
                self.stats["resources"]["deleted"] = count
            else:
                self.stats["resources"]["deleted"] = Resource.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete resources: {e}"))
            self.stats["resources"]["errors"] += 1

    def cleanup_component_usages(self):
        """Delete all component usage data."""
        self.stdout.write("Deleting component usages...")
        try:
            if not self.dry_run:
                count = ComponentUsage.objects.count()
                ComponentUsage.objects.all().delete()
                self.stats["component_usages"]["deleted"] = count
            else:
                self.stats["component_usages"]["deleted"] = (
                    ComponentUsage.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete component usages: {e}")
            )
            self.stats["component_usages"]["errors"] += 1

    def cleanup_invoices(self):
        """Delete all invoice data."""
        self.stdout.write("Deleting invoices...")
        try:
            if not self.dry_run:
                count = Invoice.objects.count()
                Invoice.objects.all().delete()
                self.stats["invoices"]["deleted"] = count
            else:
                self.stats["invoices"]["deleted"] = Invoice.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete invoices: {e}"))
            self.stats["invoices"]["errors"] += 1

    def cleanup_invoice_items(self):
        """Delete all invoice item data."""
        self.stdout.write("Deleting invoice items...")
        try:
            if not self.dry_run:
                # Temporarily disconnect invoice cache update signals to avoid race conditions
                signals.post_save.disconnect(
                    invoice_handlers.update_cache_when_invoice_item_is_updated,
                    sender=InvoiceItem,
                )
                signals.post_delete.disconnect(
                    invoice_handlers.update_cache_when_invoice_item_is_deleted,
                    sender=InvoiceItem,
                )

                try:
                    count = InvoiceItem.objects.count()
                    InvoiceItem.objects.all().delete()
                    self.stats["invoice_items"]["deleted"] = count
                finally:
                    # Reconnect signals
                    signals.post_save.connect(
                        invoice_handlers.update_cache_when_invoice_item_is_updated,
                        sender=InvoiceItem,
                        dispatch_uid="waldur_mastermind.invoices.update_cache_when_invoice_item_is_updated_%s",
                    )
                    signals.post_delete.connect(
                        invoice_handlers.update_cache_when_invoice_item_is_deleted,
                        sender=InvoiceItem,
                        dispatch_uid="waldur_mastermind.invoices.update_cache_when_invoice_item_is_deleted",
                    )
            else:
                self.stats["invoice_items"]["deleted"] = InvoiceItem.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete invoice items: {e}")
            )
            self.stats["invoice_items"]["errors"] += 1

    def cleanup_customer_credits(self):
        """Delete all customer credit data."""
        self.stdout.write("Deleting customer credits...")
        try:
            if not self.dry_run:
                count = CustomerCredit.objects.count()
                CustomerCredit.objects.all().delete()
                self.stats["customer_credits"]["deleted"] = count
            else:
                self.stats["customer_credits"]["deleted"] = (
                    CustomerCredit.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete customer credits: {e}")
            )
            self.stats["customer_credits"]["errors"] += 1

    def cleanup_project_credits(self):
        """Delete all project credit data."""
        self.stdout.write("Deleting project credits...")
        try:
            if not self.dry_run:
                count = ProjectCredit.objects.count()
                ProjectCredit.objects.all().delete()
                self.stats["project_credits"]["deleted"] = count
            else:
                self.stats["project_credits"]["deleted"] = ProjectCredit.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete project credits: {e}")
            )
            self.stats["project_credits"]["errors"] += 1

    def cleanup_orders(self):
        """Delete all order data."""
        self.stdout.write("Deleting orders...")
        try:
            if not self.dry_run:
                count = Order.objects.count()
                Order.objects.all().delete()
                self.stats["orders"]["deleted"] = count
            else:
                self.stats["orders"]["deleted"] = Order.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete orders: {e}"))
            self.stats["orders"]["errors"] += 1

    def cleanup_offering_users(self):
        """Delete all offering user data."""
        self.stdout.write("Deleting offering users...")
        try:
            if not self.dry_run:
                count = OfferingUser.objects.count()
                OfferingUser.objects.all().delete()
                self.stats["offering_users"]["deleted"] = count
            else:
                self.stats["offering_users"]["deleted"] = OfferingUser.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete offering users: {e}")
            )
            self.stats["offering_users"]["errors"] += 1

    def cleanup_robot_accounts(self):
        """Delete all robot account data."""
        self.stdout.write("Deleting robot accounts...")
        try:
            if not self.dry_run:
                count = RobotAccount.objects.count()
                RobotAccount.objects.all().delete()
                self.stats["robot_accounts"]["deleted"] = count
            else:
                self.stats["robot_accounts"]["deleted"] = RobotAccount.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete robot accounts: {e}")
            )
            self.stats["robot_accounts"]["errors"] += 1

    def cleanup_offering_user_groups(self):
        """Delete all offering user group data."""
        self.stdout.write("Deleting offering user groups...")
        try:
            if not self.dry_run:
                count = OfferingUserGroup.objects.count()
                OfferingUserGroup.objects.all().delete()
                self.stats["offering_user_groups"]["deleted"] = count
            else:
                self.stats["offering_user_groups"]["deleted"] = (
                    OfferingUserGroup.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete offering user groups: {e}")
            )
            self.stats["offering_user_groups"]["errors"] += 1

    def cleanup_answers(self):
        """Delete all answer data."""
        self.stdout.write("Deleting answers...")
        try:
            if not self.dry_run:
                count = Answer.objects.count()
                Answer.objects.all().delete()
                self.stats["answers"]["deleted"] = count
            else:
                self.stats["answers"]["deleted"] = Answer.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete answers: {e}"))
            self.stats["answers"]["errors"] += 1

    def cleanup_checklist_completions(self):
        """Delete all checklist completion data."""
        self.stdout.write("Deleting checklist completions...")
        try:
            if not self.dry_run:
                count = ChecklistCompletion.objects.count()
                ChecklistCompletion.objects.all().delete()
                self.stats["checklist_completions"]["deleted"] = count
            else:
                self.stats["checklist_completions"]["deleted"] = (
                    ChecklistCompletion.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete checklist completions: {e}")
            )
            self.stats["checklist_completions"]["errors"] += 1

    def cleanup_questions(self):
        """Delete all question data."""
        self.stdout.write("Deleting questions...")
        try:
            if not self.dry_run:
                count = Question.objects.count()
                Question.objects.all().delete()
                self.stats["questions"]["deleted"] = count
            else:
                self.stats["questions"]["deleted"] = Question.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete questions: {e}"))
            self.stats["questions"]["errors"] += 1

    def cleanup_checklists(self):
        """Delete all checklist data."""
        self.stdout.write("Deleting checklists...")
        try:
            if not self.dry_run:
                count = Checklist.objects.count()
                Checklist.objects.all().delete()
                self.stats["checklists"]["deleted"] = count
            else:
                self.stats["checklists"]["deleted"] = Checklist.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete checklists: {e}"))
            self.stats["checklists"]["errors"] += 1

    def cleanup_permission_requests(self):
        """Delete all permission request data."""
        self.stdout.write("Deleting permission requests...")
        try:
            if not self.dry_run:
                count = PermissionRequest.objects.count()
                PermissionRequest.objects.all().delete()
                self.stats["permission_requests"]["deleted"] = count
            else:
                self.stats["permission_requests"]["deleted"] = (
                    PermissionRequest.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete permission requests: {e}")
            )
            self.stats["permission_requests"]["errors"] += 1

    def cleanup_invitations(self):
        """Delete all invitation data."""
        self.stdout.write("Deleting invitations...")
        try:
            if not self.dry_run:
                count = Invitation.objects.count()
                Invitation.objects.all().delete()
                self.stats["invitations"]["deleted"] = count
            else:
                self.stats["invitations"]["deleted"] = Invitation.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete invitations: {e}"))
            self.stats["invitations"]["errors"] += 1

    def cleanup_group_invitations(self):
        """Delete all group invitation data."""
        self.stdout.write("Deleting group invitations...")
        try:
            if not self.dry_run:
                count = GroupInvitation.objects.count()
                GroupInvitation.objects.all().delete()
                self.stats["group_invitations"]["deleted"] = count
            else:
                self.stats["group_invitations"]["deleted"] = (
                    GroupInvitation.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete group invitations: {e}")
            )
            self.stats["group_invitations"]["errors"] += 1

    def cleanup_reviews(self):
        """Delete all proposal review data."""
        self.stdout.write("Deleting reviews...")
        try:
            if not self.dry_run:
                count = Review.objects.count()
                Review.objects.all().delete()
                self.stats["reviews"]["deleted"] = count
            else:
                self.stats["reviews"]["deleted"] = Review.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete reviews: {e}"))
            self.stats["reviews"]["errors"] += 1

    def cleanup_requested_resources(self):
        """Delete all requested resource data."""
        self.stdout.write("Deleting requested resources...")
        try:
            if not self.dry_run:
                count = RequestedResource.objects.count()
                RequestedResource.objects.all().delete()
                self.stats["requested_resources"]["deleted"] = count
            else:
                self.stats["requested_resources"]["deleted"] = (
                    RequestedResource.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete requested resources: {e}")
            )
            self.stats["requested_resources"]["errors"] += 1

    def cleanup_proposals(self):
        """Delete all proposal data."""
        self.stdout.write("Deleting proposals...")
        try:
            if not self.dry_run:
                count = Proposal.objects.count()
                Proposal.objects.all().delete()
                self.stats["proposals"]["deleted"] = count
            else:
                self.stats["proposals"]["deleted"] = Proposal.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete proposals: {e}"))
            self.stats["proposals"]["errors"] += 1

    def cleanup_call_resource_templates(self):
        """Delete all call resource template data."""
        self.stdout.write("Deleting call resource templates...")
        try:
            if not self.dry_run:
                count = CallResourceTemplate.objects.count()
                CallResourceTemplate.objects.all().delete()
                self.stats["call_resource_templates"]["deleted"] = count
            else:
                self.stats["call_resource_templates"]["deleted"] = (
                    CallResourceTemplate.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete call resource templates: {e}")
            )
            self.stats["call_resource_templates"]["errors"] += 1

    def cleanup_rounds(self):
        """Delete all round data."""
        self.stdout.write("Deleting rounds...")
        try:
            if not self.dry_run:
                count = Round.objects.count()
                Round.objects.all().delete()
                self.stats["rounds"]["deleted"] = count
            else:
                self.stats["rounds"]["deleted"] = Round.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete rounds: {e}"))
            self.stats["rounds"]["errors"] += 1

    def cleanup_requested_offerings(self):
        """Delete all requested offering data."""
        self.stdout.write("Deleting requested offerings...")
        try:
            if not self.dry_run:
                count = RequestedOffering.objects.count()
                RequestedOffering.objects.all().delete()
                self.stats["requested_offerings"]["deleted"] = count
            else:
                self.stats["requested_offerings"]["deleted"] = (
                    RequestedOffering.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete requested offerings: {e}")
            )
            self.stats["requested_offerings"]["errors"] += 1

    def cleanup_calls(self):
        """Delete all call data."""
        self.stdout.write("Deleting calls...")
        try:
            if not self.dry_run:
                count = Call.objects.count()
                Call.objects.all().delete()
                self.stats["calls"]["deleted"] = count
            else:
                self.stats["calls"]["deleted"] = Call.objects.count()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Failed to delete calls: {e}"))
            self.stats["calls"]["errors"] += 1

    def cleanup_call_managing_organisations(self):
        """Delete all call managing organisation data."""
        self.stdout.write("Deleting call managing organisations...")
        try:
            if not self.dry_run:
                count = CallManagingOrganisation.objects.count()
                CallManagingOrganisation.objects.all().delete()
                self.stats["call_managing_organisations"]["deleted"] = count
            else:
                self.stats["call_managing_organisations"]["deleted"] = (
                    CallManagingOrganisation.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete call managing organisations: {e}")
            )
            self.stats["call_managing_organisations"]["errors"] += 1

    def cleanup_maintenance_announcements(self):
        """Delete all maintenance announcement data."""
        self.stdout.write("Deleting maintenance announcements...")
        try:
            if not self.dry_run:
                count = MaintenanceAnnouncement.objects.count()
                MaintenanceAnnouncement.objects.all().delete()
                self.stats["maintenance_announcements"]["deleted"] = count
            else:
                self.stats["maintenance_announcements"]["deleted"] = (
                    MaintenanceAnnouncement.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete maintenance announcements: {e}")
            )
            self.stats["maintenance_announcements"]["errors"] += 1

    def cleanup_maintenance_announcement_offerings(self):
        """Delete all maintenance announcement offering data."""
        self.stdout.write("Deleting maintenance announcement offerings...")
        try:
            if not self.dry_run:
                count = MaintenanceAnnouncementOffering.objects.count()
                MaintenanceAnnouncementOffering.objects.all().delete()
                self.stats["maintenance_announcement_offerings"]["deleted"] = count
            else:
                self.stats["maintenance_announcement_offerings"]["deleted"] = (
                    MaintenanceAnnouncementOffering.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(
                    f"Failed to delete maintenance announcement offerings: {e}"
                )
            )
            self.stats["maintenance_announcement_offerings"]["errors"] += 1

    def cleanup_offering_software_catalogs(self):
        """Delete all offering-to-software-catalog links."""
        self.stdout.write("Deleting offering software catalog links...")
        try:
            if not self.dry_run:
                count = OfferingSoftwareCatalog.objects.count()
                OfferingSoftwareCatalog.objects.all().delete()
                self.stats["offering_software_catalogs"]["deleted"] = count
            else:
                self.stats["offering_software_catalogs"]["deleted"] = (
                    OfferingSoftwareCatalog.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(
                    f"Failed to delete offering software catalog links: {e}"
                )
            )
            self.stats["offering_software_catalogs"]["errors"] += 1

    def cleanup_offering_partitions(self):
        """Delete all offering partition data."""
        self.stdout.write("Deleting offering partitions...")
        try:
            if not self.dry_run:
                count = OfferingPartition.objects.count()
                OfferingPartition.objects.all().delete()
                self.stats["offering_partitions"]["deleted"] = count
            else:
                self.stats["offering_partitions"]["deleted"] = (
                    OfferingPartition.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete offering partitions: {e}")
            )
            self.stats["offering_partitions"]["errors"] += 1

    def cleanup_software_targets(self):
        """Delete all software target data."""
        self.stdout.write("Deleting software targets...")
        try:
            if not self.dry_run:
                count = SoftwareTarget.objects.count()
                SoftwareTarget.objects.all().delete()
                self.stats["software_targets"]["deleted"] = count
            else:
                self.stats["software_targets"]["deleted"] = (
                    SoftwareTarget.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete software targets: {e}")
            )
            self.stats["software_targets"]["errors"] += 1

    def cleanup_software_versions(self):
        """Delete all software version data."""
        self.stdout.write("Deleting software versions...")
        try:
            if not self.dry_run:
                count = SoftwareVersion.objects.count()
                SoftwareVersion.objects.all().delete()
                self.stats["software_versions"]["deleted"] = count
            else:
                self.stats["software_versions"]["deleted"] = (
                    SoftwareVersion.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete software versions: {e}")
            )
            self.stats["software_versions"]["errors"] += 1

    def cleanup_software_packages(self):
        """Delete all software package data."""
        self.stdout.write("Deleting software packages...")
        try:
            if not self.dry_run:
                count = SoftwarePackage.objects.count()
                SoftwarePackage.objects.all().delete()
                self.stats["software_packages"]["deleted"] = count
            else:
                self.stats["software_packages"]["deleted"] = (
                    SoftwarePackage.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete software packages: {e}")
            )
            self.stats["software_packages"]["errors"] += 1

    def cleanup_software_catalogs(self):
        """Delete all software catalog data."""
        self.stdout.write("Deleting software catalogs...")
        try:
            if not self.dry_run:
                count = SoftwareCatalog.objects.count()
                SoftwareCatalog.objects.all().delete()
                self.stats["software_catalogs"]["deleted"] = count
            else:
                self.stats["software_catalogs"]["deleted"] = (
                    SoftwareCatalog.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete software catalogs: {e}")
            )
            self.stats["software_catalogs"]["errors"] += 1

    def cleanup_openstack_volumes(self):
        """Delete all OpenStack volume data."""
        self.stdout.write("Deleting OpenStack volumes...")
        try:
            if not self.dry_run:
                count = Volume.objects.count()
                Volume.objects.all().delete()
                self.stats["openstack_volumes"]["deleted"] = count
            else:
                self.stats["openstack_volumes"]["deleted"] = Volume.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete OpenStack volumes: {e}")
            )
            self.stats["openstack_volumes"]["errors"] += 1

    def cleanup_openstack_instances(self):
        """Delete all OpenStack instance data."""
        self.stdout.write("Deleting OpenStack instances...")
        try:
            if not self.dry_run:
                count = Instance.objects.count()
                Instance.objects.all().delete()
                self.stats["openstack_instances"]["deleted"] = count
            else:
                self.stats["openstack_instances"]["deleted"] = Instance.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete OpenStack instances: {e}")
            )
            self.stats["openstack_instances"]["errors"] += 1

    def cleanup_openstack_tenants(self):
        """Delete all OpenStack tenant data."""
        self.stdout.write("Deleting OpenStack tenants...")
        try:
            if not self.dry_run:
                count = Tenant.objects.count()
                Tenant.objects.all().delete()
                self.stats["openstack_tenants"]["deleted"] = count
            else:
                self.stats["openstack_tenants"]["deleted"] = Tenant.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete OpenStack tenants: {e}")
            )
            self.stats["openstack_tenants"]["errors"] += 1

    def cleanup_openstack_images(self):
        """Delete all OpenStack image data."""
        self.stdout.write("Deleting OpenStack images...")
        try:
            if not self.dry_run:
                count = Image.all_objects.count()
                Image.all_objects.all().delete()
                self.stats["openstack_images"]["deleted"] = count
            else:
                self.stats["openstack_images"]["deleted"] = Image.all_objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete OpenStack images: {e}")
            )
            self.stats["openstack_images"]["errors"] += 1

    def cleanup_openstack_flavors(self):
        """Delete all OpenStack flavor data."""
        self.stdout.write("Deleting OpenStack flavors...")
        try:
            if not self.dry_run:
                count = Flavor.objects.count()
                Flavor.objects.all().delete()
                self.stats["openstack_flavors"]["deleted"] = count
            else:
                self.stats["openstack_flavors"]["deleted"] = Flavor.objects.count()
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete OpenStack flavors: {e}")
            )
            self.stats["openstack_flavors"]["errors"] += 1

    def cleanup_openstack_service_settings(self):
        """Delete OpenStack service settings (type='OpenStack' only)."""
        self.stdout.write("Deleting OpenStack service settings...")
        try:
            if not self.dry_run:
                qs = ServiceSettings.objects.filter(type="OpenStack")
                count = qs.count()
                qs.delete()
                self.stats["openstack_service_settings"]["deleted"] = count
            else:
                self.stats["openstack_service_settings"]["deleted"] = (
                    ServiceSettings.objects.filter(type="OpenStack").count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete OpenStack service settings: {e}")
            )
            self.stats["openstack_service_settings"]["errors"] += 1

    def cleanup_project_estimated_cost_policies(self):
        """Delete project estimated cost policies."""
        self.stdout.write("Deleting project estimated cost policies...")
        try:
            if not self.dry_run:
                count = ProjectEstimatedCostPolicy.objects.count()
                ProjectEstimatedCostPolicy.objects.all().delete()
                self.stats["project_estimated_cost_policies"]["deleted"] = count
            else:
                self.stats["project_estimated_cost_policies"]["deleted"] = (
                    ProjectEstimatedCostPolicy.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(
                    f"Failed to delete project estimated cost policies: {e}"
                )
            )
            self.stats["project_estimated_cost_policies"]["errors"] += 1

    def cleanup_customer_estimated_cost_policies(self):
        """Delete customer estimated cost policies."""
        self.stdout.write("Deleting customer estimated cost policies...")
        try:
            if not self.dry_run:
                count = CustomerEstimatedCostPolicy.objects.count()
                CustomerEstimatedCostPolicy.objects.all().delete()
                self.stats["customer_estimated_cost_policies"]["deleted"] = count
            else:
                self.stats["customer_estimated_cost_policies"]["deleted"] = (
                    CustomerEstimatedCostPolicy.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(
                    f"Failed to delete customer estimated cost policies: {e}"
                )
            )
            self.stats["customer_estimated_cost_policies"]["errors"] += 1

    def cleanup_slurm_periodic_policies(self):
        """Delete SLURM periodic usage policies."""
        self.stdout.write("Deleting SLURM periodic usage policies...")
        try:
            if not self.dry_run:
                count = SlurmPeriodicUsagePolicy.objects.count()
                SlurmPeriodicUsagePolicy.objects.all().delete()
                self.stats["slurm_periodic_policies"]["deleted"] = count
            else:
                self.stats["slurm_periodic_policies"]["deleted"] = (
                    SlurmPeriodicUsagePolicy.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(
                    f"Failed to delete SLURM periodic usage policies: {e}"
                )
            )
            self.stats["slurm_periodic_policies"]["errors"] += 1

    def print_summary(self):
        """Print cleanup summary statistics."""
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS("Cleanup Summary:"))
        self.stdout.write("=" * 60)

        for model_name, stats in self.stats.items():
            self.stdout.write(f"\n{model_name.replace('_', ' ').title()}:")
            self.stdout.write(f"  Deleted: {stats['deleted']}")
            if stats["errors"] > 0:
                self.stdout.write(self.style.WARNING(f"  Errors: {stats['errors']}"))

        total_deleted = sum(s["deleted"] for s in self.stats.values())
        total_errors = sum(s["errors"] for s in self.stats.values())

        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(f"Total Deleted: {total_deleted}")
        if total_errors > 0:
            self.stdout.write(self.style.WARNING(f"Total Errors: {total_errors}"))
        self.stdout.write("=" * 60)
