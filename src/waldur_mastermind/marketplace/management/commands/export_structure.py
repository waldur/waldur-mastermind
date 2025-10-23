import json
import os

from django.core.management.base import BaseCommand

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
    Export Waldur structure data to JSON format.

    This command exports Users, Customers, Projects, Offerings, Roles, UserRoles,
    and RolePermissions to a comprehensive JSON file for analysis or backup.

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
            "customers": self.export_customers(),
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
            "orders": self.export_orders(),
            "invoices": self.export_invoices(),
            "invoice_items": self.export_invoice_items(),
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
            self.stdout.write(f"  Customers: {len(data['customers'])}")
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

        except OSError as e:
            self.stdout.write(self.style.ERROR(f"Failed to write to file: {e}"))
            return
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Unexpected error: {e}"))
            return

    def export_users(self):
        """Export user data."""
        users = []
        for user in User.objects.all().order_by("username"):
            users.append(
                {
                    "uuid": user.uuid.hex,
                    "username": user.username,
                    "email": user.email,
                    "full_name": user.full_name,
                    "is_staff": user.is_staff,
                    "is_support": user.is_support,
                    "is_active": user.is_active,
                    "native_name": user.native_name,
                    "phone_number": user.phone_number,
                    "organization": user.organization,
                    "job_title": user.job_title,
                    "civil_number": user.civil_number,
                    "date_joined": user.date_joined.isoformat()
                    if user.date_joined
                    else None,
                }
            )
        return users

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
                    "created": customer.created.isoformat()
                    if customer.created
                    else None,
                }
            )
        return customers

    def export_projects(self):
        """Export project data."""
        projects = []
        for project in Project.objects.select_related("customer").order_by("name"):
            projects.append(
                {
                    "uuid": project.uuid.hex,
                    "name": project.name,
                    "description": project.description,
                    "customer_uuid": project.customer.uuid.hex,
                    "customer_name": project.customer.name,
                    "created": project.created.isoformat() if project.created else None,
                    "start_date": project.start_date.isoformat()
                    if project.start_date
                    else None,
                    "end_date": project.end_date.isoformat()
                    if project.end_date
                    else None,
                    "oecd_fos_2007_code": project.oecd_fos_2007_code,
                }
            )
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
            offerings.append(
                {
                    "uuid": offering.uuid.hex,
                    "name": offering.name,
                    "description": offering.description,
                    "type": offering.type,
                    "state": offering.state,
                    "category_uuid": offering.category.uuid.hex
                    if offering.category
                    else None,
                    "category_title": offering.category.title
                    if offering.category
                    else None,
                    "customer_uuid": offering.customer.uuid.hex
                    if offering.customer
                    else None,
                    "customer_name": offering.customer.name
                    if offering.customer
                    else None,
                    "shared": offering.shared,
                    "billable": offering.billable,
                    "attributes": offering.attributes,
                    "options": offering.options,
                    "resource_options": offering.resource_options,
                    "plugin_options": offering.plugin_options,
                    "created": offering.created.isoformat()
                    if offering.created
                    else None,
                }
            )
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
                    scope_uuid = str(user_role.object_id)
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
                    "created": resource.created.isoformat()
                    if resource.created
                    else None,
                    "description": resource.description,
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
                    "created": plan.created.isoformat() if plan.created else None,
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
                }
            )
        return orders
