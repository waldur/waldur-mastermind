import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from waldur_core.core.models import User
from waldur_core.permissions.models import Role, RolePermission, UserRole
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.invoices.models import Invoice, InvoiceItem
from waldur_mastermind.marketplace.models import (
    Category,
    ComponentUsage,
    CourseAccount,
    CustomerServiceAccount,
    Offering,
    OfferingComponent,
    Order,
    Plan,
    PlanComponent,
    ProjectServiceAccount,
    Resource,
)


class Command(BaseCommand):
    help = """
    Import Waldur structure data from JSON format.

    This command imports Users, Customers, Projects, Offerings, Roles, UserRoles,
    and RolePermissions from a JSON file created by export_structure command.

    Usage:
        waldur import_structure -i structure.json
        waldur import_structure --input structure.json --update
        waldur import_structure -i structure.json --skip-users --dry-run
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stats = {
            "users": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "customers": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
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
            "component_usages": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "orders": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "invoices": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "invoice_items": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
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

    def handle(self, **options):
        input_path = options["input"]
        self.update_existing = options["update"]
        self.dry_run = options["dry_run"]
        skip_users = options["skip_users"]
        skip_roles = options["skip_roles"]

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

        self.stdout.write("Starting structure import...")

        try:
            with transaction.atomic():
                # Import in dependency order
                if not skip_users:
                    self.import_users(data.get("users", []))

                self.import_customers(data.get("customers", []))
                self.import_projects(data.get("projects", []))
                self.import_categories(data.get("categories", []))
                self.import_offerings(data.get("offerings", []))

                # Import marketplace components and plans
                self.import_plans(data.get("plans", []))
                self.import_offering_components(data.get("offering_components", []))
                self.import_plan_components(data.get("plan_components", []))

                # Import resources (depends on offerings, plans, projects)
                self.import_resources(data.get("resources", []))

                # Import component usages (depends on resources and components)
                self.import_component_usages(data.get("component_usages", []))

                # Import orders (depends on resources, projects, users, plans)
                self.import_orders(data.get("orders", []))

                if not skip_roles:
                    self.import_roles(data.get("roles", []))
                    self.import_role_permissions(data.get("role_permissions", []))

                self.import_user_roles(data.get("user_roles", []))

                # Import account types
                self.import_project_service_accounts(
                    data.get("project_service_accounts", [])
                )
                self.import_customer_service_accounts(
                    data.get("customer_service_accounts", [])
                )
                self.import_course_accounts(data.get("course_accounts", []))

                # Import invoicing (depends on customers, resources, projects)
                self.import_invoices(data.get("invoices", []))
                self.import_invoice_items(data.get("invoice_items", []))

                if self.dry_run:
                    # Rollback transaction in dry-run mode
                    raise Exception("Dry run - rolling back transaction")

        except Exception as e:
            if self.dry_run:
                self.stdout.write(
                    self.style.WARNING("Dry run completed - no changes made")
                )
            else:
                self.stdout.write(self.style.ERROR(f"Import failed: {e}"))
                return

        # Print summary
        self.print_summary()

    def import_users(self, users_data):
        """Import user data."""
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

                if not username or not email:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping user {uuid}: missing username or email"
                        )
                    )
                    self.stats["users"]["errors"] += 1
                    continue

                existing_user = User.objects.filter(uuid=uuid).first()

                if existing_user:
                    if self.update_existing:
                        # Update existing user
                        existing_user.username = username
                        existing_user.email = email
                        existing_user.full_name = user_data.get("full_name", "")
                        existing_user.native_name = user_data.get("native_name", "")
                        existing_user.phone_number = user_data.get("phone_number", "")
                        existing_user.organization = user_data.get("organization", "")
                        existing_user.job_title = user_data.get("job_title", "")
                        existing_user.is_staff = user_data.get("is_staff", False)
                        existing_user.is_support = user_data.get("is_support", False)
                        existing_user.is_active = user_data.get("is_active", True)

                        if user_data.get("civil_number"):
                            existing_user.civil_number = user_data.get("civil_number")

                        if not self.dry_run:
                            existing_user.save()

                        self.stats["users"]["updated"] += 1
                    else:
                        self.stats["users"]["skipped"] += 1
                else:
                    # Create new user
                    user = User(
                        uuid=uuid,
                        username=username,
                        email=email,
                        full_name=user_data.get("full_name", ""),
                        native_name=user_data.get("native_name", ""),
                        phone_number=user_data.get("phone_number", ""),
                        organization=user_data.get("organization", ""),
                        job_title=user_data.get("job_title", ""),
                        is_staff=user_data.get("is_staff", False),
                        is_support=user_data.get("is_support", False),
                        is_active=user_data.get("is_active", True),
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
                }

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
                    "oecd_fos_2007_code": project_data.get("oecd_fos_2007_code", ""),
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
                name = role_data.get("name")
                content_type_str = role_data.get("content_type")

                if not name or not content_type_str:
                    self.stdout.write(
                        self.style.WARNING("Skipping role without name or content_type")
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
                    "description": role_data.get("description", ""),
                    "is_system_role": role_data.get("is_system_role", False),
                    "is_active": role_data.get("is_active", True),
                    "content_type": content_type,
                }

                if not self.dry_run:
                    existing_role = Role.objects.filter(name=name).first()

                    if existing_role:
                        if self.update_existing:
                            Role.objects.filter(name=name).update(**defaults)
                            self.stats["roles"]["updated"] += 1
                        else:
                            self.stats["roles"]["skipped"] += 1
                    else:
                        Role.objects.create(name=name, **defaults)
                        self.stats["roles"]["created"] += 1
                else:
                    existing = Role.objects.filter(name=name).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["roles"]["updated"] += 1
                        else:
                            self.stats["roles"]["skipped"] += 1
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
                user = User.objects.filter(uuid=user_uuid).first()
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
                        ProjectServiceAccount.objects.create(uuid=uuid, **defaults)
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
                    user = User.objects.filter(uuid=user_uuid).first()
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
                        CourseAccount.objects.create(uuid=uuid, **defaults)
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

                defaults = {
                    "resource": resource,
                    "component": component,
                    "usage": usage_data.get("usage", 0),
                    "date": date or timezone.now(),
                    "billing_period": billing_period or timezone.now().date(),
                    "recurring": usage_data.get("recurring", False),
                    "description": usage_data.get("description", ""),
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
                created_by = User.objects.filter(uuid=created_by_uuid).first()
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
                    consumer_reviewed_by = User.objects.filter(
                        uuid=consumer_reviewed_by_uuid
                    ).first()

                # Find provider_reviewed_by (optional)
                provider_reviewed_by = None
                provider_reviewed_by_uuid = order_data.get("provider_reviewed_by_uuid")
                if provider_reviewed_by_uuid:
                    provider_reviewed_by = User.objects.filter(
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
