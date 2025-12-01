import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from rest_framework.authtoken.models import Token

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
from waldur_core.permissions.models import Role, RolePermission, UserRole
from waldur_core.permissions.tasks import sync_user_deactivation_status
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.invoices.models import Invoice, InvoiceItem
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
    ResourcePlanPeriod,
    ServiceProvider,
)


class Command(BaseCommand):
    help = """
    Import comprehensive Waldur structure data from JSON format.

    This command imports a complete Waldur system structure including:
    - Users, Customers, Service Providers, Projects
    - Marketplace: Categories, Offerings, Plans, Components, Resources, Orders
    - Permissions: Roles, User Roles, Role Permissions
    - Accounts: Project/Customer Service Accounts, Course Accounts
    - Billing: Invoices, Invoice Items, Component Usages, Resource Plan Periods
    - Checklists: Categories, Checklists, Questions, Completions, Answers
    - System: Authentication Tokens, Offering Users

    The import maintains dependency order and uses transaction isolation for safety.
    RabbitMQ messages are automatically disabled during import to prevent billing issues.

    Usage:
        waldur import_structure -i structure.json
        waldur import_structure --input structure.json --update
        waldur import_structure -i structure.json --skip-users --dry-run
        waldur import_structure -i structure.json --skip-rabbitmq-messages --skip-roles
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stats = {
            "users": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "auth_tokens": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "customers": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "service_providers": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "projects": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "categories": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "offerings": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "roles": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "role_permissions": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "user_roles": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "project_service_accounts": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "customer_service_accounts": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "course_accounts": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "plans": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "offering_components": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "plan_components": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "resources": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "resource_plan_periods": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "component_usages": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "orders": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "invoices": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "invoice_items": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "offering_users": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "checklist_categories": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "checklists": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "questions": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "checklist_completions": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "answers": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
        }
        self.dry_run = False
        self.update_existing = False

    def add_arguments(self, parser):
        parser.add_argument(
            "-i",
            "--input",
            dest="input",
            type=str,
            help="Path to the input JSON file.",
            required=True,
        )
        parser.add_argument(
            "--update",
            action="store_true",
            help="Update existing objects instead of skipping them.",
        )
        parser.add_argument(
            "--skip-users",
            action="store_true",
            help="Skip importing users.",
        )
        parser.add_argument(
            "--skip-roles",
            action="store_true",
            help="Skip importing roles and role permissions.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be imported without making changes.",
        )
        parser.add_argument(
            "--skip-rabbitmq-messages",
            action="store_true",
            help="Skip sending RabbitMQ messages during import (recommended for large imports).",
        )
        parser.add_argument(
            "--skip-user-sync",
            action="store_true",
            help="Skip syncing user activation status after import.",
        )

    def handle(self, **options):
        input_path = options["input"]
        self.update_existing = options["update"]
        self.dry_run = options["dry_run"]
        skip_users = options["skip_users"]
        skip_roles = options["skip_roles"]
        self.skip_user_sync = options["skip_user_sync"]

        # Validate input file
        if not os.path.exists(input_path):
            self.stdout.write(
                self.style.ERROR(f"Input file does not exist: {input_path}")
            )
            return

        # Load data
        try:
            with open(input_path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            self.stdout.write(self.style.ERROR(f"Invalid JSON file: {e}"))
            return
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Failed to read file: {e}"))
            return

        if self.dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made")
            )

        self.stdout.write(
            self.style.WARNING(
                "IMPORT MODE - Billing and RabbitMQ messages will be disabled during import"
            )
        )

        self.stdout.write("Starting structure import...")

        try:
            # Always use skip_side_effects context to prevent side effects during import
            # This prevents ComponentUsage imports from triggering billing signals before PlanComponents are ready
            with skip_side_effects():
                self._perform_import(data, skip_users, skip_roles)

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Import failed: {e}"))
            return

        if self.dry_run:
            self.stdout.write(self.style.WARNING("Dry run completed - no changes made"))
        else:
            # After successful import, sync user activation status to match current policy
            if not self.skip_user_sync:
                self._sync_user_activation_status()
            else:
                self.stdout.write(
                    self.style.WARNING("Skipping user activation status sync")
                )

        # Print summary
        self.print_summary()

    def _perform_import(self, data, skip_users, skip_roles):
        """Perform the actual import operations."""
        # Import in dependency order, each operation in its own transaction
        # to prevent one failed import from affecting others

        if not skip_users:
            self._safe_import("users", lambda: self.import_users(data.get("users", [])))
            self._safe_import(
                "auth_tokens",
                lambda: self.import_auth_tokens(data.get("auth_tokens", [])),
            )

        self._safe_import(
            "customers", lambda: self.import_customers(data.get("customers", []))
        )
        self._safe_import(
            "service_providers",
            lambda: self.import_service_providers(data.get("service_providers", [])),
        )
        self._safe_import(
            "projects", lambda: self.import_projects(data.get("projects", []))
        )
        self._safe_import(
            "categories", lambda: self.import_categories(data.get("categories", []))
        )
        self._safe_import(
            "offerings", lambda: self.import_offerings(data.get("offerings", []))
        )

        # Import marketplace components and plans
        self._safe_import("plans", lambda: self.import_plans(data.get("plans", [])))
        self._safe_import(
            "offering_components",
            lambda: self.import_offering_components(
                data.get("offering_components", [])
            ),
        )
        self._safe_import(
            "plan_components",
            lambda: self.import_plan_components(data.get("plan_components", [])),
        )

        # Import resources (depends on offerings, plans, projects)
        self._safe_import(
            "resources", lambda: self.import_resources(data.get("resources", []))
        )

        # Import resource plan periods (depends on resources and plans)
        self._safe_import(
            "resource_plan_periods",
            lambda: self.import_resource_plan_periods(
                data.get("resource_plan_periods", [])
            ),
        )

        # Import component usages (depends on resources and components)
        self._safe_import(
            "component_usages",
            lambda: self.import_component_usages(data.get("component_usages", [])),
        )

        # Import orders (depends on resources, projects, users, plans)
        self._safe_import("orders", lambda: self.import_orders(data.get("orders", [])))

        if not skip_roles:
            self._safe_import("roles", lambda: self.import_roles(data.get("roles", [])))
            self._safe_import(
                "role_permissions",
                lambda: self.import_role_permissions(data.get("role_permissions", [])),
            )

        self._safe_import(
            "user_roles", lambda: self.import_user_roles(data.get("user_roles", []))
        )

        # Import account types
        self._safe_import(
            "project_service_accounts",
            lambda: self.import_project_service_accounts(
                data.get("project_service_accounts", [])
            ),
        )
        self._safe_import(
            "customer_service_accounts",
            lambda: self.import_customer_service_accounts(
                data.get("customer_service_accounts", [])
            ),
        )
        self._safe_import(
            "course_accounts",
            lambda: self.import_course_accounts(data.get("course_accounts", [])),
        )

        # Import invoicing (depends on customers, resources, projects)
        self._safe_import(
            "invoices", lambda: self.import_invoices(data.get("invoices", []))
        )
        self._safe_import(
            "invoice_items",
            lambda: self.import_invoice_items(data.get("invoice_items", [])),
        )

        # Import offering users (depends on offerings and users)
        self._safe_import(
            "offering_users",
            lambda: self.import_offering_users(data.get("offering_users", [])),
        )

        # Import checklist data (dependency order: categories -> checklists -> questions -> completions -> answers)
        self._safe_import(
            "checklist_categories",
            lambda: self.import_checklist_categories(
                data.get("checklist_categories", [])
            ),
        )
        self._safe_import(
            "checklists", lambda: self.import_checklists(data.get("checklists", []))
        )
        self._safe_import(
            "questions", lambda: self.import_questions(data.get("questions", []))
        )
        self._safe_import(
            "checklist_completions",
            lambda: self.import_checklist_completions(
                data.get("checklist_completions", [])
            ),
        )
        self._safe_import(
            "answers", lambda: self.import_answers(data.get("answers", []))
        )

    def _safe_import(self, import_type, import_func):
        """Safely execute an import function with proper transaction handling."""
        try:
            if self.dry_run:
                # For dry run, wrap in atomic block and rollback at the end
                with transaction.atomic():
                    import_func()
                    # Force rollback in dry run mode
                    raise Exception("Dry run - rolling back transaction")
            else:
                # For actual import, each operation in its own transaction
                with transaction.atomic():
                    import_func()
        except Exception as e:
            if self.dry_run and "Dry run - rolling back transaction" in str(e):
                # This is expected in dry run mode, don't treat as error
                pass
            else:
                # Log the error but continue with other imports
                self.stdout.write(
                    self.style.WARNING(f"Import of {import_type} failed: {e}")
                )

    def import_users(self, users_data):
        """Import user data including system_robot."""
        self.stdout.write("Importing users...")

        for user_data in users_data:
            try:
                uuid = user_data.get("uuid")
                if not uuid:
                    self.stdout.write(self.style.WARNING("Skipping user without UUID"))
                    self.stats["users"]["errors"] += 1
                    continue

                username = user_data.get("username")
                email = user_data.get("email")

                if not username:
                    self.stdout.write(
                        self.style.WARNING(f"Skipping user {uuid}: missing username")
                    )
                    self.stats["users"]["errors"] += 1
                    continue

                # Use all_objects to include inactive users to avoid unique constraint violations
                existing_user = User.all_objects.filter(uuid=uuid).first()

                # Also check if username already exists (even with different UUID)
                if not existing_user:
                    username_conflict = User.all_objects.filter(
                        username=username
                    ).first()
                    if username_conflict:
                        # For system_robot, use the existing one if UUID matches or update it
                        if username == "system_robot":
                            existing_user = username_conflict
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Using existing system_robot user with UUID {username_conflict.uuid}"
                                )
                            )
                        else:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Skipping user {uuid}: username '{username}' already exists with UUID {username_conflict.uuid}"
                                )
                            )
                            self.stats["users"]["errors"] += 1
                            continue

                if existing_user:
                    if self.update_existing:
                        # Update existing user
                        existing_user.username = username
                        existing_user.email = email
                        existing_user.first_name = user_data.get("first_name", "")
                        existing_user.last_name = user_data.get("last_name", "")
                        existing_user.native_name = user_data.get("native_name", "")
                        existing_user.phone_number = user_data.get("phone_number", "")
                        existing_user.organization = user_data.get("organization", "")
                        existing_user.job_title = user_data.get("job_title", "")
                        existing_user.description = user_data.get("description", "")
                        existing_user.is_staff = user_data.get("is_staff", False)
                        existing_user.is_support = user_data.get("is_support", False)
                        existing_user.is_active = user_data.get("is_active", True)

                        # Additional fields
                        existing_user.token_lifetime = user_data.get("token_lifetime")
                        existing_user.details = user_data.get("details", {})
                        existing_user.notifications_enabled = user_data.get(
                            "notifications_enabled", True
                        )
                        existing_user.is_identity_manager = user_data.get(
                            "is_identity_manager", False
                        )
                        existing_user.registration_method = user_data.get(
                            "registration_method", "default"
                        )
                        existing_user.identity_source = user_data.get(
                            "identity_source", ""
                        )
                        existing_user.preferred_language = user_data.get(
                            "preferred_language", ""
                        )
                        existing_user.backend_id = user_data.get("backend_id", "")
                        existing_user.affiliations = user_data.get("affiliations", [])
                        existing_user.slug = user_data.get("slug", "")
                        existing_user.query_field = user_data.get("query_field", "")
                        existing_user.is_superuser = user_data.get(
                            "is_superuser", False
                        )

                        if user_data.get("civil_number"):
                            existing_user.civil_number = user_data.get("civil_number")

                        # Parse date fields
                        if user_data.get("agreement_date"):
                            try:
                                existing_user.agreement_date = datetime.fromisoformat(
                                    user_data["agreement_date"]
                                )
                                if timezone.is_naive(existing_user.agreement_date):
                                    existing_user.agreement_date = timezone.make_aware(
                                        existing_user.agreement_date
                                    )
                            except (ValueError, TypeError):
                                pass

                        if user_data.get("birth_date"):
                            try:
                                existing_user.birth_date = datetime.fromisoformat(
                                    user_data["birth_date"]
                                ).date()
                            except (ValueError, TypeError):
                                pass

                        if not self.dry_run:
                            existing_user.save()

                        self.stats["users"]["updated"] += 1
                    else:
                        self.stats["users"]["skipped"] += 1
                else:
                    # Parse date fields
                    agreement_date = None
                    if user_data.get("agreement_date"):
                        try:
                            agreement_date = datetime.fromisoformat(
                                user_data["agreement_date"]
                            )
                            if timezone.is_naive(agreement_date):
                                agreement_date = timezone.make_aware(agreement_date)
                        except (ValueError, TypeError):
                            pass

                    birth_date = None
                    if user_data.get("birth_date"):
                        try:
                            birth_date = datetime.fromisoformat(
                                user_data["birth_date"]
                            ).date()
                        except (ValueError, TypeError):
                            pass

                    # Create new user
                    user = User(
                        uuid=uuid,
                        username=username,
                        email=email,
                        first_name=user_data.get("first_name", ""),
                        last_name=user_data.get("last_name", ""),
                        native_name=user_data.get("native_name", ""),
                        phone_number=user_data.get("phone_number", ""),
                        organization=user_data.get("organization", ""),
                        job_title=user_data.get("job_title", ""),
                        description=user_data.get("description", ""),
                        is_staff=user_data.get("is_staff", False),
                        is_support=user_data.get("is_support", False),
                        is_active=user_data.get("is_active", True),
                        # Additional fields
                        token_lifetime=user_data.get("token_lifetime"),
                        details=user_data.get("details", {}),
                        notifications_enabled=user_data.get(
                            "notifications_enabled", True
                        ),
                        is_identity_manager=user_data.get("is_identity_manager", False),
                        registration_method=user_data.get(
                            "registration_method", "default"
                        ),
                        identity_source=user_data.get("identity_source", ""),
                        preferred_language=user_data.get("preferred_language", ""),
                        backend_id=user_data.get("backend_id", ""),
                        affiliations=user_data.get("affiliations", []),
                        agreement_date=agreement_date,
                        birth_date=birth_date,
                        slug=user_data.get("slug", ""),
                        query_field=user_data.get("query_field", ""),
                        is_superuser=user_data.get("is_superuser", False),
                    )
                    if user_data.get("civil_number"):
                        user.civil_number = user_data.get("civil_number")
                    # Set unusable password for security
                    user.set_unusable_password()

                    if not self.dry_run:
                        user.save()

                    self.stats["users"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import user {user_data.get('uuid')}: {e}"
                    )
                )
                self.stats["users"]["errors"] += 1

    def import_auth_tokens(self, tokens_data):
        """Import user authentication tokens."""
        self.stdout.write("Importing auth tokens...")

        for token_data in tokens_data:
            try:
                key = token_data.get("key")
                user_uuid = token_data.get("user_uuid")

                if not key or not user_uuid:
                    self.stdout.write(
                        self.style.WARNING("Skipping token without key or user_uuid")
                    )
                    self.stats["auth_tokens"]["errors"] += 1
                    continue

                # Find user
                user = User.all_objects.filter(uuid=user_uuid).first()
                if not user:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping token {key}: user {user_uuid} not found"
                        )
                    )
                    self.stats["auth_tokens"]["errors"] += 1
                    continue

                # Parse created date
                created = None
                if token_data.get("created"):
                    try:
                        created = datetime.fromisoformat(token_data["created"])
                        if timezone.is_naive(created):
                            created = timezone.make_aware(created)
                    except (ValueError, TypeError):
                        pass

                if not self.dry_run:
                    existing_token = Token.objects.filter(key=key).first()

                    if existing_token:
                        if self.update_existing:
                            # Update existing token
                            existing_token.user = user
                            if created:
                                existing_token.created = created
                            existing_token.save()
                            self.stats["auth_tokens"]["updated"] += 1
                        else:
                            self.stats["auth_tokens"]["skipped"] += 1
                    else:
                        # Check if user already has a token
                        user_token = Token.objects.filter(user=user).first()
                        if user_token:
                            if self.update_existing:
                                # Replace existing token
                                user_token.delete()
                                token = Token(key=key, user=user)
                                if created:
                                    token.created = created
                                token.save()
                                self.stats["auth_tokens"]["updated"] += 1
                            else:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"Skipping token {key}: user already has token {user_token.key}"
                                    )
                                )
                                self.stats["auth_tokens"]["skipped"] += 1
                        else:
                            # Create new token
                            token = Token(key=key, user=user)
                            if created:
                                token.created = created
                            token.save()
                            self.stats["auth_tokens"]["created"] += 1
                else:
                    # Dry run
                    existing = Token.objects.filter(key=key).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["auth_tokens"]["updated"] += 1
                        else:
                            self.stats["auth_tokens"]["skipped"] += 1
                    else:
                        # Check for user token conflict
                        user_has_token = Token.objects.filter(user=user).exists()
                        if user_has_token:
                            if self.update_existing:
                                self.stats["auth_tokens"]["updated"] += 1
                            else:
                                self.stats["auth_tokens"]["skipped"] += 1
                        else:
                            self.stats["auth_tokens"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import auth token {token_data.get('key')}: {e}"
                    )
                )
                self.stats["auth_tokens"]["errors"] += 1

    def import_customers(self, customers_data):
        """Import customer/organization data."""
        self.stdout.write("Importing customers...")

        for customer_data in customers_data:
            try:
                uuid = customer_data.get("uuid")
                name = customer_data.get("name")

                if not uuid or not name:
                    self.stdout.write(
                        self.style.WARNING("Skipping customer without UUID or name")
                    )
                    self.stats["customers"]["errors"] += 1
                    continue

                # Parse accounting start date
                accounting_start_date = None
                if customer_data.get("accounting_start_date"):
                    try:
                        accounting_start_date = datetime.fromisoformat(
                            customer_data["accounting_start_date"]
                        )
                        if timezone.is_naive(accounting_start_date):
                            accounting_start_date = timezone.make_aware(
                                accounting_start_date
                            )
                    except (ValueError, TypeError):
                        pass

                # Parse coordinates
                latitude = None
                if customer_data.get("latitude"):
                    try:
                        latitude = Decimal(customer_data["latitude"])
                    except (ValueError, TypeError, InvalidOperation):
                        pass

                longitude = None
                if customer_data.get("longitude"):
                    try:
                        longitude = Decimal(customer_data["longitude"])
                    except (ValueError, TypeError, InvalidOperation):
                        pass

                # Parse tax percent
                default_tax_percent = None
                if customer_data.get("default_tax_percent"):
                    try:
                        default_tax_percent = Decimal(
                            customer_data["default_tax_percent"]
                        )
                    except (ValueError, TypeError, InvalidOperation):
                        pass

                defaults = {
                    "name": name,
                    "native_name": customer_data.get("native_name", ""),
                    "abbreviation": customer_data.get("abbreviation", ""),
                    "email": customer_data.get("email", ""),
                    "phone_number": customer_data.get("phone_number", ""),
                    "country": customer_data.get("country", ""),
                    "vat_code": customer_data.get("vat_code", ""),
                    "vat_name": customer_data.get("vat_name", ""),
                    "vat_address": customer_data.get("vat_address", ""),
                    "contact_details": customer_data.get("contact_details", ""),
                    "agreement_number": customer_data.get("agreement_number", ""),
                    "registration_code": customer_data.get("registration_code", ""),
                    "homepage": customer_data.get("homepage", ""),
                    "domain": customer_data.get("domain", ""),
                    "address": customer_data.get("address", ""),
                    "postal": customer_data.get("postal", ""),
                    "blocked": customer_data.get("blocked", False),
                    "archived": customer_data.get("archived", False),
                    "slug": customer_data.get("slug", ""),
                    # Enhanced fields
                    "sponsor_number": customer_data.get("sponsor_number"),
                    "access_subnets": customer_data.get("access_subnets", ""),
                    "notification_emails": customer_data.get("notification_emails", ""),
                    "display_billing_info_in_projects": customer_data.get(
                        "display_billing_info_in_projects", True
                    ),
                    "grace_period_days": customer_data.get("grace_period_days"),
                    "bank_name": customer_data.get("bank_name", ""),
                    "bank_account": customer_data.get("bank_account", ""),
                    "latitude": latitude,
                    "longitude": longitude,
                }

                # Add optional fields only if they have values
                if accounting_start_date:
                    defaults["accounting_start_date"] = accounting_start_date
                if default_tax_percent is not None:
                    defaults["default_tax_percent"] = default_tax_percent

                if not self.dry_run:
                    existing_customer = Customer.objects.filter(uuid=uuid).first()

                    if existing_customer:
                        if self.update_existing:
                            Customer.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["customers"]["updated"] += 1
                        else:
                            self.stats["customers"]["skipped"] += 1
                    else:
                        Customer.objects.create(uuid=uuid, **defaults)
                        self.stats["customers"]["created"] += 1
                else:
                    # Dry run
                    existing = Customer.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["customers"]["updated"] += 1
                        else:
                            self.stats["customers"]["skipped"] += 1
                    else:
                        self.stats["customers"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import customer {customer_data.get('uuid')}: {e}"
                    )
                )
                self.stats["customers"]["errors"] += 1

    def import_service_providers(self, service_providers_data):
        """Import service provider data."""
        self.stdout.write("Importing service providers...")

        for sp_data in service_providers_data:
            try:
                uuid = sp_data.get("uuid")
                customer_uuid = sp_data.get("customer_uuid")

                if not uuid or not customer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping service provider without UUID or customer_uuid"
                        )
                    )
                    self.stats["service_providers"]["errors"] += 1
                    continue

                # Find customer
                customer = Customer.objects.filter(uuid=customer_uuid).first()
                if not customer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping service provider {uuid}: customer {customer_uuid} not found"
                        )
                    )
                    self.stats["service_providers"]["errors"] += 1
                    continue

                defaults = {
                    "customer": customer,
                    "description": sp_data.get("description", ""),
                    "enable_notifications": sp_data.get("enable_notifications", True),
                    "api_secret_code": sp_data.get("api_secret_code"),
                    "lead_email": sp_data.get("lead_email"),
                    "lead_subject": sp_data.get("lead_subject", ""),
                    "lead_body": sp_data.get("lead_body", ""),
                }

                if not self.dry_run:
                    # Check by UUID first
                    existing_sp = ServiceProvider.objects.filter(uuid=uuid).first()

                    # Also check if customer already has a service provider
                    if not existing_sp:
                        customer_conflict = ServiceProvider.objects.filter(
                            customer=customer
                        ).first()
                        if customer_conflict:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Skipping service provider {uuid}: customer already has service provider with UUID {customer_conflict.uuid}"
                                )
                            )
                            self.stats["service_providers"]["errors"] += 1
                            continue

                    if existing_sp:
                        if self.update_existing:
                            ServiceProvider.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["service_providers"]["updated"] += 1
                        else:
                            self.stats["service_providers"]["skipped"] += 1
                    else:
                        ServiceProvider.objects.create(uuid=uuid, **defaults)
                        self.stats["service_providers"]["created"] += 1
                else:
                    # Dry run
                    existing = ServiceProvider.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["service_providers"]["updated"] += 1
                        else:
                            self.stats["service_providers"]["skipped"] += 1
                    else:
                        # Check for customer conflict in dry run
                        customer_has_sp = ServiceProvider.objects.filter(
                            customer=customer
                        ).exists()
                        if customer_has_sp:
                            self.stats["service_providers"]["errors"] += 1
                        else:
                            self.stats["service_providers"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import service provider {sp_data.get('uuid')}: {e}"
                    )
                )
                self.stats["service_providers"]["errors"] += 1

    def import_projects(self, projects_data):
        """Import project data."""
        self.stdout.write("Importing projects...")

        for project_data in projects_data:
            try:
                uuid = project_data.get("uuid")
                name = project_data.get("name")
                customer_uuid = project_data.get("customer_uuid")

                if not uuid or not name or not customer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping project without UUID, name, or customer_uuid"
                        )
                    )
                    self.stats["projects"]["errors"] += 1
                    continue

                # Find customer
                customer = Customer.objects.filter(uuid=customer_uuid).first()
                if not customer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping project {uuid}: customer {customer_uuid} not found"
                        )
                    )
                    self.stats["projects"]["errors"] += 1
                    continue

                # Parse dates
                start_date = None
                end_date = None
                if project_data.get("start_date"):
                    try:
                        start_date = datetime.fromisoformat(
                            project_data["start_date"]
                        ).date()
                    except (ValueError, TypeError):
                        pass

                if project_data.get("end_date"):
                    try:
                        end_date = datetime.fromisoformat(
                            project_data["end_date"]
                        ).date()
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "name": name,
                    "description": project_data.get("description", ""),
                    "customer": customer,
                    "start_date": start_date,
                    "end_date": end_date,
                    "kind": project_data.get("kind", ""),
                    "oecd_fos_2007_code": project_data.get("oecd_fos_2007_code", ""),
                    "slug": project_data.get("slug", ""),
                    "backend_id": project_data.get("backend_id", ""),
                }

                if not self.dry_run:
                    existing_project = Project.available_objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_project:
                        if self.update_existing:
                            Project.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["projects"]["updated"] += 1
                        else:
                            self.stats["projects"]["skipped"] += 1
                    else:
                        Project.objects.create(uuid=uuid, **defaults)
                        self.stats["projects"]["created"] += 1
                else:
                    existing = Project.available_objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["projects"]["updated"] += 1
                        else:
                            self.stats["projects"]["skipped"] += 1
                    else:
                        self.stats["projects"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import project {project_data.get('uuid')}: {e}"
                    )
                )
                self.stats["projects"]["errors"] += 1

    def import_categories(self, categories_data):
        """Import marketplace category data."""
        self.stdout.write("Importing categories...")

        for category_data in categories_data:
            try:
                uuid = category_data.get("uuid")
                title = category_data.get("title")

                if not uuid or not title:
                    self.stdout.write(
                        self.style.WARNING("Skipping category without UUID or title")
                    )
                    self.stats["categories"]["errors"] += 1
                    continue

                defaults = {
                    "title": title,
                    "description": category_data.get("description", ""),
                    "backend_id": category_data.get("backend_id", ""),
                    "default_vm_category": category_data.get(
                        "default_vm_category", False
                    ),
                    "default_volume_category": category_data.get(
                        "default_volume_category", False
                    ),
                    "default_tenant_category": category_data.get(
                        "default_tenant_category", False
                    ),
                }

                if not self.dry_run:
                    existing_category = Category.objects.filter(uuid=uuid).first()

                    if existing_category:
                        if self.update_existing:
                            Category.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["categories"]["updated"] += 1
                        else:
                            self.stats["categories"]["skipped"] += 1
                    else:
                        Category.objects.create(uuid=uuid, **defaults)
                        self.stats["categories"]["created"] += 1
                else:
                    existing = Category.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["categories"]["updated"] += 1
                        else:
                            self.stats["categories"]["skipped"] += 1
                    else:
                        self.stats["categories"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import category {category_data.get('uuid')}: {e}"
                    )
                )
                self.stats["categories"]["errors"] += 1

    def import_offerings(self, offerings_data):
        """Import marketplace offering data."""
        self.stdout.write("Importing offerings...")

        for offering_data in offerings_data:
            try:
                uuid = offering_data.get("uuid")
                name = offering_data.get("name")

                if not uuid or not name:
                    self.stdout.write(
                        self.style.WARNING("Skipping offering without UUID or name")
                    )
                    self.stats["offerings"]["errors"] += 1
                    continue

                # Resolve customer
                customer = None
                customer_uuid = offering_data.get("customer_uuid")
                if customer_uuid:
                    customer = Customer.objects.filter(uuid=customer_uuid).first()
                    if not customer:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping offering {uuid}: customer {customer_uuid} not found"
                            )
                        )
                        self.stats["offerings"]["errors"] += 1
                        continue

                # Resolve category reference
                category = None
                category_uuid = offering_data.get("category_uuid")
                if category_uuid:
                    category = Category.objects.filter(uuid=category_uuid).first()
                    if not category:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping offering {uuid}: category {category_uuid} not found"
                            )
                        )
                        self.stats["offerings"]["errors"] += 1
                        continue

                # Resolve parent offering reference (optional)
                parent = None
                parent_uuid = offering_data.get("parent_uuid")
                if parent_uuid:
                    parent = Offering.objects.filter(uuid=parent_uuid).first()

                # Resolve project reference (optional)
                project = None
                project_uuid = offering_data.get("project_uuid")
                if project_uuid:
                    project = Project.available_objects.filter(
                        uuid=project_uuid
                    ).first()

                # Resolve compliance checklist reference (optional)
                compliance_checklist = None
                compliance_checklist_uuid = offering_data.get(
                    "compliance_checklist_uuid"
                )
                if compliance_checklist_uuid:
                    compliance_checklist = Checklist.objects.filter(
                        uuid=compliance_checklist_uuid
                    ).first()

                defaults = {
                    "name": name,
                    "description": offering_data.get("description", ""),
                    "type": offering_data.get("type", ""),
                    "state": offering_data.get("state", 1),
                    "customer": customer,
                    "shared": offering_data.get("shared", False),
                    "billable": offering_data.get("billable", True),
                    "attributes": offering_data.get("attributes", {}),
                    "options": offering_data.get("options", {}),
                    "resource_options": offering_data.get("resource_options", {}),
                    "plugin_options": offering_data.get("plugin_options", {}),
                    "slug": offering_data.get("slug", ""),
                    # Additional fields
                    "backend_id": offering_data.get("backend_id", ""),
                    "full_description": offering_data.get("full_description", ""),
                    "vendor_details": offering_data.get("vendor_details", ""),
                    "getting_started": offering_data.get("getting_started", ""),
                    "integration_guide": offering_data.get("integration_guide", ""),
                    "privacy_policy_link": offering_data.get("privacy_policy_link", ""),
                    "access_url": offering_data.get("access_url", ""),
                    "country": offering_data.get("country", ""),
                    "paused_reason": offering_data.get("paused_reason", ""),
                    "secret_options": offering_data.get("secret_options", {}),
                    "support_per_user_consumption_limitation": offering_data.get(
                        "support_per_user_consumption_limitation", False
                    ),
                    "parent": parent,
                    "project": project,
                    "compliance_checklist": compliance_checklist,
                }

                if category:
                    defaults["category"] = category

                if not self.dry_run:
                    existing_offering = Offering.objects.filter(uuid=uuid).first()

                    if existing_offering:
                        if self.update_existing:
                            Offering.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["offerings"]["updated"] += 1
                        else:
                            self.stats["offerings"]["skipped"] += 1
                    else:
                        Offering.objects.create(uuid=uuid, **defaults)
                        self.stats["offerings"]["created"] += 1
                else:
                    existing = Offering.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["offerings"]["updated"] += 1
                        else:
                            self.stats["offerings"]["skipped"] += 1
                    else:
                        self.stats["offerings"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import offering {offering_data.get('uuid')}: {e}"
                    )
                )
                self.stats["offerings"]["errors"] += 1

    def import_roles(self, roles_data):
        """Import role definitions."""
        self.stdout.write("Importing roles...")

        for role_data in roles_data:
            try:
                uuid = role_data.get("uuid")
                name = role_data.get("name")
                content_type_str = role_data.get("content_type")

                if not uuid or not name or not content_type_str:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping role without UUID, name, or content_type"
                        )
                    )
                    self.stats["roles"]["errors"] += 1
                    continue

                # Parse content type
                try:
                    app_label, model = content_type_str.split(".")
                    content_type = ContentType.objects.get(
                        app_label=app_label, model=model
                    )
                except (ValueError, ContentType.DoesNotExist):
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping role {name}: invalid content_type {content_type_str}"
                        )
                    )
                    self.stats["roles"]["errors"] += 1
                    continue

                defaults = {
                    "name": name,
                    "description": role_data.get("description", ""),
                    "is_system_role": role_data.get("is_system_role", False),
                    "is_active": role_data.get("is_active", True),
                    "content_type": content_type,
                }

                if not self.dry_run:
                    # Check by UUID first
                    existing_role = Role.objects.filter(uuid=uuid).first()

                    # Also check if name already exists with different UUID
                    if not existing_role:
                        name_conflict = Role.objects.filter(name=name).first()
                        if name_conflict:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Skipping role {uuid}: name '{name}' already exists with UUID {name_conflict.uuid}"
                                )
                            )
                            self.stats["roles"]["errors"] += 1
                            continue

                    if existing_role:
                        if self.update_existing:
                            Role.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["roles"]["updated"] += 1
                        else:
                            self.stats["roles"]["skipped"] += 1
                    else:
                        Role.objects.create(uuid=uuid, **defaults)
                        self.stats["roles"]["created"] += 1
                else:
                    # Dry run
                    existing = Role.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["roles"]["updated"] += 1
                        else:
                            self.stats["roles"]["skipped"] += 1
                    else:
                        # Check for name conflict in dry run
                        name_exists = Role.objects.filter(name=name).exists()
                        if name_exists:
                            self.stats["roles"]["errors"] += 1
                        else:
                            self.stats["roles"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import role {role_data.get('name')}: {e}"
                    )
                )
                self.stats["roles"]["errors"] += 1

    def import_role_permissions(self, role_permissions_data):
        """Import role permission mappings."""
        self.stdout.write("Importing role permissions...")

        # Group permissions by role
        role_perms = {}
        for perm_data in role_permissions_data:
            role_name = perm_data.get("role_name")
            permission = perm_data.get("permission")

            if not role_name or not permission:
                continue

            if role_name not in role_perms:
                role_perms[role_name] = []
            role_perms[role_name].append(permission)

        # Import permissions for each role
        for role_name, permissions in role_perms.items():
            try:
                role = Role.objects.filter(name=role_name).first()
                if not role:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping permissions for role {role_name}: role not found"
                        )
                    )
                    self.stats["role_permissions"]["errors"] += len(permissions)
                    continue

                if not self.dry_run:
                    # Get current permissions
                    current_permissions = set(
                        RolePermission.objects.filter(role=role).values_list(
                            "permission", flat=True
                        )
                    )
                    new_permissions = set(permissions)

                    # Remove permissions not in new set
                    RolePermission.objects.filter(
                        role=role, permission__in=current_permissions - new_permissions
                    ).delete()

                    # Add new permissions
                    for permission in new_permissions - current_permissions:
                        RolePermission.objects.create(role=role, permission=permission)
                        self.stats["role_permissions"]["created"] += 1

                    # Count unchanged
                    self.stats["role_permissions"]["skipped"] += len(
                        current_permissions & new_permissions
                    )
                else:
                    self.stats["role_permissions"]["created"] += len(permissions)

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import permissions for role {role_name}: {e}"
                    )
                )
                self.stats["role_permissions"]["errors"] += len(permissions)

    def import_user_roles(self, user_roles_data):
        """Import user role assignments."""
        self.stdout.write("Importing user roles...")

        for user_role_data in user_roles_data:
            try:
                uuid = user_role_data.get("uuid")
                user_uuid = user_role_data.get("user_uuid")
                role_uuid = user_role_data.get("role_uuid")
                scope_type = user_role_data.get("scope_type")
                scope_uuid = user_role_data.get("scope_uuid")

                if not uuid or not user_uuid or not role_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping user role without UUID, user_uuid, or role_uuid"
                        )
                    )
                    self.stats["user_roles"]["errors"] += 1
                    continue

                # Find user
                user = User.all_objects.filter(uuid=user_uuid).first()
                if not user:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping user role {uuid}: user {user_uuid} not found"
                        )
                    )
                    self.stats["user_roles"]["errors"] += 1
                    continue

                # Find role
                role = Role.objects.filter(uuid=role_uuid).first()
                if not role:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping user role {uuid}: role {role_uuid} not found"
                        )
                    )
                    self.stats["user_roles"]["errors"] += 1
                    continue

                # Resolve scope
                content_type = None
                object_id = None

                if scope_type and scope_uuid:
                    try:
                        app_label, model = scope_type.split(".")
                        content_type = ContentType.objects.get(
                            app_label=app_label, model=model
                        )
                        # Get the model class and find the object by UUID
                        model_class = content_type.model_class()
                        if not model_class:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Skipping user role {uuid}: could not get model class for {scope_type}"
                                )
                            )
                            self.stats["user_roles"]["errors"] += 1
                            continue
                        scope_object = model_class.objects.filter(
                            uuid=scope_uuid
                        ).first()
                        if not scope_object:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Skipping user role {uuid}: scope object {scope_uuid} not found"
                                )
                            )
                            self.stats["user_roles"]["errors"] += 1
                            continue
                        # Use the id field instead of UUID
                        object_id = scope_object.id  # type: ignore[attr-defined]
                    except (ValueError, ContentType.DoesNotExist):
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping user role {uuid}: invalid scope_type {scope_type}"
                            )
                        )
                        self.stats["user_roles"]["errors"] += 1
                        continue

                # Parse expiration time
                expiration_time = None
                if user_role_data.get("expiration_time"):
                    try:
                        expiration_time = datetime.fromisoformat(
                            user_role_data["expiration_time"]
                        )
                        if timezone.is_naive(expiration_time):
                            expiration_time = timezone.make_aware(expiration_time)
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "user": user,
                    "role": role,
                    "content_type": content_type,
                    "object_id": object_id,
                    "expiration_time": expiration_time,
                    "is_active": user_role_data.get("is_active", True),
                }

                if not self.dry_run:
                    # Check if already exists
                    existing = UserRole.objects.filter(uuid=uuid).first()

                    if existing:
                        if self.update_existing:
                            for key, value in defaults.items():
                                setattr(existing, key, value)
                            existing.save()
                            self.stats["user_roles"]["updated"] += 1
                        else:
                            self.stats["user_roles"]["skipped"] += 1
                    else:
                        UserRole.objects.create(uuid=uuid, **defaults)
                        self.stats["user_roles"]["created"] += 1
                else:
                    existing = UserRole.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["user_roles"]["updated"] += 1
                        else:
                            self.stats["user_roles"]["skipped"] += 1
                    else:
                        self.stats["user_roles"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import user role {user_role_data.get('uuid')}: {e}"
                    )
                )
                self.stats["user_roles"]["errors"] += 1

    def import_project_service_accounts(self, accounts_data):
        """Import project service account data."""
        self.stdout.write("Importing project service accounts...")

        for account_data in accounts_data:
            try:
                uuid = account_data.get("uuid")
                project_uuid = account_data.get("project_uuid")

                if not uuid or not project_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping project service account without UUID or project_uuid"
                        )
                    )
                    self.stats["project_service_accounts"]["errors"] += 1
                    continue

                # Find project
                project = Project.available_objects.filter(uuid=project_uuid).first()
                if not project:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping project service account {uuid}: project {project_uuid} not found"
                        )
                    )
                    self.stats["project_service_accounts"]["errors"] += 1
                    continue

                defaults = {
                    "username": account_data.get("username", ""),
                    "email": account_data.get("email", ""),
                    "preferred_identifier": account_data.get(
                        "preferred_identifier", ""
                    ),
                    "description": account_data.get("description", ""),
                    "state": account_data.get("state", 1),
                    "project": project,
                }

                if not self.dry_run:
                    existing_account = ProjectServiceAccount.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_account:
                        if self.update_existing:
                            ProjectServiceAccount.objects.filter(uuid=uuid).update(
                                **defaults
                            )
                            self.stats["project_service_accounts"]["updated"] += 1
                        else:
                            self.stats["project_service_accounts"]["skipped"] += 1
                    else:
                        ProjectServiceAccount.objects.create(
                            uuid=UUID(uuid), **defaults
                        )
                        self.stats["project_service_accounts"]["created"] += 1
                else:
                    existing = ProjectServiceAccount.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["project_service_accounts"]["updated"] += 1
                        else:
                            self.stats["project_service_accounts"]["skipped"] += 1
                    else:
                        self.stats["project_service_accounts"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import project service account {account_data.get('uuid')}: {e}"
                    )
                )
                self.stats["project_service_accounts"]["errors"] += 1

    def import_customer_service_accounts(self, accounts_data):
        """Import customer service account data."""
        self.stdout.write("Importing customer service accounts...")

        for account_data in accounts_data:
            try:
                uuid = account_data.get("uuid")
                customer_uuid = account_data.get("customer_uuid")

                if not uuid or not customer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping customer service account without UUID or customer_uuid"
                        )
                    )
                    self.stats["customer_service_accounts"]["errors"] += 1
                    continue

                # Find customer
                customer = Customer.objects.filter(uuid=customer_uuid).first()
                if not customer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping customer service account {uuid}: customer {customer_uuid} not found"
                        )
                    )
                    self.stats["customer_service_accounts"]["errors"] += 1
                    continue

                defaults = {
                    "username": account_data.get("username", ""),
                    "email": account_data.get("email", ""),
                    "preferred_identifier": account_data.get(
                        "preferred_identifier", ""
                    ),
                    "description": account_data.get("description", ""),
                    "state": account_data.get("state", 1),
                    "customer": customer,
                }

                if not self.dry_run:
                    existing_account = CustomerServiceAccount.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_account:
                        if self.update_existing:
                            CustomerServiceAccount.objects.filter(uuid=uuid).update(
                                **defaults
                            )
                            self.stats["customer_service_accounts"]["updated"] += 1
                        else:
                            self.stats["customer_service_accounts"]["skipped"] += 1
                    else:
                        CustomerServiceAccount.objects.create(uuid=uuid, **defaults)
                        self.stats["customer_service_accounts"]["created"] += 1
                else:
                    existing = CustomerServiceAccount.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["customer_service_accounts"]["updated"] += 1
                        else:
                            self.stats["customer_service_accounts"]["skipped"] += 1
                    else:
                        self.stats["customer_service_accounts"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import customer service account {account_data.get('uuid')}: {e}"
                    )
                )
                self.stats["customer_service_accounts"]["errors"] += 1

    def import_course_accounts(self, accounts_data):
        """Import course account data."""
        self.stdout.write("Importing course accounts...")

        for account_data in accounts_data:
            try:
                uuid = account_data.get("uuid")
                project_uuid = account_data.get("project_uuid")

                if not uuid or not project_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping course account without UUID or project_uuid"
                        )
                    )
                    self.stats["course_accounts"]["errors"] += 1
                    continue

                # Find project
                project = Project.available_objects.filter(uuid=project_uuid).first()
                if not project:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping course account {uuid}: project {project_uuid} not found"
                        )
                    )
                    self.stats["course_accounts"]["errors"] += 1
                    continue

                # Find user (optional)
                user = None
                user_uuid = account_data.get("user_uuid")
                if user_uuid:
                    user = User.all_objects.filter(uuid=user_uuid).first()
                    if not user:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping course account {uuid}: user {user_uuid} not found"
                            )
                        )
                        self.stats["course_accounts"]["errors"] += 1
                        continue

                defaults = {
                    "email": account_data.get("email", ""),
                    "description": account_data.get("description", ""),
                    "state": account_data.get("state", 1),
                    "project": project,
                    "user": user,
                    "error_message": account_data.get("error_message", ""),
                }

                if not self.dry_run:
                    existing_account = CourseAccount.objects.filter(uuid=uuid).first()

                    if existing_account:
                        if self.update_existing:
                            CourseAccount.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["course_accounts"]["updated"] += 1
                        else:
                            self.stats["course_accounts"]["skipped"] += 1
                    else:
                        CourseAccount.objects.create(uuid=UUID(uuid), **defaults)
                        self.stats["course_accounts"]["created"] += 1
                else:
                    existing = CourseAccount.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["course_accounts"]["updated"] += 1
                        else:
                            self.stats["course_accounts"]["skipped"] += 1
                    else:
                        self.stats["course_accounts"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import course account {account_data.get('uuid')}: {e}"
                    )
                )
                self.stats["course_accounts"]["errors"] += 1

    def import_plans(self, plans_data):
        """Import plan data."""
        self.stdout.write("Importing plans...")

        for plan_data in plans_data:
            try:
                uuid = plan_data.get("uuid")
                offering_uuid = plan_data.get("offering_uuid")
                name = plan_data.get("name")

                if not uuid or not offering_uuid or not name:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping plan without UUID, offering_uuid, or name"
                        )
                    )
                    self.stats["plans"]["errors"] += 1
                    continue

                # Find offering
                offering = Offering.objects.filter(uuid=offering_uuid).first()
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping plan {uuid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["plans"]["errors"] += 1
                    continue

                defaults = {
                    "name": name,
                    "description": plan_data.get("description", ""),
                    "offering": offering,
                    "unit_price": plan_data.get("unit_price", 0),
                    "unit": plan_data.get("unit", ""),
                    "archived": plan_data.get("archived", False),
                    "max_amount": plan_data.get("max_amount"),
                    "article_code": plan_data.get("article_code", ""),
                    "backend_id": plan_data.get("backend_id", ""),
                }

                if not self.dry_run:
                    existing_plan = Plan.objects.filter(uuid=uuid).first()

                    if existing_plan:
                        if self.update_existing:
                            Plan.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["plans"]["updated"] += 1
                        else:
                            self.stats["plans"]["skipped"] += 1
                    else:
                        Plan.objects.create(uuid=uuid, **defaults)
                        self.stats["plans"]["created"] += 1
                else:
                    existing = Plan.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["plans"]["updated"] += 1
                        else:
                            self.stats["plans"]["skipped"] += 1
                    else:
                        self.stats["plans"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import plan {plan_data.get('uuid')}: {e}"
                    )
                )
                self.stats["plans"]["errors"] += 1

    def import_offering_components(self, components_data):
        """Import offering component data."""
        self.stdout.write("Importing offering components...")

        for component_data in components_data:
            try:
                uuid = component_data.get("uuid")
                offering_uuid = component_data.get("offering_uuid")
                component_type = component_data.get("type")

                if not uuid or not offering_uuid or not component_type:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping offering component without UUID, offering_uuid, or type"
                        )
                    )
                    self.stats["offering_components"]["errors"] += 1
                    continue

                # Find offering
                offering = Offering.objects.filter(uuid=offering_uuid).first()
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping offering component {uuid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["offering_components"]["errors"] += 1
                    continue

                defaults = {
                    "offering": offering,
                    "type": component_type,
                    "name": component_data.get("name", ""),
                    "description": component_data.get("description", ""),
                    "billing_type": component_data.get("billing_type", "fixed"),
                    "measured_unit": component_data.get("measured_unit", ""),
                    "limit_period": component_data.get("limit_period"),
                    "limit_amount": component_data.get("limit_amount"),
                    "article_code": component_data.get("article_code", ""),
                    "backend_id": component_data.get("backend_id", ""),
                }

                if not self.dry_run:
                    existing_component = OfferingComponent.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_component:
                        if self.update_existing:
                            OfferingComponent.objects.filter(uuid=uuid).update(
                                **defaults
                            )
                            self.stats["offering_components"]["updated"] += 1
                        else:
                            self.stats["offering_components"]["skipped"] += 1
                    else:
                        OfferingComponent.objects.create(uuid=uuid, **defaults)
                        self.stats["offering_components"]["created"] += 1
                else:
                    existing = OfferingComponent.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["offering_components"]["updated"] += 1
                        else:
                            self.stats["offering_components"]["skipped"] += 1
                    else:
                        self.stats["offering_components"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import offering component {component_data.get('uuid')}: {e}"
                    )
                )
                self.stats["offering_components"]["errors"] += 1

    def import_plan_components(self, plan_components_data):
        """Import plan component data."""
        self.stdout.write("Importing plan components...")

        for pc_data in plan_components_data:
            try:
                plan_uuid = pc_data.get("plan_uuid")
                component_uuid = pc_data.get("component_uuid")

                if not plan_uuid or not component_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping plan component without plan_uuid or component_uuid"
                        )
                    )
                    self.stats["plan_components"]["errors"] += 1
                    continue

                # Find plan
                plan = Plan.objects.filter(uuid=plan_uuid).first()
                if not plan:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping plan component: plan {plan_uuid} not found"
                        )
                    )
                    self.stats["plan_components"]["errors"] += 1
                    continue

                # Find component
                component = OfferingComponent.objects.filter(
                    uuid=component_uuid
                ).first()
                if not component:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping plan component: component {component_uuid} not found"
                        )
                    )
                    self.stats["plan_components"]["errors"] += 1
                    continue

                defaults = {
                    "amount": pc_data.get("amount", 0),
                    "price": pc_data.get("price", 0),
                    "future_price": pc_data.get("future_price"),
                }

                if not self.dry_run:
                    existing_pc = PlanComponent.objects.filter(
                        plan=plan, component=component
                    ).first()

                    if existing_pc:
                        if self.update_existing:
                            PlanComponent.objects.filter(
                                plan=plan, component=component
                            ).update(**defaults)
                            self.stats["plan_components"]["updated"] += 1
                        else:
                            self.stats["plan_components"]["skipped"] += 1
                    else:
                        PlanComponent.objects.create(
                            plan=plan, component=component, **defaults
                        )
                        self.stats["plan_components"]["created"] += 1
                else:
                    existing = PlanComponent.objects.filter(
                        plan=plan, component=component
                    ).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["plan_components"]["updated"] += 1
                        else:
                            self.stats["plan_components"]["skipped"] += 1
                    else:
                        self.stats["plan_components"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import plan component for plan {pc_data.get('plan_uuid')}: {e}"
                    )
                )
                self.stats["plan_components"]["errors"] += 1

    def import_resources(self, resources_data):
        """Import marketplace resource data."""
        self.stdout.write("Importing resources...")

        for resource_data in resources_data:
            try:
                uuid = resource_data.get("uuid")
                offering_uuid = resource_data.get("offering_uuid")
                project_uuid = resource_data.get("project_uuid")

                if not uuid or not offering_uuid or not project_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping resource without UUID, offering_uuid, or project_uuid"
                        )
                    )
                    self.stats["resources"]["errors"] += 1
                    continue

                # Find offering
                offering = Offering.objects.filter(uuid=offering_uuid).first()
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping resource {uuid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["resources"]["errors"] += 1
                    continue

                # Find project
                project = Project.available_objects.filter(uuid=project_uuid).first()
                if not project:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping resource {uuid}: project {project_uuid} not found"
                        )
                    )
                    self.stats["resources"]["errors"] += 1
                    continue

                # Find plan (optional)
                plan = None
                plan_uuid = resource_data.get("plan_uuid")
                if plan_uuid:
                    plan = Plan.objects.filter(uuid=plan_uuid).first()

                defaults = {
                    "name": resource_data.get("name", ""),
                    "state": resource_data.get("state", 1),
                    "offering": offering,
                    "plan": plan,
                    "project": project,
                    "attributes": resource_data.get("attributes", {}),
                    "limits": resource_data.get("limits", {}),
                    "options": resource_data.get("options", {}),
                    "backend_id": resource_data.get("backend_id", ""),
                    "effective_id": resource_data.get("effective_id", ""),
                    "description": resource_data.get("description", ""),
                    "slug": resource_data.get("slug", ""),
                }

                if not self.dry_run:
                    existing_resource = Resource.objects.filter(uuid=uuid).first()

                    if existing_resource:
                        if self.update_existing:
                            Resource.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["resources"]["updated"] += 1
                        else:
                            self.stats["resources"]["skipped"] += 1
                    else:
                        Resource.objects.create(uuid=uuid, **defaults)
                        self.stats["resources"]["created"] += 1
                else:
                    existing = Resource.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["resources"]["updated"] += 1
                        else:
                            self.stats["resources"]["skipped"] += 1
                    else:
                        self.stats["resources"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import resource {resource_data.get('uuid')}: {e}"
                    )
                )
                self.stats["resources"]["errors"] += 1

    def import_resource_plan_periods(self, periods_data):
        """Import resource plan period data."""

        self.stdout.write("Importing resource plan periods...")

        for period_data in periods_data:
            try:
                uuid = period_data.get("uuid")
                resource_uuid = period_data.get("resource_uuid")
                plan_uuid = period_data.get("plan_uuid")

                if not uuid or not resource_uuid or not plan_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping resource plan period without UUID, resource_uuid, or plan_uuid"
                        )
                    )
                    self.stats["resource_plan_periods"]["errors"] += 1
                    continue

                # Find resource
                resource = Resource.objects.filter(uuid=resource_uuid).first()
                if not resource:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping resource plan period {uuid}: resource {resource_uuid} not found"
                        )
                    )
                    self.stats["resource_plan_periods"]["errors"] += 1
                    continue

                # Find plan
                plan = Plan.objects.filter(uuid=plan_uuid).first()
                if not plan:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping resource plan period {uuid}: plan {plan_uuid} not found"
                        )
                    )
                    self.stats["resource_plan_periods"]["errors"] += 1
                    continue

                # Parse dates
                start = None
                if period_data.get("start"):
                    try:
                        start = datetime.fromisoformat(period_data["start"])
                        if timezone.is_naive(start):
                            start = timezone.make_aware(start)
                    except (ValueError, TypeError):
                        pass

                end = None
                if period_data.get("end"):
                    try:
                        end = datetime.fromisoformat(period_data["end"])
                        if timezone.is_naive(end):
                            end = timezone.make_aware(end)
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "resource": resource,
                    "plan": plan,
                    "start": start,
                    "end": end,
                }

                if not self.dry_run:
                    existing_period = ResourcePlanPeriod.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_period:
                        if self.update_existing:
                            ResourcePlanPeriod.objects.filter(uuid=uuid).update(
                                **defaults
                            )
                            self.stats["resource_plan_periods"]["updated"] += 1
                        else:
                            self.stats["resource_plan_periods"]["skipped"] += 1
                    else:
                        ResourcePlanPeriod.objects.create(uuid=uuid, **defaults)
                        self.stats["resource_plan_periods"]["created"] += 1
                else:
                    existing = ResourcePlanPeriod.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["resource_plan_periods"]["updated"] += 1
                        else:
                            self.stats["resource_plan_periods"]["skipped"] += 1
                    else:
                        self.stats["resource_plan_periods"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"Failed to import resource plan period {period_data.get('uuid')}: {e}"
                    )
                )
                self.stats["resource_plan_periods"]["errors"] += 1

    def import_component_usages(self, usages_data):
        """Import component usage data."""
        self.stdout.write("Importing component usages...")

        for usage_data in usages_data:
            try:
                uuid = usage_data.get("uuid")
                resource_uuid = usage_data.get("resource_uuid")
                component_uuid = usage_data.get("component_uuid")

                if not uuid or not resource_uuid or not component_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping component usage without UUID, resource_uuid, or component_uuid"
                        )
                    )
                    self.stats["component_usages"]["errors"] += 1
                    continue

                # Find resource
                resource = Resource.objects.filter(uuid=resource_uuid).first()
                if not resource:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping component usage {uuid}: resource {resource_uuid} not found"
                        )
                    )
                    self.stats["component_usages"]["errors"] += 1
                    continue

                # Find component
                component = OfferingComponent.objects.filter(
                    uuid=component_uuid
                ).first()
                if not component:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping component usage {uuid}: component {component_uuid} not found"
                        )
                    )
                    self.stats["component_usages"]["errors"] += 1
                    continue

                # Parse dates
                date = None
                if usage_data.get("date"):
                    try:
                        date = datetime.fromisoformat(usage_data["date"])
                        if timezone.is_naive(date):
                            date = timezone.make_aware(date)
                    except (ValueError, TypeError):
                        pass

                billing_period = None
                if usage_data.get("billing_period"):
                    try:
                        billing_period = datetime.fromisoformat(
                            usage_data["billing_period"]
                        ).date()
                    except (ValueError, TypeError):
                        pass

                # Find plan_period if provided
                plan_period = None
                plan_period_uuid = usage_data.get("plan_period")
                if plan_period_uuid:
                    plan_period = ResourcePlanPeriod.objects.filter(
                        uuid=plan_period_uuid
                    ).first()
                    if not plan_period:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Plan period {plan_period_uuid} not found for component usage {uuid}"
                            )
                        )

                defaults = {
                    "resource": resource,
                    "component": component,
                    "usage": usage_data.get("usage", 0),
                    "date": date or timezone.now(),
                    "billing_period": billing_period or timezone.now().date(),
                    "plan_period": plan_period,
                    "recurring": usage_data.get("recurring", False),
                    "description": usage_data.get("description", ""),
                    "backend_id": usage_data.get("backend_id", ""),
                }

                if not self.dry_run:
                    existing_usage = ComponentUsage.objects.filter(uuid=uuid).first()

                    if existing_usage:
                        if self.update_existing:
                            ComponentUsage.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["component_usages"]["updated"] += 1
                        else:
                            self.stats["component_usages"]["skipped"] += 1
                    else:
                        # Check if a record with the same business key exists
                        # (unique constraint on resource, component, billing_period when plan_period is NULL)
                        duplicate_usage = ComponentUsage.objects.filter(
                            resource=resource,
                            component=component,
                            billing_period=billing_period or timezone.now().date(),
                            plan_period__isnull=True,
                        ).first()

                        if duplicate_usage:
                            if self.update_existing:
                                # Update the existing record with the new UUID and data
                                ComponentUsage.objects.filter(
                                    pk=duplicate_usage.pk
                                ).update(uuid=uuid, **defaults)
                                self.stats["component_usages"]["updated"] += 1
                            else:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"Skipping component usage {uuid}: duplicate exists with UUID {duplicate_usage.uuid}"
                                    )
                                )
                                self.stats["component_usages"]["skipped"] += 1
                        else:
                            ComponentUsage.objects.create(uuid=uuid, **defaults)
                            self.stats["component_usages"]["created"] += 1
                else:
                    existing = ComponentUsage.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["component_usages"]["updated"] += 1
                        else:
                            self.stats["component_usages"]["skipped"] += 1
                    else:
                        # Check for duplicate by business key
                        duplicate_exists = ComponentUsage.objects.filter(
                            resource=resource,
                            component=component,
                            billing_period=billing_period or timezone.now().date(),
                            plan_period__isnull=True,
                        ).exists()

                        if duplicate_exists:
                            if self.update_existing:
                                self.stats["component_usages"]["updated"] += 1
                            else:
                                self.stats["component_usages"]["skipped"] += 1
                        else:
                            self.stats["component_usages"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import component usage {usage_data.get('uuid')}: {e}"
                    )
                )
                self.stats["component_usages"]["errors"] += 1

    def import_invoices(self, invoices_data):
        """Import invoice data."""
        self.stdout.write("Importing invoices...")

        for invoice_data in invoices_data:
            try:
                uuid = invoice_data.get("uuid")
                customer_uuid = invoice_data.get("customer_uuid")
                month = invoice_data.get("month")
                year = invoice_data.get("year")

                if not uuid or not customer_uuid or not month or not year:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping invoice without UUID, customer_uuid, month, or year"
                        )
                    )
                    self.stats["invoices"]["errors"] += 1
                    continue

                # Find customer
                customer = Customer.objects.filter(uuid=customer_uuid).first()
                if not customer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping invoice {uuid}: customer {customer_uuid} not found"
                        )
                    )
                    self.stats["invoices"]["errors"] += 1
                    continue

                # Parse invoice_date
                invoice_date = None
                if invoice_data.get("invoice_date"):
                    try:
                        invoice_date = datetime.fromisoformat(
                            invoice_data["invoice_date"]
                        ).date()
                    except (ValueError, TypeError):
                        pass

                # Parse created date
                created = None
                if invoice_data.get("created"):
                    try:
                        created = datetime.fromisoformat(invoice_data["created"]).date()
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "customer": customer,
                    "month": month,
                    "year": year,
                    "state": invoice_data.get("state", "pending"),
                    "total_cost": invoice_data.get("total_cost", 0),
                    "total_price": invoice_data.get("total_price", 0),
                    "tax_percent": invoice_data.get("tax_percent", 0),
                    "invoice_date": invoice_date,
                    "created": created,
                }

                if not self.dry_run:
                    existing_invoice = Invoice.objects.filter(uuid=uuid).first()

                    if existing_invoice:
                        if self.update_existing:
                            Invoice.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["invoices"]["updated"] += 1
                        else:
                            self.stats["invoices"]["skipped"] += 1
                    else:
                        Invoice.objects.create(uuid=uuid, **defaults)
                        self.stats["invoices"]["created"] += 1
                else:
                    existing = Invoice.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["invoices"]["updated"] += 1
                        else:
                            self.stats["invoices"]["skipped"] += 1
                    else:
                        self.stats["invoices"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import invoice {invoice_data.get('uuid')}: {e}"
                    )
                )
                self.stats["invoices"]["errors"] += 1

    def import_invoice_items(self, invoice_items_data):
        """Import invoice item data."""
        self.stdout.write("Importing invoice items...")

        for item_data in invoice_items_data:
            try:
                uuid = item_data.get("uuid")
                invoice_uuid = item_data.get("invoice_uuid")

                if not uuid or not invoice_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping invoice item without UUID or invoice_uuid"
                        )
                    )
                    self.stats["invoice_items"]["errors"] += 1
                    continue

                # Find invoice
                invoice = Invoice.objects.filter(uuid=invoice_uuid).first()
                if not invoice:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping invoice item {uuid}: invoice {invoice_uuid} not found"
                        )
                    )
                    self.stats["invoice_items"]["errors"] += 1
                    continue

                # Find resource (optional)
                resource = None
                resource_uuid = item_data.get("resource_uuid")
                if resource_uuid:
                    resource = Resource.objects.filter(uuid=resource_uuid).first()

                # Find project (optional)
                project = None
                project_uuid = item_data.get("project_uuid")
                if project_uuid:
                    project = Project.available_objects.filter(
                        uuid=project_uuid
                    ).first()

                # Parse dates
                start = None
                if item_data.get("start"):
                    try:
                        start = datetime.fromisoformat(item_data["start"])
                        if timezone.is_naive(start):
                            start = timezone.make_aware(start)
                    except (ValueError, TypeError):
                        pass

                end = None
                if item_data.get("end"):
                    try:
                        end = datetime.fromisoformat(item_data["end"])
                        if timezone.is_naive(end):
                            end = timezone.make_aware(end)
                    except (ValueError, TypeError):
                        pass

                # Find plan component (optional)
                plan_component = None
                plan_component_id = item_data.get("plan_component")
                if plan_component_id:
                    plan_component = PlanComponent.objects.filter(
                        id=plan_component_id
                    ).first()

                # Parse backend_uuid
                backend_uuid = None
                if item_data.get("backend_uuid"):
                    try:
                        backend_uuid = UUID(item_data["backend_uuid"])
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "invoice": invoice,
                    "resource": resource,
                    "project": project,
                    "name": item_data.get("name", ""),
                    "quantity": item_data.get("quantity", 0),
                    "measured_unit": item_data.get("measured_unit", ""),
                    "unit_price": item_data.get("unit_price", 0),
                    "article_code": item_data.get("article_code", ""),
                    "start": start,
                    "end": end,
                    "backend_uuid": backend_uuid,
                    "details": item_data.get("details", {}),
                    "plan_component": plan_component,
                }

                if not self.dry_run:
                    existing_item = InvoiceItem.objects.filter(uuid=uuid).first()

                    if existing_item:
                        if self.update_existing:
                            InvoiceItem.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["invoice_items"]["updated"] += 1
                        else:
                            self.stats["invoice_items"]["skipped"] += 1
                    else:
                        InvoiceItem.objects.create(uuid=uuid, **defaults)
                        self.stats["invoice_items"]["created"] += 1
                else:
                    existing = InvoiceItem.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["invoice_items"]["updated"] += 1
                        else:
                            self.stats["invoice_items"]["skipped"] += 1
                    else:
                        self.stats["invoice_items"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import invoice item {item_data.get('uuid')}: {e}"
                    )
                )
                self.stats["invoice_items"]["errors"] += 1

    def import_orders(self, orders_data):
        """Import order data."""
        self.stdout.write("Importing orders...")

        for order_data in orders_data:
            try:
                uuid = order_data.get("uuid")
                project_uuid = order_data.get("project_uuid")
                resource_uuid = order_data.get("resource_uuid")
                created_by_uuid = order_data.get("created_by_uuid")
                offering_uuid = order_data.get("offering_uuid")

                if (
                    not uuid
                    or not project_uuid
                    or not resource_uuid
                    or not created_by_uuid
                    or not offering_uuid
                ):
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping order without UUID, project_uuid, resource_uuid, created_by_uuid, or offering_uuid"
                        )
                    )
                    self.stats["orders"]["errors"] += 1
                    continue

                # Find project
                project = Project.available_objects.filter(uuid=project_uuid).first()
                if not project:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping order {uuid}: project {project_uuid} not found"
                        )
                    )
                    self.stats["orders"]["errors"] += 1
                    continue

                # Find resource
                resource = Resource.objects.filter(uuid=resource_uuid).first()
                if not resource:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping order {uuid}: resource {resource_uuid} not found"
                        )
                    )
                    self.stats["orders"]["errors"] += 1
                    continue

                # Find offering
                offering = Offering.objects.filter(uuid=offering_uuid).first()
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping order {uuid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["orders"]["errors"] += 1
                    continue

                # Find created_by user
                created_by = User.all_objects.filter(uuid=created_by_uuid).first()
                if not created_by:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping order {uuid}: user {created_by_uuid} not found"
                        )
                    )
                    self.stats["orders"]["errors"] += 1
                    continue

                # Find plan (optional)
                plan = None
                plan_uuid = order_data.get("plan_uuid")
                if plan_uuid:
                    plan = Plan.objects.filter(uuid=plan_uuid).first()

                # Find old_plan (optional)
                old_plan = None
                old_plan_uuid = order_data.get("old_plan_uuid")
                if old_plan_uuid:
                    old_plan = Plan.objects.filter(uuid=old_plan_uuid).first()

                # Find consumer_reviewed_by (optional)
                consumer_reviewed_by = None
                consumer_reviewed_by_uuid = order_data.get("consumer_reviewed_by_uuid")
                if consumer_reviewed_by_uuid:
                    consumer_reviewed_by = User.all_objects.filter(
                        uuid=consumer_reviewed_by_uuid
                    ).first()

                # Find provider_reviewed_by (optional)
                provider_reviewed_by = None
                provider_reviewed_by_uuid = order_data.get("provider_reviewed_by_uuid")
                if provider_reviewed_by_uuid:
                    provider_reviewed_by = User.all_objects.filter(
                        uuid=provider_reviewed_by_uuid
                    ).first()

                # Parse datetime fields
                consumer_reviewed_at = None
                if order_data.get("consumer_reviewed_at"):
                    try:
                        consumer_reviewed_at = datetime.fromisoformat(
                            order_data["consumer_reviewed_at"]
                        )
                        if timezone.is_naive(consumer_reviewed_at):
                            consumer_reviewed_at = timezone.make_aware(
                                consumer_reviewed_at
                            )
                    except (ValueError, TypeError):
                        pass

                provider_reviewed_at = None
                if order_data.get("provider_reviewed_at"):
                    try:
                        provider_reviewed_at = datetime.fromisoformat(
                            order_data["provider_reviewed_at"]
                        )
                        if timezone.is_naive(provider_reviewed_at):
                            provider_reviewed_at = timezone.make_aware(
                                provider_reviewed_at
                            )
                    except (ValueError, TypeError):
                        pass

                completed_at = None
                if order_data.get("completed_at"):
                    try:
                        completed_at = datetime.fromisoformat(
                            order_data["completed_at"]
                        )
                        if timezone.is_naive(completed_at):
                            completed_at = timezone.make_aware(completed_at)
                    except (ValueError, TypeError):
                        pass

                # Parse cost (Decimal field)
                cost = None
                if order_data.get("cost"):
                    try:
                        cost = Decimal(order_data["cost"])
                    except (ValueError, TypeError, InvalidOperation):
                        pass

                defaults = {
                    "type": order_data.get("type", 1),
                    "state": order_data.get("state", 1),
                    "project": project,
                    "resource": resource,
                    "offering": offering,
                    "plan": plan,
                    "old_plan": old_plan,
                    "created_by": created_by,
                    "consumer_reviewed_by": consumer_reviewed_by,
                    "provider_reviewed_by": provider_reviewed_by,
                    "output": order_data.get("output", ""),
                    "callback_url": order_data.get("callback_url", ""),
                    "termination_comment": order_data.get("termination_comment", ""),
                    "request_comment": order_data.get("request_comment", ""),
                    "attributes": order_data.get("attributes", {}),
                    "limits": order_data.get("limits", {}),
                    "cost": cost,
                    "consumer_reviewed_at": consumer_reviewed_at,
                    "provider_reviewed_at": provider_reviewed_at,
                    "completed_at": completed_at,
                    "backend_id": order_data.get("backend_id", ""),
                }

                if not self.dry_run:
                    existing_order = Order.objects.filter(uuid=uuid).first()

                    if existing_order:
                        if self.update_existing:
                            Order.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["orders"]["updated"] += 1
                        else:
                            self.stats["orders"]["skipped"] += 1
                    else:
                        Order.objects.create(uuid=uuid, **defaults)
                        self.stats["orders"]["created"] += 1
                else:
                    existing = Order.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["orders"]["updated"] += 1
                        else:
                            self.stats["orders"]["skipped"] += 1
                    else:
                        self.stats["orders"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import order {order_data.get('uuid')}: {e}"
                    )
                )
                self.stats["orders"]["errors"] += 1

    def import_offering_users(self, offering_users_data):
        """Import offering user data."""
        self.stdout.write("Importing offering users...")

        for offering_user_data in offering_users_data:
            try:
                uuid = offering_user_data.get("uuid")
                offering_uuid = offering_user_data.get("offering_uuid")
                user_uuid = offering_user_data.get("user_uuid")

                if not uuid or not offering_uuid or not user_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping offering user without UUID, offering_uuid, or user_uuid"
                        )
                    )
                    self.stats["offering_users"]["errors"] += 1
                    continue

                # Find offering
                offering = Offering.objects.filter(uuid=offering_uuid).first()
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping offering user {uuid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["offering_users"]["errors"] += 1
                    continue

                # Find user
                user = User.all_objects.filter(uuid=user_uuid).first()
                if not user:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping offering user {uuid}: user {user_uuid} not found"
                        )
                    )
                    self.stats["offering_users"]["errors"] += 1
                    continue

                defaults = {
                    "offering": offering,
                    "user": user,
                    "username": offering_user_data.get("username", ""),
                    "is_restricted": offering_user_data.get("is_restricted", False),
                    "state": offering_user_data.get("state", 1),
                    "service_provider_comment": offering_user_data.get(
                        "service_provider_comment", ""
                    ),
                    "service_provider_comment_url": offering_user_data.get(
                        "service_provider_comment_url", ""
                    ),
                }

                if not self.dry_run:
                    existing_offering_user = OfferingUser.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_offering_user:
                        if self.update_existing:
                            OfferingUser.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["offering_users"]["updated"] += 1
                        else:
                            self.stats["offering_users"]["skipped"] += 1
                    else:
                        OfferingUser.objects.create(uuid=UUID(uuid), **defaults)
                        self.stats["offering_users"]["created"] += 1
                else:
                    existing = OfferingUser.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["offering_users"]["updated"] += 1
                        else:
                            self.stats["offering_users"]["skipped"] += 1
                    else:
                        self.stats["offering_users"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import offering user {offering_user_data.get('uuid')}: {e}"
                    )
                )
                self.stats["offering_users"]["errors"] += 1

    def import_checklist_categories(self, categories_data):
        """Import checklist category data."""
        self.stdout.write("Importing checklist categories...")

        for category_data in categories_data:
            try:
                uuid = category_data.get("uuid")
                name = category_data.get("name")

                if not uuid or not name:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping checklist category without UUID or name"
                        )
                    )
                    self.stats["checklist_categories"]["errors"] += 1
                    continue

                defaults = {
                    "name": name,
                    "description": category_data.get("description", ""),
                }

                if not self.dry_run:
                    existing_category = ChecklistCategory.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_category:
                        if self.update_existing:
                            ChecklistCategory.objects.filter(uuid=uuid).update(
                                **defaults
                            )
                            self.stats["checklist_categories"]["updated"] += 1
                        else:
                            self.stats["checklist_categories"]["skipped"] += 1
                    else:
                        ChecklistCategory.objects.create(uuid=UUID(uuid), **defaults)
                        self.stats["checklist_categories"]["created"] += 1
                else:
                    existing = ChecklistCategory.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["checklist_categories"]["updated"] += 1
                        else:
                            self.stats["checklist_categories"]["skipped"] += 1
                    else:
                        self.stats["checklist_categories"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import checklist category {category_data.get('uuid')}: {e}"
                    )
                )
                self.stats["checklist_categories"]["errors"] += 1

    def import_checklists(self, checklists_data):
        """Import checklist data."""
        self.stdout.write("Importing checklists...")

        for checklist_data in checklists_data:
            try:
                uuid = checklist_data.get("uuid")
                name = checklist_data.get("name")

                if not uuid or not name:
                    self.stdout.write(
                        self.style.WARNING("Skipping checklist without UUID or name")
                    )
                    self.stats["checklists"]["errors"] += 1
                    continue

                # Find category (optional)
                category = None
                category_uuid = checklist_data.get("category_uuid")
                if category_uuid:
                    category = ChecklistCategory.objects.filter(
                        uuid=category_uuid
                    ).first()

                # Parse dates
                created = None
                if checklist_data.get("created"):
                    try:
                        created = datetime.fromisoformat(checklist_data["created"])
                        if timezone.is_naive(created):
                            created = timezone.make_aware(created)
                    except (ValueError, TypeError):
                        pass

                modified = None
                if checklist_data.get("modified"):
                    try:
                        modified = datetime.fromisoformat(checklist_data["modified"])
                        if timezone.is_naive(modified):
                            modified = timezone.make_aware(modified)
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "name": name,
                    "description": checklist_data.get("description", ""),
                    "checklist_type": checklist_data.get(
                        "checklist_type", "project_compliance"
                    ),
                    "category": category,
                }

                if not self.dry_run:
                    existing_checklist = Checklist.objects.filter(uuid=uuid).first()

                    if existing_checklist:
                        if self.update_existing:
                            Checklist.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["checklists"]["updated"] += 1
                        else:
                            self.stats["checklists"]["skipped"] += 1
                    else:
                        checklist = Checklist.objects.create(
                            uuid=UUID(uuid), **defaults
                        )
                        # Set timestamps after creation
                        if created:
                            checklist.created = created
                        if modified:
                            checklist.modified = modified
                        if created or modified:
                            checklist.save()
                        self.stats["checklists"]["created"] += 1
                else:
                    existing = Checklist.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["checklists"]["updated"] += 1
                        else:
                            self.stats["checklists"]["skipped"] += 1
                    else:
                        self.stats["checklists"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import checklist {checklist_data.get('uuid')}: {e}"
                    )
                )
                self.stats["checklists"]["errors"] += 1

    def import_questions(self, questions_data):
        """Import question data."""
        self.stdout.write("Importing questions...")

        for question_data in questions_data:
            try:
                uuid = question_data.get("uuid")
                checklist_uuid = question_data.get("checklist_uuid")

                if not uuid or not checklist_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping question without UUID or checklist_uuid"
                        )
                    )
                    self.stats["questions"]["errors"] += 1
                    continue

                # Find checklist
                checklist = Checklist.objects.filter(uuid=checklist_uuid).first()
                if not checklist:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping question {uuid}: checklist {checklist_uuid} not found"
                        )
                    )
                    self.stats["questions"]["errors"] += 1
                    continue

                defaults = {
                    "checklist": checklist,
                    "description": question_data.get("description", ""),
                    "order": question_data.get("order", 0),
                    "required": question_data.get("required", False),
                    "question_type": question_data.get("question_type", "boolean"),
                    "min_value": question_data.get("min_value"),
                    "max_value": question_data.get("max_value"),
                    "dependency_logic_operator": question_data.get(
                        "dependency_logic_operator", "and"
                    ),
                    "always_requires_review": question_data.get(
                        "requires_review", False
                    ),
                    "max_files_count": question_data.get("max_files"),
                }

                if not self.dry_run:
                    existing_question = Question.objects.filter(uuid=uuid).first()

                    if existing_question:
                        if self.update_existing:
                            Question.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["questions"]["updated"] += 1
                        else:
                            self.stats["questions"]["skipped"] += 1
                    else:
                        Question.objects.create(uuid=UUID(uuid), **defaults)
                        self.stats["questions"]["created"] += 1
                else:
                    existing = Question.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["questions"]["updated"] += 1
                        else:
                            self.stats["questions"]["skipped"] += 1
                    else:
                        self.stats["questions"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import question {question_data.get('uuid')}: {e}"
                    )
                )
                self.stats["questions"]["errors"] += 1

    def import_checklist_completions(self, completions_data):
        """Import checklist completion data."""
        self.stdout.write("Importing checklist completions...")

        for completion_data in completions_data:
            try:
                uuid = completion_data.get("uuid")
                checklist_uuid = completion_data.get("checklist_uuid")
                scope_content_type = completion_data.get("scope_content_type")
                scope_object_id = completion_data.get("scope_object_id")

                if not uuid or not checklist_uuid or not scope_content_type:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping completion without required fields"
                        )
                    )
                    self.stats["checklist_completions"]["errors"] += 1
                    continue

                # Find checklist
                checklist = Checklist.objects.filter(uuid=checklist_uuid).first()
                if not checklist:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping completion {uuid}: checklist {checklist_uuid} not found"
                        )
                    )
                    self.stats["checklist_completions"]["errors"] += 1
                    continue

                # Parse content type
                try:
                    app_label, model = scope_content_type.split(".")
                    content_type = ContentType.objects.get(
                        app_label=app_label, model=model
                    )
                except (ValueError, ContentType.DoesNotExist):
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping completion {uuid}: invalid scope_content_type {scope_content_type}"
                        )
                    )
                    self.stats["checklist_completions"]["errors"] += 1
                    continue

                # For project scope, try to resolve by UUID if provided
                if model == "project" and completion_data.get("scope_object_uuid"):
                    try:
                        project = Project.objects.get(
                            uuid=completion_data["scope_object_uuid"]
                        )
                        scope_object_id = project.id
                    except Project.DoesNotExist:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping completion {uuid}: project {completion_data['scope_object_uuid']} not found"
                            )
                        )
                        self.stats["checklist_completions"]["errors"] += 1
                        continue

                # Parse dates
                created = None
                if completion_data.get("created"):
                    try:
                        created = datetime.fromisoformat(completion_data["created"])
                        if timezone.is_naive(created):
                            created = timezone.make_aware(created)
                    except (ValueError, TypeError):
                        pass

                modified = None
                if completion_data.get("modified"):
                    try:
                        modified = datetime.fromisoformat(completion_data["modified"])
                        if timezone.is_naive(modified):
                            modified = timezone.make_aware(modified)
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "checklist": checklist,
                    "scope_content_type": content_type,
                    "scope_object_id": scope_object_id,
                }

                if not self.dry_run:
                    existing_completion = ChecklistCompletion.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_completion:
                        if self.update_existing:
                            ChecklistCompletion.objects.filter(uuid=uuid).update(
                                **defaults
                            )
                            self.stats["checklist_completions"]["updated"] += 1
                        else:
                            self.stats["checklist_completions"]["skipped"] += 1
                    else:
                        completion = ChecklistCompletion.objects.create(
                            uuid=UUID(uuid), **defaults
                        )
                        # Set timestamps after creation
                        if created:
                            completion.created = created
                        if modified:
                            completion.modified = modified
                        if created or modified:
                            completion.save()
                        self.stats["checklist_completions"]["created"] += 1
                else:
                    existing = ChecklistCompletion.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["checklist_completions"]["updated"] += 1
                        else:
                            self.stats["checklist_completions"]["skipped"] += 1
                    else:
                        self.stats["checklist_completions"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import checklist completion {completion_data.get('uuid')}: {e}"
                    )
                )
                self.stats["checklist_completions"]["errors"] += 1

    def import_answers(self, answers_data):
        """Import answer data."""
        self.stdout.write("Importing answers...")

        for answer_data in answers_data:
            try:
                uuid = answer_data.get("uuid")
                user_uuid = answer_data.get("user_uuid")
                question_uuid = answer_data.get("question_uuid")
                completion_uuid = answer_data.get("completion_uuid")

                if not uuid or not user_uuid or not question_uuid:
                    self.stdout.write(
                        self.style.WARNING("Skipping answer without required fields")
                    )
                    self.stats["answers"]["errors"] += 1
                    continue

                # Find user
                user = User.all_objects.filter(uuid=user_uuid).first()
                if not user:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping answer {uuid}: user {user_uuid} not found"
                        )
                    )
                    self.stats["answers"]["errors"] += 1
                    continue

                # Find question
                question = Question.objects.filter(uuid=question_uuid).first()
                if not question:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping answer {uuid}: question {question_uuid} not found"
                        )
                    )
                    self.stats["answers"]["errors"] += 1
                    continue

                # Find completion (optional but usually present)
                completion = None
                if completion_uuid:
                    completion = ChecklistCompletion.objects.filter(
                        uuid=completion_uuid
                    ).first()

                # Find reviewer (optional)
                reviewed_by = None
                reviewed_by_uuid = answer_data.get("reviewed_by_uuid")
                if reviewed_by_uuid:
                    reviewed_by = User.all_objects.filter(uuid=reviewed_by_uuid).first()

                # Parse dates
                created = None
                if answer_data.get("created"):
                    try:
                        created = datetime.fromisoformat(answer_data["created"])
                        if timezone.is_naive(created):
                            created = timezone.make_aware(created)
                    except (ValueError, TypeError):
                        pass

                modified = None
                if answer_data.get("modified"):
                    try:
                        modified = datetime.fromisoformat(answer_data["modified"])
                        if timezone.is_naive(modified):
                            modified = timezone.make_aware(modified)
                    except (ValueError, TypeError):
                        pass

                reviewed_at = None
                if answer_data.get("reviewed_at"):
                    try:
                        reviewed_at = datetime.fromisoformat(answer_data["reviewed_at"])
                        if timezone.is_naive(reviewed_at):
                            reviewed_at = timezone.make_aware(reviewed_at)
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "user": user,
                    "question": question,
                    "completion": completion,
                    "answer_data": answer_data.get("answer_data", []),
                    "requires_review": answer_data.get("requires_review", False),
                    "reviewed_by": reviewed_by,
                    "reviewed_at": reviewed_at,
                    "review_notes": answer_data.get("review_notes", ""),
                }

                if not self.dry_run:
                    existing_answer = Answer.objects.filter(uuid=uuid).first()

                    if existing_answer:
                        if self.update_existing:
                            Answer.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["answers"]["updated"] += 1
                        else:
                            self.stats["answers"]["skipped"] += 1
                    else:
                        answer = Answer.objects.create(uuid=UUID(uuid), **defaults)
                        # Set timestamps after creation
                        if created:
                            answer.created = created
                        if modified:
                            answer.modified = modified
                        if created or modified:
                            answer.save()
                        self.stats["answers"]["created"] += 1
                else:
                    existing = Answer.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["answers"]["updated"] += 1
                        else:
                            self.stats["answers"]["skipped"] += 1
                    else:
                        self.stats["answers"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import answer {answer_data.get('uuid')}: {e}"
                    )
                )
                self.stats["answers"]["errors"] += 1

    def _sync_user_activation_status(self):
        """
        Sync user activation status after import to ensure all users match current policy.

        This ensures that imported users have the correct activation status based on
        the DEACTIVATE_USER_IF_NO_ROLES setting, regardless of their imported state.
        """
        self.stdout.write(self.style.SUCCESS("\nSyncing user activation status..."))

        try:
            # Call the task function directly (not as a Celery task)
            sync_user_deactivation_status()
            self.stdout.write(
                self.style.SUCCESS("✓ User activation status synced successfully")
            )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"⚠ Failed to sync user activation status: {e}")
            )

    def print_summary(self):
        """Print import summary statistics."""
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS("Import Summary:"))
        self.stdout.write("=" * 60)

        for model_name, stats in self.stats.items():
            self.stdout.write(f"\n{model_name.replace('_', ' ').title()}:")
            self.stdout.write(f"  Created: {stats['created']}")
            self.stdout.write(f"  Updated: {stats['updated']}")
            self.stdout.write(f"  Skipped: {stats['skipped']}")
            if stats["errors"] > 0:
                self.stdout.write(self.style.WARNING(f"  Errors: {stats['errors']}"))

        total_created = sum(s["created"] for s in self.stats.values())
        total_updated = sum(s["updated"] for s in self.stats.values())
        total_errors = sum(s["errors"] for s in self.stats.values())

        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(f"Total Created: {total_created}")
        self.stdout.write(f"Total Updated: {total_updated}")
        if total_errors > 0:
            self.stdout.write(self.style.WARNING(f"Total Errors: {total_errors}"))
        self.stdout.write("=" * 60)
