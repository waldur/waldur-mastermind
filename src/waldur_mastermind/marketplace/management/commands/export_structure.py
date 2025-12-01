import json
import os

from django.core.management.base import BaseCommand
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
    Export comprehensive Waldur structure data to JSON format.

    This command exports a complete Waldur system structure including:
    - Users, Customers, Service Providers, Projects
    - Marketplace: Categories, Offerings, Plans, Components, Resources, Orders
    - Permissions: Roles, User Roles, Role Permissions
    - Accounts: Project/Customer Service Accounts, Course Accounts
    - Billing: Invoices, Invoice Items, Component Usages, Resource Plan Periods
    - Checklists: Categories, Checklists, Questions, Completions, Answers
    - System: Authentication Tokens, Offering Users

    The exported JSON file can be used for backup, migration, analysis, or import
    using the import_structure command. All UUIDs and relationships are preserved.

    Usage:
        waldur export_structure -o structure.json
        waldur export_structure --output /path/to/structure.json
    """

    def add_arguments(self, parser):
        parser.add_argument(
            "-o",
            "--output",
            dest="output",
            type=str,
            help="Path to the output JSON file.",
            required=True,
        )

    def handle(self, **options):
        output_path = options["output"]

        # Validate output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            self.stdout.write(
                self.style.ERROR(f"Output directory does not exist: {output_dir}")
            )
            return

        self.stdout.write("Starting structure export...")

        # Export data
        data = {
            "users": self.export_users(),
            "auth_tokens": self.export_auth_tokens(),
            "customers": self.export_customers(),
            "service_providers": self.export_service_providers(),
            "projects": self.export_projects(),
            "categories": self.export_categories(),
            "offerings": self.export_offerings(),
            "roles": self.export_roles(),
            "user_roles": self.export_user_roles(),
            "role_permissions": self.export_role_permissions(),
            "project_service_accounts": self.export_project_service_accounts(),
            "customer_service_accounts": self.export_customer_service_accounts(),
            "course_accounts": self.export_course_accounts(),
            "resources": self.export_resources(),
            "offering_components": self.export_offering_components(),
            "component_usages": self.export_component_usages(),
            "plans": self.export_plans(),
            "plan_components": self.export_plan_components(),
            "resource_plan_periods": self.export_resource_plan_periods(),
            "orders": self.export_orders(),
            "invoices": self.export_invoices(),
            "invoice_items": self.export_invoice_items(),
            "offering_users": self.export_offering_users(),
            # Checklist exports
            "checklist_categories": self.export_checklist_categories(),
            "checklists": self.export_checklists(),
            "questions": self.export_questions(),
            "checklist_completions": self.export_checklist_completions(),
            "answers": self.export_answers(),
        }

        # Write to JSON file
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)

            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully exported structure data to {output_path}"
                )
            )

            # Print summary
            self.stdout.write("\nExport summary:")
            self.stdout.write(f"  Users: {len(data['users'])}")
            self.stdout.write(f"  Auth Tokens: {len(data['auth_tokens'])}")
            self.stdout.write(f"  Customers: {len(data['customers'])}")
            self.stdout.write(f"  Service Providers: {len(data['service_providers'])}")
            self.stdout.write(f"  Projects: {len(data['projects'])}")
            self.stdout.write(f"  Categories: {len(data['categories'])}")
            self.stdout.write(f"  Offerings: {len(data['offerings'])}")
            self.stdout.write(f"  Roles: {len(data['roles'])}")
            self.stdout.write(f"  User Roles: {len(data['user_roles'])}")
            self.stdout.write(f"  Role Permissions: {len(data['role_permissions'])}")
            self.stdout.write(
                f"  Project Service Accounts: {len(data['project_service_accounts'])}"
            )
            self.stdout.write(
                f"  Customer Service Accounts: {len(data['customer_service_accounts'])}"
            )
            self.stdout.write(f"  Course Accounts: {len(data['course_accounts'])}")
            self.stdout.write(f"  Resources: {len(data['resources'])}")
            self.stdout.write(
                f"  Offering Components: {len(data['offering_components'])}"
            )
            self.stdout.write(f"  Component Usages: {len(data['component_usages'])}")
            self.stdout.write(f"  Plans: {len(data['plans'])}")
            self.stdout.write(f"  Plan Components: {len(data['plan_components'])}")
            self.stdout.write(f"  Orders: {len(data['orders'])}")
            self.stdout.write(f"  Invoices: {len(data['invoices'])}")
            self.stdout.write(f"  Invoice Items: {len(data['invoice_items'])}")
            self.stdout.write(f"  Offering Users: {len(data['offering_users'])}")

        except OSError as e:
            self.stdout.write(self.style.ERROR(f"Failed to write to file: {e}"))
            return
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Unexpected error: {e}"))
            return

    def export_users(self):
        """Export user data including system_robot."""
        users = []
        # Export even inactive users because they might be referenced in orders
        for user in User.all_objects.all().order_by("username"):
            users.append(
                {
                    "uuid": user.uuid.hex,
                    "username": user.username,
                    "email": user.email,
                    "full_name": user.full_name,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "is_staff": user.is_staff,
                    "is_support": user.is_support,
                    "is_active": user.is_active,
                    "native_name": user.native_name,
                    "phone_number": user.phone_number,
                    "organization": user.organization,
                    "job_title": user.job_title,
                    "civil_number": user.civil_number,
                    "description": user.description,
                    "date_joined": user.date_joined.isoformat()
                    if user.date_joined
                    else None,
                    # Additional fields identified in analysis
                    "token_lifetime": user.token_lifetime,
                    "details": user.details,
                    "notifications_enabled": user.notifications_enabled,
                    "is_identity_manager": user.is_identity_manager,
                    "registration_method": user.registration_method,
                    "identity_source": user.identity_source,
                    "agreement_date": user.agreement_date.isoformat()
                    if user.agreement_date
                    else None,
                    "preferred_language": user.preferred_language,
                    "backend_id": user.backend_id,
                    "birth_date": user.birth_date.isoformat()
                    if user.birth_date
                    else None,
                    "affiliations": user.affiliations,
                    "modified": user.modified.isoformat() if user.modified else None,
                    "slug": user.slug,
                    "query_field": user.query_field,
                    "is_superuser": user.is_superuser,
                    "last_login": user.last_login.isoformat()
                    if user.last_login
                    else None,
                }
            )
        return users

    def export_auth_tokens(self):
        """Export user authentication tokens."""
        auth_tokens = []
        for token in Token.objects.select_related("user").all():
            auth_tokens.append(
                {
                    "key": token.key,
                    "user_uuid": token.user.uuid.hex,
                    "user_username": token.user.username,
                    "created": token.created.isoformat() if token.created else None,
                }
            )
        return auth_tokens

    def export_customers(self):
        """Export customer/organization data."""
        customers = []
        for customer in Customer.objects.all().order_by("name"):
            customers.append(
                {
                    "uuid": customer.uuid.hex,
                    "name": customer.name,
                    "native_name": customer.native_name,
                    "abbreviation": customer.abbreviation,
                    "email": customer.email,
                    "phone_number": customer.phone_number,
                    "country": customer.country,
                    "vat_code": customer.vat_code,
                    "vat_name": customer.vat_name,
                    "vat_address": customer.vat_address,
                    "contact_details": customer.contact_details,
                    "agreement_number": customer.agreement_number,
                    "registration_code": customer.registration_code,
                    "homepage": customer.homepage,
                    "domain": customer.domain,
                    "address": customer.address,
                    "postal": customer.postal,
                    "blocked": customer.blocked,
                    "archived": customer.archived,
                    "slug": customer.slug,
                    "created": customer.created.isoformat()
                    if customer.created
                    else None,
                    "accounting_start_date": customer.accounting_start_date.isoformat()
                    if customer.accounting_start_date
                    else None,
                    "default_tax_percent": str(customer.default_tax_percent)
                    if customer.default_tax_percent
                    else None,
                    "sponsor_number": customer.sponsor_number,
                    "access_subnets": customer.access_subnets,
                    "notification_emails": customer.notification_emails,
                    "display_billing_info_in_projects": customer.display_billing_info_in_projects,
                    "grace_period_days": customer.grace_period_days,
                    "bank_name": customer.bank_name,
                    "bank_account": customer.bank_account,
                    "latitude": str(customer.latitude) if customer.latitude else None,
                    "longitude": str(customer.longitude)
                    if customer.longitude
                    else None,
                    "modified": customer.modified.isoformat()
                    if customer.modified
                    else None,
                }
            )
        return customers

    def export_service_providers(self):
        """Export service provider data."""
        service_providers = []
        for sp in ServiceProvider.objects.select_related("customer").order_by(
            "customer__name"
        ):
            service_providers.append(
                {
                    "uuid": sp.uuid.hex,
                    "customer_uuid": sp.customer.uuid.hex,
                    "customer_name": sp.customer.name,
                    "description": sp.description,
                    "enable_notifications": sp.enable_notifications,
                    "api_secret_code": sp.api_secret_code,
                    "lead_email": sp.lead_email,
                    "lead_subject": sp.lead_subject,
                    "lead_body": sp.lead_body,
                    "created": sp.created.isoformat() if sp.created else None,
                }
            )
        return service_providers

    def export_projects(self):
        """Export project data."""
        projects = []
        for project in Project.objects.select_related("customer").order_by("name"):
            project_data = {
                "uuid": project.uuid.hex,
                "name": project.name,
                "description": project.description,
                "customer_uuid": project.customer.uuid.hex,
                "customer_name": project.customer.name,
                "kind": project.kind,
                "created": project.created.isoformat() if project.created else None,
                "slug": project.slug,
                "start_date": project.start_date.isoformat()
                if project.start_date
                else None,
                "end_date": project.end_date.isoformat() if project.end_date else None,
                "oecd_fos_2007_code": project.oecd_fos_2007_code,
                "backend_id": project.backend_id,
                "modified": project.modified.isoformat() if project.modified else None,
            }

            # Add project metadata checklist reference if it exists
            if (
                hasattr(project.customer, "project_metadata_checklist")
                and project.customer.project_metadata_checklist
            ):
                project_data["project_metadata_checklist_uuid"] = (
                    project.customer.project_metadata_checklist.uuid.hex
                )

            projects.append(project_data)
        return projects

    def export_categories(self):
        """Export marketplace category data."""
        categories = []
        for category in Category.objects.all().order_by("title"):
            categories.append(
                {
                    "uuid": category.uuid.hex,
                    "title": category.title,
                    "description": category.description,
                    "backend_id": category.backend_id,
                    "default_vm_category": category.default_vm_category,
                    "default_volume_category": category.default_volume_category,
                    "default_tenant_category": category.default_tenant_category,
                    "created": category.created.isoformat()
                    if category.created
                    else None,
                }
            )
        return categories

    def export_offerings(self):
        """Export marketplace offering data."""
        offerings = []
        for offering in Offering.objects.select_related(
            "customer", "category"
        ).order_by("name"):
            offering_data = {
                "uuid": offering.uuid.hex,
                "name": offering.name,
                "description": offering.description,
                "type": offering.type,
                "state": offering.state,
                "slug": offering.slug,
                "category_uuid": offering.category.uuid.hex
                if offering.category
                else None,
                "category_title": offering.category.title
                if offering.category
                else None,
                "customer_uuid": offering.customer.uuid.hex
                if offering.customer
                else None,
                "customer_name": offering.customer.name if offering.customer else None,
                "shared": offering.shared,
                "billable": offering.billable,
                "attributes": offering.attributes,
                "options": offering.options,
                "resource_options": offering.resource_options,
                "plugin_options": offering.plugin_options,
                "created": offering.created.isoformat() if offering.created else None,
                # Additional fields identified in analysis
                "backend_id": offering.backend_id,
                "full_description": offering.full_description,
                "vendor_details": offering.vendor_details,
                "getting_started": offering.getting_started,
                "integration_guide": offering.integration_guide,
                "privacy_policy_link": offering.privacy_policy_link,
                "access_url": offering.access_url,
                "country": offering.country,
                "paused_reason": offering.paused_reason,
                "secret_options": offering.secret_options,
                "support_per_user_consumption_limitation": offering.support_per_user_consumption_limitation,
                "modified": offering.modified.isoformat()
                if offering.modified
                else None,
            }

            # Add parent offering reference if it exists
            if offering.parent:
                offering_data["parent_uuid"] = offering.parent.uuid.hex

            # Add project reference if it exists
            if offering.project:
                offering_data["project_uuid"] = offering.project.uuid.hex

            # Add compliance checklist reference if it exists
            if offering.compliance_checklist:
                offering_data["compliance_checklist_uuid"] = (
                    offering.compliance_checklist.uuid.hex
                )

            offerings.append(offering_data)
        return offerings

    def export_roles(self):
        """Export role definitions."""
        roles = []
        for role in Role.objects.select_related("content_type").order_by("name"):
            # Get content type representation
            content_type_str = (
                f"{role.content_type.app_label}.{role.content_type.model}"
                if role.content_type
                else None
            )

            roles.append(
                {
                    "uuid": role.uuid.hex,
                    "name": role.name,
                    "description": role.description,
                    "is_system_role": role.is_system_role,
                    "is_active": role.is_active,
                    "content_type": content_type_str,
                }
            )
        return roles

    def export_user_roles(self):
        """Export user role assignments."""
        user_roles = []
        scope_type_to_model = {
            "marketplace.serviceprovider": ServiceProvider,
            "structure.customer": Customer,
            "marketplace.offering": Offering,
            "structure.project": Project,
        }
        for user_role in (
            UserRole.objects.select_related(
                "user", "role", "content_type", "created_by"
            )
            .prefetch_related("role__permissions")
            .order_by("created")
        ):
            # Get scope information
            scope_type = None
            scope_uuid = None
            scope_name = None

            if user_role.content_type:
                scope_type = (
                    f"{user_role.content_type.app_label}.{user_role.content_type.model}"
                )
                if user_role.object_id:
                    model = scope_type_to_model.get(scope_type)
                    scope_uuid = model.objects.get(id=user_role.object_id).uuid.hex
                    # Try to get the scope object name
                    try:
                        scope_obj = user_role.scope
                        if hasattr(scope_obj, "name"):
                            scope_name = scope_obj.name
                        elif hasattr(scope_obj, "username"):
                            scope_name = scope_obj.username
                    except Exception:
                        pass

            user_roles.append(
                {
                    "uuid": user_role.uuid.hex,
                    "user_uuid": user_role.user.uuid.hex,
                    "user_username": user_role.user.username,
                    "role_uuid": user_role.role.uuid.hex,
                    "role_name": user_role.role.name,
                    "scope_type": scope_type,
                    "scope_uuid": scope_uuid,
                    "scope_name": scope_name,
                    "expiration_time": user_role.expiration_time.isoformat()
                    if user_role.expiration_time
                    else None,
                    "is_active": user_role.is_active,
                    "created": user_role.created.isoformat()
                    if user_role.created
                    else None,
                    "created_by_uuid": user_role.created_by.uuid.hex
                    if user_role.created_by
                    else None,
                    "created_by_username": user_role.created_by.username
                    if user_role.created_by
                    else None,
                }
            )
        return user_roles

    def export_role_permissions(self):
        """Export role permission mappings."""
        role_permissions = []
        for role_perm in RolePermission.objects.select_related("role").order_by(
            "role__name", "permission"
        ):
            role_permissions.append(
                {
                    "role_uuid": role_perm.role.uuid.hex,
                    "role_name": role_perm.role.name,
                    "permission": role_perm.permission,
                }
            )
        return role_permissions

    def export_project_service_accounts(self):
        """Export project service account data."""
        project_service_accounts = []
        for account in ProjectServiceAccount.objects.select_related(
            "project", "project__customer"
        ).order_by("created"):
            project_service_accounts.append(
                {
                    "uuid": account.uuid.hex,
                    "username": account.username,
                    "email": account.email,
                    "preferred_identifier": account.preferred_identifier,
                    "description": account.description,
                    "state": account.state,
                    "project_uuid": account.project.uuid.hex
                    if account.project
                    else None,
                    "project_name": account.project.name if account.project else None,
                    "customer_uuid": account.project.customer.uuid.hex
                    if account.project and account.project.customer
                    else None,
                    "customer_name": account.project.customer.name
                    if account.project and account.project.customer
                    else None,
                    "created": account.created.isoformat() if account.created else None,
                }
            )
        return project_service_accounts

    def export_customer_service_accounts(self):
        """Export customer service account data."""
        customer_service_accounts = []
        for account in CustomerServiceAccount.objects.select_related(
            "customer"
        ).order_by("created"):
            customer_service_accounts.append(
                {
                    "uuid": account.uuid.hex,
                    "username": account.username,
                    "email": account.email,
                    "preferred_identifier": account.preferred_identifier,
                    "description": account.description,
                    "state": account.state,
                    "customer_uuid": account.customer.uuid.hex
                    if account.customer
                    else None,
                    "customer_name": account.customer.name
                    if account.customer
                    else None,
                    "created": account.created.isoformat() if account.created else None,
                }
            )
        return customer_service_accounts

    def export_course_accounts(self):
        """Export course account data."""
        course_accounts = []
        for account in CourseAccount.objects.select_related(
            "project", "project__customer", "user"
        ).order_by("created"):
            course_accounts.append(
                {
                    "uuid": account.uuid.hex,
                    "email": account.email,
                    "description": account.description,
                    "state": account.state,
                    "user_uuid": account.user.uuid.hex if account.user else None,
                    "user_username": account.user.username if account.user else None,
                    "project_uuid": account.project.uuid.hex,
                    "project_name": account.project.name,
                    "customer_uuid": account.project.customer.uuid.hex
                    if account.project.customer
                    else None,
                    "customer_name": account.project.customer.name
                    if account.project.customer
                    else None,
                    "error_message": account.error_message,
                    "created": account.created.isoformat() if account.created else None,
                }
            )
        return course_accounts

    def export_resources(self):
        """Export marketplace resource data."""
        resources = []
        for resource in Resource.objects.select_related(
            "offering", "plan", "project", "project__customer"
        ).order_by("created"):
            resources.append(
                {
                    "uuid": resource.uuid.hex,
                    "name": resource.name,
                    "state": resource.state,
                    "offering_uuid": resource.offering.uuid.hex
                    if resource.offering
                    else None,
                    "offering_name": resource.offering.name
                    if resource.offering
                    else None,
                    "plan_uuid": resource.plan.uuid.hex if resource.plan else None,
                    "plan_name": resource.plan.name if resource.plan else None,
                    "project_uuid": resource.project.uuid.hex,
                    "project_name": resource.project.name,
                    "customer_uuid": resource.project.customer.uuid.hex,
                    "customer_name": resource.project.customer.name,
                    "attributes": resource.attributes,
                    "limits": resource.limits,
                    "options": resource.options,
                    "backend_id": resource.backend_id,
                    "effective_id": resource.effective_id,
                    "slug": resource.slug,
                    "created": resource.created.isoformat()
                    if resource.created
                    else None,
                    "description": resource.description,
                    "modified": resource.modified.isoformat()
                    if resource.modified
                    else None,
                    "end_date": resource.end_date.isoformat()
                    if resource.end_date
                    else None,
                    "report": resource.report,
                    "cost": str(resource.cost) if resource.cost else None,
                    "current_usages": resource.current_usages,
                    "error_message": resource.error_message,
                    "error_traceback": resource.error_traceback,
                }
            )
        return resources

    def export_offering_components(self):
        """Export offering component data."""
        components = []
        for component in OfferingComponent.objects.select_related("offering").order_by(
            "offering", "name"
        ):
            components.append(
                {
                    "uuid": component.uuid.hex,
                    "offering_uuid": component.offering.uuid.hex,
                    "offering_name": component.offering.name,
                    "type": component.type,
                    "name": component.name,
                    "description": component.description,
                    "billing_type": component.billing_type,
                    "measured_unit": component.measured_unit,
                    "limit_period": component.limit_period,
                    "limit_amount": component.limit_amount,
                    "article_code": component.article_code,
                    "backend_id": component.backend_id,
                }
            )
        return components

    def export_component_usages(self):
        """Export component usage data."""
        usages = []
        for usage in ComponentUsage.objects.select_related(
            "resource", "component", "component__offering"
        ).order_by("date"):
            usages.append(
                {
                    "uuid": usage.uuid.hex,
                    "resource_uuid": usage.resource.uuid.hex,
                    "resource_name": usage.resource.name,
                    "component_uuid": usage.component.uuid.hex,
                    "component_type": usage.component.type,
                    "component_name": usage.component.name,
                    "usage": str(usage.usage),
                    "date": usage.date.isoformat() if usage.date else None,
                    "billing_period": usage.billing_period.isoformat()
                    if usage.billing_period
                    else None,
                    "recurring": usage.recurring,
                    "description": usage.description,
                    "backend_id": usage.backend_id,
                    "modified": usage.modified.isoformat() if usage.modified else None,
                    "plan_period": usage.plan_period.uuid.hex
                    if usage.plan_period
                    else None,
                }
            )
        return usages

    def export_plans(self):
        """Export plan data."""
        plans = []
        for plan in Plan.objects.select_related("offering").order_by(
            "offering", "name"
        ):
            plans.append(
                {
                    "uuid": plan.uuid.hex,
                    "offering_uuid": plan.offering.uuid.hex,
                    "offering_name": plan.offering.name,
                    "name": plan.name,
                    "description": plan.description,
                    "unit_price": str(plan.unit_price),
                    "unit": plan.unit,
                    "archived": plan.archived,
                    "max_amount": plan.max_amount,
                    "article_code": plan.article_code,
                    "backend_id": plan.backend_id,
                    "created": plan.created.isoformat() if plan.created else None,
                    "modified": plan.modified.isoformat() if plan.modified else None,
                }
            )
        return plans

    def export_plan_components(self):
        """Export plan component data."""
        plan_components = []
        for pc in PlanComponent.objects.select_related(
            "plan", "component", "component__offering"
        ).order_by("plan", "component"):
            plan_components.append(
                {
                    "plan_uuid": pc.plan.uuid.hex,
                    "plan_name": pc.plan.name,
                    "component_uuid": pc.component.uuid.hex if pc.component else None,
                    "component_type": pc.component.type if pc.component else None,
                    "component_name": pc.component.name if pc.component else None,
                    "amount": pc.amount,
                    "price": str(pc.price),
                    "future_price": str(pc.future_price) if pc.future_price else None,
                }
            )
        return plan_components

    def export_resource_plan_periods(self):
        """Export resource plan period data."""

        periods = []
        for period in ResourcePlanPeriod.objects.select_related(
            "resource", "plan"
        ).order_by("resource", "start"):
            periods.append(
                {
                    "uuid": period.uuid.hex,
                    "resource_uuid": period.resource.uuid.hex,
                    "resource_name": period.resource.name,
                    "plan_uuid": period.plan.uuid.hex,
                    "plan_name": period.plan.name,
                    "start": period.start.isoformat() if period.start else None,
                    "end": period.end.isoformat() if period.end else None,
                    "created": period.created.isoformat() if period.created else None,
                    "modified": period.modified.isoformat()
                    if period.modified
                    else None,
                }
            )
        return periods

    def export_invoices(self):
        """Export invoice data."""
        invoices = []
        for invoice in Invoice.objects.select_related("customer").order_by(
            "year", "month", "customer"
        ):
            invoices.append(
                {
                    "uuid": invoice.uuid.hex,
                    "customer_uuid": invoice.customer.uuid.hex,
                    "customer_name": invoice.customer.name,
                    "month": invoice.month,
                    "year": invoice.year,
                    "state": invoice.state,
                    "total_cost": str(invoice.total_cost),
                    "total_price": str(invoice.total_price),
                    "tax_percent": str(invoice.tax_percent),
                    "invoice_date": invoice.invoice_date.isoformat()
                    if invoice.invoice_date
                    else None,
                    "created": invoice.created.isoformat() if invoice.created else None,
                    "backend_id": invoice.backend_id,
                }
            )
        return invoices

    def export_invoice_items(self):
        """Export invoice item data."""
        invoice_items = []
        for item in InvoiceItem.objects.select_related(
            "invoice", "invoice__customer", "resource", "project"
        ).order_by("invoice", "name"):
            invoice_items.append(
                {
                    "uuid": item.uuid.hex,
                    "invoice_uuid": item.invoice.uuid.hex,
                    "customer_uuid": item.invoice.customer.uuid.hex,
                    "customer_name": item.invoice.customer.name,
                    "resource_uuid": item.resource.uuid.hex if item.resource else None,
                    "resource_name": item.resource.name if item.resource else None,
                    "project_uuid": item.project.uuid.hex if item.project else None,
                    "project_name": item.project.name if item.project else None,
                    "name": item.name,
                    "quantity": str(item.quantity),
                    "measured_unit": item.measured_unit,
                    "unit_price": str(item.unit_price),
                    "article_code": item.article_code,
                    "start": item.start.isoformat() if item.start else None,
                    "end": item.end.isoformat() if item.end else None,
                    "backend_uuid": item.backend_uuid.hex
                    if item.backend_uuid
                    else None,
                    "details": item.details,
                    "plan_component": item.plan_component.id
                    if item.plan_component
                    else None,
                }
            )
        return invoice_items

    def export_orders(self):
        """Export order data."""
        orders = []
        for order in Order.objects.select_related(
            "project",
            "project__customer",
            "resource",
            "offering",
            "plan",
            "old_plan",
            "created_by",
            "consumer_reviewed_by",
            "provider_reviewed_by",
        ).order_by("created"):
            orders.append(
                {
                    "uuid": order.uuid.hex,
                    "type": order.type,
                    "state": order.state,
                    "project_uuid": order.project.uuid.hex,
                    "project_name": order.project.name,
                    "customer_uuid": order.project.customer.uuid.hex,
                    "customer_name": order.project.customer.name,
                    "resource_uuid": order.resource.uuid.hex,
                    "resource_name": order.resource.name,
                    "offering_uuid": order.offering.uuid.hex,
                    "offering_name": order.offering.name,
                    "plan_uuid": order.plan.uuid.hex if order.plan else None,
                    "plan_name": order.plan.name if order.plan else None,
                    "old_plan_uuid": order.old_plan.uuid.hex
                    if order.old_plan
                    else None,
                    "old_plan_name": order.old_plan.name if order.old_plan else None,
                    "created_by_uuid": order.created_by.uuid.hex,
                    "created_by_username": order.created_by.username,
                    "consumer_reviewed_by_uuid": order.consumer_reviewed_by.uuid.hex
                    if order.consumer_reviewed_by
                    else None,
                    "consumer_reviewed_by_username": order.consumer_reviewed_by.username
                    if order.consumer_reviewed_by
                    else None,
                    "provider_reviewed_by_uuid": order.provider_reviewed_by.uuid.hex
                    if order.provider_reviewed_by
                    else None,
                    "provider_reviewed_by_username": order.provider_reviewed_by.username
                    if order.provider_reviewed_by
                    else None,
                    "output": order.output,
                    "callback_url": order.callback_url or "",
                    "termination_comment": order.termination_comment or "",
                    "request_comment": order.request_comment or "",
                    "attributes": order.attributes,
                    "limits": order.limits,
                    "cost": str(order.cost) if order.cost else None,
                    "consumer_reviewed_at": order.consumer_reviewed_at.isoformat()
                    if order.consumer_reviewed_at
                    else None,
                    "provider_reviewed_at": order.provider_reviewed_at.isoformat()
                    if order.provider_reviewed_at
                    else None,
                    "completed_at": order.completed_at.isoformat()
                    if order.completed_at
                    else None,
                    "created": order.created.isoformat() if order.created else None,
                    "backend_id": order.backend_id,
                    "modified": order.modified.isoformat() if order.modified else None,
                    "slug": order.slug,
                    "error_message": order.error_message,
                    "error_traceback": order.error_traceback,
                }
            )
        return orders

    def export_offering_users(self):
        """Export offering user data."""
        offering_users = []
        for offering_user in OfferingUser.objects.select_related(
            "offering", "offering__customer", "user"
        ).order_by("created"):
            offering_users.append(
                {
                    "uuid": offering_user.uuid.hex,
                    "offering_uuid": offering_user.offering.uuid.hex,
                    "offering_name": offering_user.offering.name,
                    "user_uuid": offering_user.user.uuid.hex,
                    "user_username": offering_user.user.username,
                    "username": offering_user.username,
                    "is_restricted": offering_user.is_restricted,
                    "state": offering_user.state,
                    "service_provider_comment": offering_user.service_provider_comment,
                    "service_provider_comment_url": offering_user.service_provider_comment_url,
                    "created": offering_user.created.isoformat()
                    if offering_user.created
                    else None,
                    "modified": offering_user.modified.isoformat()
                    if offering_user.modified
                    else None,
                }
            )
        return offering_users

    def export_checklist_categories(self):
        """Export checklist category data."""
        categories = []
        for category in ChecklistCategory.objects.all().order_by("name"):
            categories.append(
                {
                    "uuid": category.uuid.hex,
                    "name": category.name,
                    "description": category.description,
                }
            )
        return categories

    def export_checklists(self):
        """Export checklist data."""
        checklists = []
        for checklist in Checklist.objects.select_related("category").order_by("name"):
            checklist_data = {
                "uuid": checklist.uuid.hex,
                "name": checklist.name,
                "description": checklist.description,
                "checklist_type": checklist.checklist_type,
                "created": checklist.created.isoformat() if checklist.created else None,
                "modified": checklist.modified.isoformat()
                if checklist.modified
                else None,
            }

            if checklist.category:
                checklist_data["category_uuid"] = checklist.category.uuid.hex

            checklists.append(checklist_data)
        return checklists

    def export_questions(self):
        """Export question data."""
        questions = []
        for question in Question.objects.select_related("checklist").order_by(
            "checklist", "order"
        ):
            questions.append(
                {
                    "uuid": question.uuid.hex,
                    "checklist_uuid": question.checklist.uuid.hex,
                    "description": question.description,
                    "order": question.order,
                    "required": question.required,
                    "question_type": question.question_type,
                    "min_value": question.min_value,
                    "max_value": question.max_value,
                    "dependency_logic_operator": question.dependency_logic_operator,
                    "requires_review": question.always_requires_review,
                    "max_files": question.max_files_count,
                }
            )
        return questions

    def export_checklist_completions(self):
        """Export checklist completion data for projects."""
        completions = []
        for completion in ChecklistCompletion.objects.select_related(
            "checklist"
        ).order_by("created"):
            completion_data = {
                "uuid": completion.uuid.hex,
                "checklist_uuid": completion.checklist.uuid.hex,
                "scope_content_type": f"{completion.scope_content_type.app_label}.{completion.scope_content_type.model}",
                "scope_object_id": completion.scope_object_id,
                "created": completion.created.isoformat()
                if completion.created
                else None,
                "modified": completion.modified.isoformat()
                if completion.modified
                else None,
            }

            # Add scope object UUID if it's a project (most common case)
            if completion.scope_content_type.model == "project":
                try:
                    project = Project.objects.get(id=completion.scope_object_id)
                    completion_data["scope_object_uuid"] = project.uuid.hex
                except Project.DoesNotExist:
                    pass

            completions.append(completion_data)
        return completions

    def export_answers(self):
        """Export answer data."""
        answers = []
        for answer in Answer.objects.select_related(
            "user", "question", "completion"
        ).order_by("created"):
            answers.append(
                {
                    "uuid": answer.uuid.hex,
                    "user_uuid": answer.user.uuid.hex,
                    "question_uuid": answer.question.uuid.hex,
                    "completion_uuid": answer.completion.uuid.hex
                    if answer.completion
                    else None,
                    "answer_data": answer.answer_data,
                    "requires_review": answer.requires_review,
                    "reviewed_by_uuid": answer.reviewed_by.uuid.hex
                    if answer.reviewed_by
                    else None,
                    "reviewed_at": answer.reviewed_at.isoformat()
                    if answer.reviewed_at
                    else None,
                    "review_notes": answer.review_notes,
                    "created": answer.created.isoformat() if answer.created else None,
                    "modified": answer.modified.isoformat()
                    if answer.modified
                    else None,
                }
            )
        return answers
