from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import signals

from waldur_core.checklist.models import (
    Answer,
    Checklist,
    ChecklistCompletion,
    Question,
)
from waldur_core.checklist.models import (
    Category as ChecklistCategory,
)
from waldur_core.core.middleware import skip_side_effects
from waldur_core.core.models import User
from waldur_core.logging.models import Event, Feed
from waldur_core.permissions.models import Role, RolePermission, UserRole
from waldur_core.structure.models import Customer, Project
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
    ComponentUsage,
    CourseAccount,
    CustomerServiceAccount,
    Offering,
    OfferingComponent,
    OfferingUser,
    Order,
    Plan,
    PlanComponent,
    ProjectServiceAccount,
    Resource,
    ServiceProvider,
)


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
            "checklist_categories": {"deleted": 0, "errors": 0},
            "offering_users": {"deleted": 0, "errors": 0},
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
            "customers": {"deleted": 0, "errors": 0},
            "users": {"deleted": 0, "errors": 0},
            "permission_requests": {"deleted": 0, "errors": 0},
            "invitations": {"deleted": 0, "errors": 0},
            "group_invitations": {"deleted": 0, "errors": 0},
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

    def _perform_cleanup(self, skip_users, skip_roles):
        """Perform the actual cleanup operations."""
        with transaction.atomic():
            # Delete in reverse dependency order (opposite of import)

            # Delete feeds and events first (logging)
            self.cleanup_feeds()
            self.cleanup_events()

            # Delete offering users
            self.cleanup_offering_users()

            # Delete checklist data (answers -> completions -> questions -> checklists -> categories)
            self.cleanup_answers()
            self.cleanup_checklist_completions()
            self.cleanup_questions()
            self.cleanup_checklists()
            self.cleanup_checklist_categories()

            # Delete credits (depends on customers, projects)
            self.cleanup_project_credits()
            self.cleanup_customer_credits()
            # Delete invoicing (depends on customers, resources, projects)
            self.cleanup_invoice_items()
            self.cleanup_invoices()

            # Delete account types
            self.cleanup_course_accounts()
            self.cleanup_customer_service_accounts()
            self.cleanup_project_service_accounts()

            # Delete user roles
            self.cleanup_user_roles()

            # Delete user management data (depends on users, customers, roles)
            self.cleanup_permission_requests()
            self.cleanup_invitations()
            self.cleanup_group_invitations()

            # Delete roles and permissions
            if not skip_roles:
                self.cleanup_role_permissions()
                self.cleanup_roles()

            # Delete orders (depends on resources, projects, users, plans)
            self.cleanup_orders()

            # Delete component usages (depends on resources and components)
            self.cleanup_component_usages()

            # Delete resources (depends on offerings, plans, projects)
            self.cleanup_resources()

            # Delete marketplace components and plans
            self.cleanup_plan_components()
            self.cleanup_offering_components()
            self.cleanup_plans()

            # Delete offerings, service providers, projects, customers, categories
            self.cleanup_offerings()
            self.cleanup_service_providers()
            self.cleanup_projects()
            self.cleanup_categories()
            self.cleanup_customers()

            # Delete users last
            if not skip_users:
                self.cleanup_users()

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
            # Checklists
            ("answers", "checklist_answer"),
            ("checklist_completions", "checklist_checklistcompletion"),
            ("questions", "checklist_question"),
            ("checklists", "checklist_checklist"),
            ("checklist_categories", "checklist_category"),
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
            # Resources
            ("resources", "marketplace_resource"),
            # Marketplace components and plans
            ("plan_components", "marketplace_plancomponent"),
            ("offering_components", "marketplace_offeringcomponent"),
            ("plans", "marketplace_plan"),
            # Offerings, service providers, projects, customers, categories
            ("offerings", "marketplace_offering"),
            ("service_providers", "marketplace_serviceprovider"),
            ("projects", "structure_project"),
            ("categories", "marketplace_category"),
            ("customers", "structure_customer"),
        ]

        # Add roles if not skipped
        if not skip_roles:
            tables.append(("role_permissions", "permissions_rolepermission"))
            tables.append(("roles", "permissions_role"))

        # Add users if not skipped
        if not skip_users:
            tables.append(("users", "core_user"))

        with transaction.atomic():
            cursor = connection.cursor()

            for stat_key, table_name in tables:
                self.stdout.write(f"Deleting {stat_key}...")
                try:
                    # Get count first
                    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608
                    count = cursor.fetchone()[0]

                    if not self.dry_run:
                        # Use TRUNCATE CASCADE for speed and to handle FK dependencies
                        cursor.execute(
                            f"TRUNCATE TABLE {table_name} CASCADE"  # noqa: S608
                        )

                    self.stats[stat_key]["deleted"] = count
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(f"Failed to delete {stat_key}: {e}")
                    )
                    self.stats[stat_key]["errors"] += 1

            if self.dry_run:
                raise Exception("Dry run - rolling back transaction")

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

    def cleanup_checklist_categories(self):
        """Delete all checklist category data."""
        self.stdout.write("Deleting checklist categories...")
        try:
            if not self.dry_run:
                count = ChecklistCategory.objects.count()
                ChecklistCategory.objects.all().delete()
                self.stats["checklist_categories"]["deleted"] = count
            else:
                self.stats["checklist_categories"]["deleted"] = (
                    ChecklistCategory.objects.count()
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to delete checklist categories: {e}")
            )
            self.stats["checklist_categories"]["errors"] += 1

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
