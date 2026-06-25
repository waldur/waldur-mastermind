import json
import logging
import os
import time

from django.core.management.base import BaseCommand
from rest_framework.authtoken.models import Token

from waldur_core.checklist.models import (
    Answer,
    Checklist,
    ChecklistCompletion,
    Question,
    QuestionDependency,
    QuestionOption,
)
from waldur_core.core.models import User
from waldur_core.logging.enums import EVENT_GROUP_MAPPING, EventGroup, EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.models import Role, RolePermission, UserRole
from waldur_core.structure.models import (
    Customer,
    Project,
    ServiceSettings,
    UserAgreement,
)
from waldur_core.users.models import GroupInvitation, Invitation, PermissionRequest
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
    ResourcePlanPeriod,
    RobotAccount,
    ServiceProvider,
    SoftwareCatalog,
)
from waldur_mastermind.policy.models import (
    CustomerEstimatedCostPolicy,
    ProjectEstimatedCostPolicy,
    SlurmPeriodicUsagePolicy,
)
from waldur_mastermind.proposal.models import (
    AssignmentBatch,
    AssignmentItem,
    Call,
    CallManagingOrganisation,
    CallResourceTemplate,
    Proposal,
    ProposalProjectRoleMapping,
    RequestedOffering,
    RequestedResource,
    Review,
    ReviewerSuggestion,
    Round,
)
from waldur_openstack.models import Flavor, Image, Instance, Tenant, Volume


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
    - User Management: Invitations, Group Invitations, Permission Requests

    The exported JSON file can be used for backup, migration, analysis, or import
    using the import_structure command. All UUIDs and relationships are preserved.

    Usage:
        waldur export_structure -o structure.json
        waldur export_structure --output /path/to/structure.json
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = logging.getLogger(__name__)
        self.export_times = {}

    def add_arguments(self, parser):
        parser.add_argument(
            "-o",
            "--output",
            dest="output",
            type=str,
            help="Path to the output JSON file.",
            required=True,
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose logging output",
        )
        parser.add_argument(
            "--include-events",
            action="store_true",
            help="Include audit log events related to invoicing, credits and policies.",
        )

    def log_export_step(self, step_name, export_function):
        """Helper method to log and time export steps."""
        self.logger.info(f"Starting export of {step_name}...")
        start_time = time.time()

        try:
            result = export_function()
            count = len(result) if result else 0
            elapsed = time.time() - start_time
            self.export_times[step_name] = elapsed

            self.logger.info(
                f"Completed export of {step_name}: {count} records in {elapsed:.2f}s"
            )
            self.stdout.write(f"  ✓ {step_name}: {count} records ({elapsed:.2f}s)")

            return result
        except Exception as e:
            elapsed = time.time() - start_time
            self.logger.error(f"Failed to export {step_name} after {elapsed:.2f}s: {e}")
            self.stdout.write(self.style.ERROR(f"  ✗ {step_name}: Failed ({e})"))
            raise

    def handle(self, **options):
        output_path = options["output"]
        verbose = options.get("verbose", False)
        include_events = options.get("include_events", False)

        # Setup logging level
        if verbose:
            logging.basicConfig(level=logging.INFO)

        self.logger.info(f"Starting structure export to {output_path}")

        # Validate output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            error_msg = f"Output directory does not exist: {output_dir}"
            self.logger.error(error_msg)
            self.stdout.write(self.style.ERROR(error_msg))
            return

        self.stdout.write("Starting structure export...")
        self.logger.info("Beginning data export phase")

        export_start_time = time.time()

        # Export data with logging
        data = {
            "users": self.log_export_step("users", self.export_users),
            "auth_tokens": self.log_export_step("auth_tokens", self.export_auth_tokens),
            "customers": self.log_export_step("customers", self.export_customers),
            "service_providers": self.log_export_step(
                "service_providers", self.export_service_providers
            ),
            "projects": self.log_export_step("projects", self.export_projects),
            "category_groups": self.log_export_step(
                "category_groups", self.export_category_groups
            ),
            "categories": self.log_export_step("categories", self.export_categories),
            "offerings": self.log_export_step("offerings", self.export_offerings),
            "roles": self.log_export_step("roles", self.export_roles),
            "user_roles": self.log_export_step("user_roles", self.export_user_roles),
            "role_permissions": self.log_export_step(
                "role_permissions", self.export_role_permissions
            ),
            "project_service_accounts": self.log_export_step(
                "project_service_accounts", self.export_project_service_accounts
            ),
            "customer_service_accounts": self.log_export_step(
                "customer_service_accounts", self.export_customer_service_accounts
            ),
            "course_accounts": self.log_export_step(
                "course_accounts", self.export_course_accounts
            ),
            "resources": self.log_export_step("resources", self.export_resources),
            "offering_components": self.log_export_step(
                "offering_components", self.export_offering_components
            ),
            "component_usages": self.log_export_step(
                "component_usages", self.export_component_usages
            ),
            "plans": self.log_export_step("plans", self.export_plans),
            "plan_components": self.log_export_step(
                "plan_components", self.export_plan_components
            ),
            "resource_plan_periods": self.log_export_step(
                "resource_plan_periods", self.export_resource_plan_periods
            ),
            "orders": self.log_export_step("orders", self.export_orders),
            "invoices": self.log_export_step("invoices", self.export_invoices),
            "invoice_items": self.log_export_step(
                "invoice_items", self.export_invoice_items
            ),
            "offering_users": self.log_export_step(
                "offering_users", self.export_offering_users
            ),
            "robot_accounts": self.log_export_step(
                "robot_accounts", self.export_robot_accounts
            ),
            "offering_user_groups": self.log_export_step(
                "offering_user_groups", self.export_offering_user_groups
            ),
            # Checklist exports
            "checklists": self.log_export_step("checklists", self.export_checklists),
            "questions": self.log_export_step("questions", self.export_questions),
            "question_options": self.log_export_step(
                "question_options", self.export_question_options
            ),
            "question_dependencies": self.log_export_step(
                "question_dependencies", self.export_question_dependencies
            ),
            "checklist_completions": self.log_export_step(
                "checklist_completions", self.export_checklist_completions
            ),
            "answers": self.log_export_step("answers", self.export_answers),
            # User management exports
            "invitations": self.log_export_step("invitations", self.export_invitations),
            "group_invitations": self.log_export_step(
                "group_invitations", self.export_group_invitations
            ),
            "permission_requests": self.log_export_step(
                "permission_requests", self.export_permission_requests
            ),
            # Credit exports
            "customer_credits": self.log_export_step(
                "customer_credits", self.export_customer_credits
            ),
            "project_credits": self.log_export_step(
                "project_credits", self.export_project_credits
            ),
            # User agreements
            "user_agreements": self.log_export_step(
                "user_agreements", self.export_user_agreements
            ),
            # Proposal/Call management exports
            "call_managing_organisations": self.log_export_step(
                "call_managing_organisations", self.export_call_managing_organisations
            ),
            "calls": self.log_export_step("calls", self.export_calls),
            "requested_offerings": self.log_export_step(
                "requested_offerings", self.export_requested_offerings
            ),
            "call_resource_templates": self.log_export_step(
                "call_resource_templates", self.export_call_resource_templates
            ),
            "rounds": self.log_export_step("rounds", self.export_rounds),
            "proposals": self.log_export_step("proposals", self.export_proposals),
            "requested_resources": self.log_export_step(
                "requested_resources", self.export_requested_resources
            ),
            "reviews": self.log_export_step("reviews", self.export_reviews),
            "assignment_batches": self.log_export_step(
                "assignment_batches", self.export_assignment_batches
            ),
            "assignment_items": self.log_export_step(
                "assignment_items", self.export_assignment_items
            ),
            "reviewer_suggestions": self.log_export_step(
                "reviewer_suggestions", self.export_reviewer_suggestions
            ),
            "role_mappings": self.log_export_step(
                "role_mappings", self.export_role_mappings
            ),
            # Maintenance announcement exports
            "maintenance_announcements": self.log_export_step(
                "maintenance_announcements", self.export_maintenance_announcements
            ),
            "maintenance_announcement_offerings": self.log_export_step(
                "maintenance_announcement_offerings",
                self.export_maintenance_announcement_offerings,
            ),
            # Software catalog exports
            "software_catalogs": self.log_export_step(
                "software_catalogs", self.export_software_catalogs
            ),
            "offering_partitions": self.log_export_step(
                "offering_partitions", self.export_offering_partitions
            ),
            "offering_software_catalogs": self.log_export_step(
                "offering_software_catalogs", self.export_offering_software_catalogs
            ),
            # Policy exports
            "project_estimated_cost_policies": self.log_export_step(
                "project_estimated_cost_policies",
                self.export_project_estimated_cost_policies,
            ),
            "customer_estimated_cost_policies": self.log_export_step(
                "customer_estimated_cost_policies",
                self.export_customer_estimated_cost_policies,
            ),
            "slurm_periodic_policies": self.log_export_step(
                "slurm_periodic_policies",
                self.export_slurm_periodic_policies,
            ),
            # OpenStack backend model exports
            "openstack_service_settings": self.log_export_step(
                "openstack_service_settings", self.export_openstack_service_settings
            ),
            "openstack_flavors": self.log_export_step(
                "openstack_flavors", self.export_openstack_flavors
            ),
            "openstack_images": self.log_export_step(
                "openstack_images", self.export_openstack_images
            ),
            "openstack_tenants": self.log_export_step(
                "openstack_tenants", self.export_openstack_tenants
            ),
            "openstack_instances": self.log_export_step(
                "openstack_instances", self.export_openstack_instances
            ),
            "openstack_volumes": self.log_export_step(
                "openstack_volumes", self.export_openstack_volumes
            ),
        }

        if include_events:
            data["events"] = self.log_export_step("events", self.export_events)

        export_elapsed = time.time() - export_start_time
        self.logger.info(f"Data export completed in {export_elapsed:.2f}s")

        # Write to JSON file
        self.logger.info(f"Writing data to JSON file: {output_path}")
        write_start_time = time.time()

        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)

            write_elapsed = time.time() - write_start_time
            file_size = os.path.getsize(output_path) / (1024 * 1024)  # Size in MB

            self.logger.info(
                f"Successfully wrote {file_size:.1f}MB JSON file in {write_elapsed:.2f}s"
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully exported structure data to {output_path} ({file_size:.1f}MB)"
                )
            )

            # Print summary
            total_elapsed = time.time() - export_start_time + write_elapsed
            total_records = sum(
                len(v) if isinstance(v, list) else 0 for v in data.values()
            )

            self.stdout.write("\nExport summary:")
            self.stdout.write(f"  Total records: {total_records}")
            self.stdout.write(f"  Total time: {total_elapsed:.2f}s")
            self.stdout.write(f"  File size: {file_size:.1f}MB")
            self.stdout.write("")

            # Detailed breakdown
            self.stdout.write("Record counts by type:")
            for key, value in data.items():
                count = len(value) if isinstance(value, list) else 0
                timing = self.export_times.get(key, 0)
                self.stdout.write(
                    f"  {key.replace('_', ' ').title()}: {count} ({timing:.2f}s)"
                )

            # Performance summary
            if self.export_times:
                slowest_exports = sorted(
                    self.export_times.items(), key=lambda x: x[1], reverse=True
                )[:5]
                self.stdout.write("\nSlowest exports:")
                for export_name, export_time in slowest_exports:
                    count = len(data.get(export_name, []))
                    rate = count / export_time if export_time > 0 else 0
                    self.stdout.write(
                        f"  {export_name}: {export_time:.2f}s ({count} records, {rate:.1f} rec/s)"
                    )

            self.logger.info(
                f"Export completed successfully: {total_records} records, {file_size:.1f}MB, {total_elapsed:.2f}s"
            )

        except OSError as e:
            write_elapsed = time.time() - write_start_time
            self.logger.error(f"Failed to write file after {write_elapsed:.2f}s: {e}")
            self.stdout.write(self.style.ERROR(f"Failed to write to file: {e}"))
            return
        except Exception as e:
            write_elapsed = time.time() - write_start_time
            self.logger.error(f"Unexpected error after {write_elapsed:.2f}s: {e}")
            self.stdout.write(self.style.ERROR(f"Unexpected error: {e}"))
            return

    def export_users(self):
        """Export user data including system_robot."""
        users = []
        # Export even inactive users because they might be referenced in orders
        queryset = User.all_objects.all().order_by("username")
        self.logger.debug(f"Querying {queryset.count()} users...")

        for user in queryset:
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
                    "deactivation_reason": user.deactivation_reason,
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
                    "token_lifetime": user.token_lifetime
                    if user.token_lifetime is not None
                    else -1,
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
                    # AAI (Authentication and Authorization Infrastructure) attributes
                    "gender": user.gender,
                    "personal_title": user.personal_title,
                    "place_of_birth": user.place_of_birth,
                    "country_of_residence": user.country_of_residence,
                    "nationality": user.nationality,
                    "nationalities": user.nationalities,
                    "organization_country": user.organization_country,
                    "organization_type": user.organization_type,
                    "organization_registry_code": user.organization_registry_code,
                    "organization_vat_code": user.organization_vat_code,
                    "organization_address": user.organization_address,
                    "eduperson_assurance": user.eduperson_assurance,
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

    def export_category_groups(self):
        """Export marketplace category group data."""
        category_groups = []
        for group in CategoryGroup.objects.all().order_by("title"):
            category_groups.append(
                {
                    "uuid": group.uuid.hex,
                    "title": group.title,
                    "description": group.description,
                    "created": group.created.isoformat() if group.created else None,
                }
            )
        return category_groups

    def export_categories(self):
        """Export marketplace category data."""
        categories = []
        for category in Category.objects.select_related("group").order_by("title"):
            categories.append(
                {
                    "uuid": category.uuid.hex,
                    "title": category.title,
                    "description": category.description,
                    "backend_id": category.backend_id,
                    "default_vm_category": category.default_vm_category,
                    "default_volume_category": category.default_volume_category,
                    "default_tenant_category": category.default_tenant_category,
                    "group_uuid": category.group.uuid.hex if category.group else None,
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
                "helpdesk_url": offering.helpdesk_url,
                "documentation_url": offering.documentation_url,
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

            # Add scope reference if it exists
            if offering.content_type and offering.object_id:
                offering_data["scope_type"] = (
                    f"{offering.content_type.app_label}.{offering.content_type.model}"
                )
                if hasattr(offering.scope, "uuid"):
                    offering_data["scope_uuid"] = offering.scope.uuid.hex

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

        queryset = (
            UserRole.objects.select_related(
                "user", "role", "content_type", "created_by"
            )
            .prefetch_related("role__permissions")
            .order_by("created")
        )
        self.logger.debug(
            f"Querying {queryset.count()} user roles with related data..."
        )

        for user_role in queryset:
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
                    if model:
                        try:
                            scope_obj = model.objects.get(id=user_role.object_id)
                            scope_uuid = scope_obj.uuid.hex
                            # Try to get the scope object name
                            if hasattr(scope_obj, "name"):
                                scope_name = scope_obj.name
                            elif hasattr(scope_obj, "username"):
                                scope_name = scope_obj.username
                        except model.DoesNotExist:
                            # Skip user roles that reference deleted objects
                            warning_msg = (
                                f"Skipping user role {user_role.uuid.hex}: "
                                f"Referenced {scope_type} with ID {user_role.object_id} does not exist"
                            )
                            self.logger.warning(warning_msg)
                            self.stdout.write(self.style.WARNING(warning_msg))
                            continue
                        except Exception as e:
                            # Handle any other unexpected errors
                            error_msg = (
                                f"Error processing user role {user_role.uuid.hex}: "
                                f"Could not access {scope_type} with ID {user_role.object_id}: {e}"
                            )
                            self.logger.error(error_msg)
                            self.stdout.write(self.style.WARNING(error_msg))
                            continue

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
            "offering", "plan", "project", "project__customer", "content_type"
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
                    "scope_type": f"{resource.content_type.app_label}.{resource.content_type.model}"
                    if resource.content_type
                    else None,
                    "scope_uuid": resource.scope.uuid.hex
                    if resource.content_type and resource.scope
                    else None,
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
        queryset = ComponentUsage.objects.select_related(
            "resource", "component", "component__offering"
        ).order_by("date")
        self.logger.debug(f"Querying {queryset.count()} component usages...")

        for usage in queryset:
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
                    "backend_metadata": offering_user.backend_metadata,
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

    def export_robot_accounts(self):
        """Export robot account data."""
        robot_accounts = []
        for robot_account in (
            RobotAccount.objects.select_related("resource", "responsible_user")
            .prefetch_related("users")
            .order_by("created")
        ):
            robot_accounts.append(
                {
                    "uuid": robot_account.uuid.hex,
                    "resource_uuid": robot_account.resource.uuid.hex,
                    "resource_name": robot_account.resource.name,
                    "username": robot_account.username,
                    "description": robot_account.description,
                    "type": robot_account.type,
                    "keys": robot_account.keys,
                    "state": robot_account.state,
                    "backend_metadata": robot_account.backend_metadata,
                    "backend_id": robot_account.backend_id,
                    "responsible_user_uuid": robot_account.responsible_user.uuid.hex
                    if robot_account.responsible_user
                    else None,
                    "user_uuids": [user.uuid.hex for user in robot_account.users.all()],
                    "created": robot_account.created.isoformat()
                    if robot_account.created
                    else None,
                    "modified": robot_account.modified.isoformat()
                    if robot_account.modified
                    else None,
                }
            )
        return robot_accounts

    def export_offering_user_groups(self):
        """Export offering user group data.

        OfferingUserGroup has no UUID field; the import side matches groups
        by the natural key (offering, backend_metadata.gid).
        """
        offering_user_groups = []
        for group in (
            OfferingUserGroup.objects.select_related("offering")
            .prefetch_related("projects")
            .order_by("created")
        ):
            offering_user_groups.append(
                {
                    "offering_uuid": group.offering.uuid.hex,
                    "offering_name": group.offering.name,
                    "backend_metadata": group.backend_metadata,
                    "project_uuids": [
                        project.uuid.hex for project in group.projects.all()
                    ],
                    "created": group.created.isoformat() if group.created else None,
                    "modified": group.modified.isoformat() if group.modified else None,
                }
            )
        return offering_user_groups

    def export_checklists(self):
        """Export checklist data."""
        checklists = []
        for checklist in Checklist.objects.order_by("name"):
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

    def export_question_options(self):
        """Export question option data for select-type questions."""
        options = []
        for option in QuestionOption.objects.select_related("question").order_by(
            "question", "order"
        ):
            options.append(
                {
                    "uuid": option.uuid.hex,
                    "question_uuid": option.question.uuid.hex,
                    "label": option.label,
                    "order": option.order,
                }
            )
        return options

    def export_question_dependencies(self):
        """Export question dependency data for conditional visibility."""
        dependencies = []
        for dep in QuestionDependency.objects.select_related(
            "question", "depends_on_question"
        ).order_by("created"):
            dependencies.append(
                {
                    "uuid": dep.uuid.hex,
                    "question_uuid": dep.question.uuid.hex,
                    "depends_on_question_uuid": dep.depends_on_question.uuid.hex,
                    "required_answer_value": dep.required_answer_value,
                    "operator": dep.operator,
                }
            )
        return dependencies

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

    def export_invitations(self):
        """Export invitation data."""
        invitations = []
        for invitation in Invitation.objects.select_related(
            "customer", "role", "created_by", "approved_by"
        ).order_by("created"):
            invitation_data = {
                "uuid": invitation.uuid.hex,
                "customer_uuid": invitation.customer.uuid.hex,
                "customer_name": invitation.customer.name,
                "role_uuid": invitation.role.uuid.hex,
                "role_name": invitation.role.name,
                "email": invitation.email,
                "civil_number": invitation.civil_number,
                "full_name": invitation.full_name,
                "state": invitation.state,
                "execution_state": invitation.execution_state,
                "extra_invitation_text": invitation.extra_invitation_text,
                "error_message": invitation.error_message,
                "error_traceback": invitation.error_traceback,
                "created": invitation.created.isoformat()
                if invitation.created
                else None,
                "modified": invitation.modified.isoformat()
                if invitation.modified
                else None,
            }

            # Add created_by reference if it exists
            if invitation.created_by:
                invitation_data["created_by_uuid"] = invitation.created_by.uuid.hex
                invitation_data["created_by_username"] = invitation.created_by.username

            # Add approved_by reference if it exists
            if invitation.approved_by:
                invitation_data["approved_by_uuid"] = invitation.approved_by.uuid.hex
                invitation_data["approved_by_username"] = (
                    invitation.approved_by.username
                )

            # Add scope information
            invitation_data["scope_content_type"] = (
                f"{invitation.content_type.app_label}.{invitation.content_type.model}"
                if invitation.content_type
                else None
            )
            invitation_data["scope_object_id"] = invitation.object_id

            invitations.append(invitation_data)
        return invitations

    def export_group_invitations(self):
        """Export group invitation data."""
        group_invitations = []
        for group_invitation in GroupInvitation.objects.select_related(
            "customer", "role", "project_role", "created_by"
        ).order_by("created"):
            group_invitation_data = {
                "uuid": group_invitation.uuid.hex,
                "customer_uuid": group_invitation.customer.uuid.hex,
                "customer_name": group_invitation.customer.name,
                "role_uuid": group_invitation.role.uuid.hex,
                "role_name": group_invitation.role.name,
                "is_active": group_invitation.is_active,
                "is_public": group_invitation.is_public,
                "auto_create_project": group_invitation.auto_create_project,
                "created": group_invitation.created.isoformat()
                if group_invitation.created
                else None,
                "modified": group_invitation.modified.isoformat()
                if group_invitation.modified
                else None,
            }

            # Add project_role reference if it exists
            if group_invitation.project_role:
                group_invitation_data["project_role_uuid"] = (
                    group_invitation.project_role.uuid.hex
                )
                group_invitation_data["project_role_name"] = (
                    group_invitation.project_role.name
                )

            # Add created_by reference if it exists
            if group_invitation.created_by:
                group_invitation_data["created_by_uuid"] = (
                    group_invitation.created_by.uuid.hex
                )
                group_invitation_data["created_by_username"] = (
                    group_invitation.created_by.username
                )

            # Add scope information
            group_invitation_data["scope_content_type"] = (
                f"{group_invitation.content_type.app_label}.{group_invitation.content_type.model}"
                if group_invitation.content_type
                else None
            )
            group_invitation_data["scope_object_id"] = group_invitation.object_id

            group_invitations.append(group_invitation_data)
        return group_invitations

    def export_permission_requests(self):
        """Export permission request data."""
        permission_requests = []
        for permission_request in PermissionRequest.objects.select_related(
            "invitation", "created_by", "reviewed_by"
        ).order_by("created"):
            permission_request_data = {
                "uuid": permission_request.uuid.hex,
                "invitation_uuid": permission_request.invitation.uuid.hex,
                "created_by_uuid": permission_request.created_by.uuid.hex,
                "created_by_username": permission_request.created_by.username,
                "state": permission_request.state,
                "created": permission_request.created.isoformat()
                if permission_request.created
                else None,
                "modified": permission_request.modified.isoformat()
                if permission_request.modified
                else None,
                "review_comment": permission_request.review_comment,
            }

            # Add reviewed_by reference if it exists
            if permission_request.reviewed_by:
                permission_request_data["reviewed_by_uuid"] = (
                    permission_request.reviewed_by.uuid.hex
                )
                permission_request_data["reviewed_by_username"] = (
                    permission_request.reviewed_by.username
                )

            # Add reviewed_at if it exists
            if permission_request.reviewed_at:
                permission_request_data["reviewed_at"] = (
                    permission_request.reviewed_at.isoformat()
                )

            permission_requests.append(permission_request_data)
        return permission_requests

    def export_customer_credits(self):
        """Export customer credit data."""
        customer_credits = []
        for credit in CustomerCredit.objects.select_related("customer").order_by(
            "customer__name"
        ):
            customer_credit_data = {
                "uuid": credit.uuid.hex,
                "customer_uuid": credit.customer.uuid.hex,
                "customer_name": credit.customer.name,
                "value": str(credit.value),
                "expected_consumption": str(credit.expected_consumption),
                "minimal_consumption_logic": credit.minimal_consumption_logic,
                "grace_coefficient": str(credit.grace_coefficient),
                "apply_as_minimal_consumption": credit.apply_as_minimal_consumption,
                "end_date": credit.end_date.isoformat() if credit.end_date else None,
                "created": credit.created.isoformat() if credit.created else None,
                "modified": credit.modified.isoformat() if credit.modified else None,
            }

            # Add offerings if any
            if credit.offerings.exists():
                customer_credit_data["offering_uuids"] = [
                    offering.uuid.hex for offering in credit.offerings.all()
                ]

            customer_credits.append(customer_credit_data)
        return customer_credits

    def export_project_credits(self):
        """Export project credit data."""
        project_credits = []
        for credit in ProjectCredit.objects.select_related(
            "project", "project__customer"
        ).order_by("project__name"):
            project_credits.append(
                {
                    "uuid": credit.uuid.hex,
                    "project_uuid": credit.project.uuid.hex,
                    "project_name": credit.project.name,
                    "customer_uuid": credit.project.customer.uuid.hex,
                    "customer_name": credit.project.customer.name,
                    "value": str(credit.value),
                    "expected_consumption": str(credit.expected_consumption),
                    "minimal_consumption_logic": credit.minimal_consumption_logic,
                    "grace_coefficient": str(credit.grace_coefficient),
                    "apply_as_minimal_consumption": credit.apply_as_minimal_consumption,
                    "end_date": credit.end_date.isoformat()
                    if credit.end_date
                    else None,
                    "mark_unused_credit_as_spent_on_project_termination": credit.mark_unused_credit_as_spent_on_project_termination,
                    "created": credit.created.isoformat() if credit.created else None,
                    "modified": credit.modified.isoformat()
                    if credit.modified
                    else None,
                }
            )
        return project_credits

    def export_user_agreements(self):
        """Export user agreement data (Terms of Service, Privacy Policy)."""
        user_agreements = []
        for agreement in UserAgreement.objects.all().order_by("agreement_type"):
            user_agreements.append(
                {
                    "uuid": agreement.uuid.hex,
                    "agreement_type": agreement.agreement_type,
                    "content": agreement.content,
                    "created": agreement.created.isoformat()
                    if agreement.created
                    else None,
                    "modified": agreement.modified.isoformat()
                    if agreement.modified
                    else None,
                }
            )
        return user_agreements

    def export_call_managing_organisations(self):
        """Export call managing organisation data."""
        organisations = []
        for org in CallManagingOrganisation.objects.select_related("customer").order_by(
            "customer__name"
        ):
            organisations.append(
                {
                    "uuid": org.uuid.hex,
                    "customer_uuid": org.customer.uuid.hex,
                    "customer_name": org.customer.name,
                    "name": org.name,
                    "description": org.description,
                    "created": org.created.isoformat() if org.created else None,
                    "modified": org.modified.isoformat() if org.modified else None,
                }
            )
        return organisations

    def export_calls(self):
        """Export call data."""
        calls = []
        for call in Call.objects.select_related(
            "manager", "manager__customer", "created_by"
        ).order_by("name"):
            calls.append(
                {
                    "uuid": call.uuid.hex,
                    "manager_uuid": call.manager.uuid.hex,
                    "manager_customer_name": call.manager.customer.name,
                    "name": call.name,
                    "description": call.description,
                    "state": call.state,
                    "created_by_uuid": call.created_by.uuid.hex
                    if call.created_by
                    else None,
                    "external_url": call.external_url,
                    "reviewer_identity_visible_to_submitters": call.reviewer_identity_visible_to_submitters,
                    "reviews_visible_to_submitters": call.reviews_visible_to_submitters,
                    "fixed_duration_in_days": call.fixed_duration_in_days,
                    "created": call.created.isoformat() if call.created else None,
                    "modified": call.modified.isoformat() if call.modified else None,
                }
            )
        return calls

    def export_requested_offerings(self):
        """Export requested offering data."""
        requested_offerings = []
        for ro in RequestedOffering.objects.select_related(
            "call", "offering", "plan", "created_by", "approved_by"
        ).order_by("call__name", "offering__name"):
            requested_offerings.append(
                {
                    "uuid": ro.uuid.hex,
                    "call_uuid": ro.call.uuid.hex,
                    "call_name": ro.call.name,
                    "offering_uuid": ro.offering.uuid.hex,
                    "offering_name": ro.offering.name,
                    "plan_uuid": ro.plan.uuid.hex if ro.plan else None,
                    "plan_name": ro.plan.name if ro.plan else None,
                    "state": ro.state,
                    "description": ro.description,
                    "created_by_uuid": ro.created_by.uuid.hex
                    if ro.created_by
                    else None,
                    "approved_by_uuid": ro.approved_by.uuid.hex
                    if ro.approved_by
                    else None,
                    "created": ro.created.isoformat() if ro.created else None,
                    "modified": ro.modified.isoformat() if ro.modified else None,
                }
            )
        return requested_offerings

    def export_call_resource_templates(self):
        """Export call resource template data."""
        templates = []
        for template in CallResourceTemplate.objects.select_related(
            "call", "requested_offering", "requested_offering__offering", "created_by"
        ).order_by("call__name", "name"):
            templates.append(
                {
                    "uuid": template.uuid.hex,
                    "call_uuid": template.call.uuid.hex,
                    "call_name": template.call.name,
                    "requested_offering_uuid": template.requested_offering.uuid.hex,
                    "offering_name": template.requested_offering.offering.name,
                    "name": template.name,
                    "description": template.description,
                    "attributes": template.attributes,
                    "limits": template.limits,
                    "is_required": template.is_required,
                    "created_by_uuid": template.created_by.uuid.hex
                    if template.created_by
                    else None,
                    "created": template.created.isoformat()
                    if template.created
                    else None,
                    "modified": template.modified.isoformat()
                    if template.modified
                    else None,
                }
            )
        return templates

    def export_rounds(self):
        """Export round data."""
        rounds = []
        for round_obj in Round.objects.select_related("call").order_by(
            "call__name", "start_time"
        ):
            rounds.append(
                {
                    "uuid": round_obj.uuid.hex,
                    "call_uuid": round_obj.call.uuid.hex,
                    "call_name": round_obj.call.name,
                    "start_time": round_obj.start_time.isoformat()
                    if round_obj.start_time
                    else None,
                    "cutoff_time": round_obj.cutoff_time.isoformat()
                    if round_obj.cutoff_time
                    else None,
                    "review_strategy": round_obj.review_strategy,
                    "deciding_entity": round_obj.deciding_entity,
                    "allocation_time": round_obj.allocation_time,
                    "review_duration_in_days": round_obj.review_duration_in_days,
                    "minimum_number_of_reviewers": round_obj.minimum_number_of_reviewers,
                    "minimal_average_scoring": str(round_obj.minimal_average_scoring)
                    if round_obj.minimal_average_scoring is not None
                    else None,
                    "allocation_date": round_obj.allocation_date.isoformat()
                    if round_obj.allocation_date
                    else None,
                    "slug": round_obj.slug,
                    "created": round_obj.created.isoformat()
                    if round_obj.created
                    else None,
                    "modified": round_obj.modified.isoformat()
                    if round_obj.modified
                    else None,
                }
            )
        return rounds

    def export_proposals(self):
        """Export proposal data."""
        proposals = []
        for proposal in Proposal.objects.select_related(
            "round", "round__call", "project", "created_by", "approved_by"
        ).order_by("round__call__name", "name"):
            proposals.append(
                {
                    "uuid": proposal.uuid.hex,
                    "round_uuid": proposal.round.uuid.hex,
                    "call_name": proposal.round.call.name,
                    "name": proposal.name,
                    "description": proposal.description,
                    "state": proposal.state,
                    "project_uuid": proposal.project.uuid.hex
                    if proposal.project
                    else None,
                    "project_name": proposal.project.name if proposal.project else None,
                    "created_by_uuid": proposal.created_by.uuid.hex
                    if proposal.created_by
                    else None,
                    "approved_by_uuid": proposal.approved_by.uuid.hex
                    if proposal.approved_by
                    else None,
                    "duration_in_days": proposal.duration_in_days,
                    "project_summary": proposal.project_summary,
                    "project_duration": proposal.project_duration,
                    "project_is_confidential": proposal.project_is_confidential,
                    "project_has_civilian_purpose": proposal.project_has_civilian_purpose,
                    "allocation_comment": proposal.allocation_comment,
                    "slug": proposal.slug,
                    "created": proposal.created.isoformat()
                    if proposal.created
                    else None,
                    "modified": proposal.modified.isoformat()
                    if proposal.modified
                    else None,
                }
            )
        return proposals

    def export_requested_resources(self):
        """Export requested resource data."""
        requested_resources = []
        for rr in RequestedResource.objects.select_related(
            "proposal",
            "requested_offering",
            "requested_offering__offering",
            "call_resource_template",
            "created_by",
            "resource",
        ).order_by("proposal__name"):
            requested_resources.append(
                {
                    "uuid": rr.uuid.hex,
                    "proposal_uuid": rr.proposal.uuid.hex,
                    "proposal_name": rr.proposal.name,
                    "requested_offering_uuid": rr.requested_offering.uuid.hex,
                    "offering_name": rr.requested_offering.offering.name,
                    "call_resource_template_uuid": rr.call_resource_template.uuid.hex
                    if rr.call_resource_template
                    else None,
                    "description": rr.description,
                    "attributes": rr.attributes,
                    "limits": rr.limits,
                    "created_by_uuid": rr.created_by.uuid.hex
                    if rr.created_by
                    else None,
                    "resource_uuid": rr.resource.uuid.hex if rr.resource else None,
                    "created": rr.created.isoformat() if rr.created else None,
                    "modified": rr.modified.isoformat() if rr.modified else None,
                }
            )
        return requested_resources

    def export_reviews(self):
        """Export review data."""
        reviews = []
        for review in Review.objects.select_related(
            "proposal", "proposal__round__call", "reviewer"
        ).order_by("proposal__name", "reviewer__username"):
            reviews.append(
                {
                    "uuid": review.uuid.hex,
                    "proposal_uuid": review.proposal.uuid.hex,
                    "proposal_name": review.proposal.name,
                    "reviewer_uuid": review.reviewer.uuid.hex,
                    "reviewer_username": review.reviewer.username,
                    "state": review.state,
                    "summary_score": review.summary_score,
                    "summary_public_comment": review.summary_public_comment,
                    "summary_private_comment": review.summary_private_comment,
                    "comment_project_title": review.comment_project_title,
                    "comment_project_summary": review.comment_project_summary,
                    "comment_project_description": review.comment_project_description,
                    "comment_project_duration": review.comment_project_duration,
                    "comment_project_is_confidential": review.comment_project_is_confidential,
                    "comment_project_has_civilian_purpose": review.comment_project_has_civilian_purpose,
                    "comment_project_supporting_documentation": review.comment_project_supporting_documentation,
                    "comment_resource_requests": review.comment_resource_requests,
                    "comment_team": review.comment_team,
                    "created": review.created.isoformat() if review.created else None,
                    "modified": review.modified.isoformat()
                    if review.modified
                    else None,
                }
            )
        return reviews

    def export_assignment_batches(self):
        """Export assignment batch data."""
        batches = []
        for batch in AssignmentBatch.objects.select_related(
            "call", "reviewer_pool_entry", "reviewer_pool_entry__reviewer", "created_by"
        ).order_by("call__name", "created"):
            batches.append(
                {
                    "uuid": batch.uuid.hex,
                    "call_uuid": batch.call.uuid.hex,
                    "call_name": batch.call.name,
                    "reviewer_pool_entry_uuid": batch.reviewer_pool_entry.uuid.hex,
                    "reviewer_uuid": batch.reviewer_pool_entry.reviewer.uuid.hex
                    if batch.reviewer_pool_entry.reviewer
                    else None,
                    "status": batch.status,
                    "sent_at": batch.sent_at.isoformat() if batch.sent_at else None,
                    "expires_at": batch.expires_at.isoformat()
                    if batch.expires_at
                    else None,
                    "responded_at": batch.responded_at.isoformat()
                    if batch.responded_at
                    else None,
                    "source": batch.source,
                    "created_by_uuid": batch.created_by.uuid.hex
                    if batch.created_by
                    else None,
                    "invitation_token": batch.invitation_token,
                    "manager_notes": batch.manager_notes,
                    "created": batch.created.isoformat() if batch.created else None,
                    "modified": batch.modified.isoformat() if batch.modified else None,
                }
            )
        return batches

    def export_assignment_items(self):
        """Export assignment item data."""
        items = []
        for item in AssignmentItem.objects.select_related(
            "batch", "batch__call", "proposal", "review", "reassigned_from"
        ).order_by("batch__call__name", "batch__created", "proposal__name"):
            items.append(
                {
                    "uuid": item.uuid.hex,
                    "batch_uuid": item.batch.uuid.hex,
                    "proposal_uuid": item.proposal.uuid.hex,
                    "proposal_name": item.proposal.name,
                    "status": item.status,
                    "affinity_score": item.affinity_score,
                    "has_coi": item.has_coi,
                    "responded_at": item.responded_at.isoformat()
                    if item.responded_at
                    else None,
                    "decline_reason": item.decline_reason,
                    "review_uuid": item.review.uuid.hex if item.review else None,
                    "reassigned_from_uuid": item.reassigned_from.uuid.hex
                    if item.reassigned_from
                    else None,
                    "reassign_count": item.reassign_count,
                    "created": item.created.isoformat() if item.created else None,
                    "modified": item.modified.isoformat() if item.modified else None,
                }
            )
        return items

    def export_reviewer_suggestions(self):
        """Export reviewer suggestion data (algorithm-generated matches)."""
        suggestions = []
        for s in ReviewerSuggestion.objects.select_related(
            "call", "reviewer", "reviewer__user", "reviewed_by"
        ).order_by("call__name", "-affinity_score"):
            suggestions.append(
                {
                    "uuid": s.uuid.hex,
                    "call_uuid": s.call.uuid.hex,
                    "call_name": s.call.name,
                    "reviewer_uuid": s.reviewer.uuid.hex,
                    "reviewer_name": s.reviewer.user.full_name,
                    "affinity_score": s.affinity_score,
                    "keyword_score": s.keyword_score,
                    "text_score": s.text_score,
                    "status": s.status,
                    "reviewed_by_uuid": s.reviewed_by.uuid.hex
                    if s.reviewed_by
                    else None,
                    "reviewed_at": s.reviewed_at.isoformat() if s.reviewed_at else None,
                    "rejection_reason": s.rejection_reason,
                    "matched_keywords": s.matched_keywords,
                    "top_matching_proposals": s.top_matching_proposals,
                }
            )
        return suggestions

    def export_role_mappings(self):
        """Export proposal-to-project role mapping data."""
        mappings = []
        for m in ProposalProjectRoleMapping.objects.select_related(
            "call", "proposal_role", "project_role"
        ).order_by("call__name", "proposal_role__name"):
            mappings.append(
                {
                    "uuid": m.uuid.hex,
                    "call_uuid": m.call.uuid.hex,
                    "call_name": m.call.name,
                    "proposal_role": m.proposal_role.name,
                    "project_role": m.project_role.name if m.project_role else None,
                }
            )
        return mappings

    def export_maintenance_announcements(self):
        """Export maintenance announcement data."""
        announcements = []
        for announcement in MaintenanceAnnouncement.objects.select_related(
            "service_provider", "service_provider__customer", "created_by"
        ).order_by("-scheduled_start"):
            announcements.append(
                {
                    "uuid": announcement.uuid.hex,
                    "name": announcement.name,
                    "message": announcement.message,
                    "internal_notes": announcement.internal_notes,
                    "maintenance_type": announcement.maintenance_type,
                    "state": announcement.state,
                    "scheduled_start": announcement.scheduled_start.isoformat()
                    if announcement.scheduled_start
                    else None,
                    "scheduled_end": announcement.scheduled_end.isoformat()
                    if announcement.scheduled_end
                    else None,
                    "actual_start": announcement.actual_start.isoformat()
                    if announcement.actual_start
                    else None,
                    "actual_end": announcement.actual_end.isoformat()
                    if announcement.actual_end
                    else None,
                    "service_provider_uuid": announcement.service_provider.uuid.hex,
                    "created_by_uuid": announcement.created_by.uuid.hex
                    if announcement.created_by
                    else None,
                    "external_reference_url": announcement.external_reference_url,
                    "created": announcement.created.isoformat()
                    if announcement.created
                    else None,
                    "modified": announcement.modified.isoformat()
                    if announcement.modified
                    else None,
                }
            )
        return announcements

    def export_maintenance_announcement_offerings(self):
        """Export maintenance announcement offering data."""
        offerings = []
        for offering in MaintenanceAnnouncementOffering.objects.select_related(
            "maintenance", "offering"
        ).order_by("maintenance__scheduled_start", "offering__name"):
            offerings.append(
                {
                    "uuid": offering.uuid.hex,
                    "maintenance_uuid": offering.maintenance.uuid.hex,
                    "offering_uuid": offering.offering.uuid.hex,
                    "impact_level": offering.impact_level,
                    "impact_description": offering.impact_description,
                    "created": offering.created.isoformat()
                    if offering.created
                    else None,
                    "modified": offering.modified.isoformat()
                    if offering.modified
                    else None,
                }
            )
        return offerings

    def export_software_catalogs(self):
        """Export software catalog definitions (not package content)."""
        catalogs = []
        for catalog in SoftwareCatalog.objects.all().order_by("name", "catalog_type"):
            catalogs.append(
                {
                    "uuid": catalog.uuid.hex,
                    "name": catalog.name,
                    "version": catalog.version,
                    "catalog_type": catalog.catalog_type,
                    "source_url": catalog.source_url,
                    "description": catalog.description,
                    "metadata": catalog.metadata,
                    "auto_update_enabled": catalog.auto_update_enabled,
                    "last_update_attempt": catalog.last_update_attempt.isoformat()
                    if catalog.last_update_attempt
                    else None,
                    "last_successful_update": catalog.last_successful_update.isoformat()
                    if catalog.last_successful_update
                    else None,
                    "update_errors": catalog.update_errors,
                    "created": catalog.created.isoformat() if catalog.created else None,
                    "modified": catalog.modified.isoformat()
                    if catalog.modified
                    else None,
                }
            )
        return catalogs

    def export_offering_partitions(self):
        """Export offering partition data (SLURM partitions)."""
        partitions = []
        for partition in OfferingPartition.objects.select_related("offering").order_by(
            "offering__name", "partition_name"
        ):
            partitions.append(
                {
                    "uuid": partition.uuid.hex,
                    "offering_uuid": partition.offering.uuid.hex,
                    "offering_name": partition.offering.name,
                    "partition_name": partition.partition_name,
                    "cpu_arch": partition.cpu_arch,
                    "gpu_arch": partition.gpu_arch,
                    "cpu_bind": partition.cpu_bind,
                    "def_cpu_per_gpu": partition.def_cpu_per_gpu,
                    "max_cpus_per_node": partition.max_cpus_per_node,
                    "created": partition.created.isoformat()
                    if partition.created
                    else None,
                    "modified": partition.modified.isoformat()
                    if partition.modified
                    else None,
                }
            )
        return partitions

    def export_offering_software_catalogs(self):
        """Export offering-to-software-catalog links."""
        links = []
        for link in OfferingSoftwareCatalog.objects.select_related(
            "offering", "catalog", "partition"
        ).order_by("offering__name", "catalog__name"):
            link_data = {
                "uuid": link.uuid.hex,
                "offering_uuid": link.offering.uuid.hex,
                "offering_name": link.offering.name,
                "catalog_uuid": link.catalog.uuid.hex,
                "catalog_name": link.catalog.name,
                "enabled_cpu_family": link.enabled_cpu_family,
                "enabled_cpu_microarchitectures": link.enabled_cpu_microarchitectures,
                "created": link.created.isoformat() if link.created else None,
                "modified": link.modified.isoformat() if link.modified else None,
            }
            if link.partition:
                link_data["partition_uuid"] = link.partition.uuid.hex
            links.append(link_data)
        return links

    def export_openstack_service_settings(self):
        """Export OpenStack service settings."""
        settings_list = []
        for ss in ServiceSettings.objects.filter(type="OpenStack").select_related(
            "customer"
        ):
            settings_list.append(
                {
                    "uuid": ss.uuid.hex,
                    "name": ss.name,
                    "type": ss.type,
                    "backend_url": ss.backend_url or "",
                    "username": ss.username or "",
                    "password": ss.password or "",
                    "domain": ss.domain or "",
                    "token": ss.token or "",
                    "shared": ss.shared,
                    "options": ss.options,
                    "is_active": ss.is_active,
                    "state": ss.state,
                    "customer_uuid": ss.customer.uuid.hex if ss.customer else None,
                    "customer_name": ss.customer.name if ss.customer else None,
                }
            )
        return settings_list

    def export_openstack_flavors(self):
        """Export OpenStack flavors."""
        flavors = []
        for flavor in Flavor.objects.select_related("settings"):
            flavors.append(
                {
                    "uuid": flavor.uuid.hex,
                    "name": flavor.name,
                    "backend_id": flavor.backend_id,
                    "cores": flavor.cores,
                    "ram": flavor.ram,
                    "disk": flavor.disk,
                    "settings_uuid": flavor.settings.uuid.hex,
                    "settings_name": flavor.settings.name,
                }
            )
        return flavors

    def export_openstack_images(self):
        """Export OpenStack images."""
        images = []
        for image in Image.all_objects.select_related("settings"):
            images.append(
                {
                    "uuid": image.uuid.hex,
                    "name": image.name,
                    "backend_id": image.backend_id,
                    "min_disk": image.min_disk,
                    "min_ram": image.min_ram,
                    "settings_uuid": image.settings.uuid.hex,
                    "settings_name": image.settings.name,
                }
            )
        return images

    def export_openstack_tenants(self):
        """Export OpenStack tenants."""
        tenants = []
        for tenant in Tenant.objects.select_related("service_settings", "project"):
            tenants.append(
                {
                    "uuid": tenant.uuid.hex,
                    "name": tenant.name,
                    "description": tenant.description,
                    "backend_id": tenant.backend_id,
                    "state": tenant.state,
                    "runtime_state": tenant.runtime_state,
                    "service_settings_uuid": tenant.service_settings.uuid.hex,
                    "project_uuid": tenant.project.uuid.hex,
                    "project_name": tenant.project.name,
                    "internal_network_id": tenant.internal_network_id,
                    "external_network_id": tenant.external_network_id,
                    "availability_zone": tenant.availability_zone,
                    "user_username": tenant.user_username,
                    "user_password": tenant.user_password,
                }
            )
        return tenants

    def export_openstack_instances(self):
        """Export OpenStack instances."""
        instances = []
        for instance in Instance.objects.select_related(
            "tenant", "service_settings", "project"
        ):
            instances.append(
                {
                    "uuid": instance.uuid.hex,
                    "name": instance.name,
                    "description": instance.description,
                    "backend_id": instance.backend_id,
                    "state": instance.state,
                    "runtime_state": instance.runtime_state,
                    "tenant_uuid": instance.tenant.uuid.hex,
                    "cores": instance.cores,
                    "ram": instance.ram,
                    "disk": instance.disk,
                    "image_name": instance.image_name,
                    "flavor_name": instance.flavor_name,
                    "flavor_disk": instance.flavor_disk,
                    "hypervisor_hostname": instance.hypervisor_hostname,
                    "key_name": instance.key_name,
                    "key_fingerprint": instance.key_fingerprint,
                    "directly_connected_ips": instance.directly_connected_ips,
                }
            )
        return instances

    def export_openstack_volumes(self):
        """Export OpenStack volumes."""
        volumes = []
        for volume in Volume.objects.select_related(
            "tenant", "instance", "service_settings", "project"
        ):
            volumes.append(
                {
                    "uuid": volume.uuid.hex,
                    "name": volume.name,
                    "description": volume.description,
                    "backend_id": volume.backend_id,
                    "state": volume.state,
                    "runtime_state": volume.runtime_state,
                    "tenant_uuid": volume.tenant.uuid.hex,
                    "instance_uuid": volume.instance.uuid.hex
                    if volume.instance
                    else None,
                    "size": volume.size,
                    "bootable": volume.bootable,
                    "device": volume.device,
                    "image_name": volume.image_name,
                }
            )
        return volumes

    def export_project_estimated_cost_policies(self):
        """Export project estimated cost policies."""
        policies = []
        for policy in ProjectEstimatedCostPolicy.objects.select_related(
            "scope", "created_by"
        ).order_by("scope__name"):
            policies.append(
                {
                    "uuid": policy.uuid.hex,
                    "project_uuid": policy.scope.uuid.hex,
                    "project_name": policy.scope.name,
                    "limit_cost": policy.limit_cost,
                    "period": policy.period,
                    "actions": policy.actions,
                    "options": policy.options,
                    "has_fired": policy.has_fired,
                    "fired_datetime": policy.fired_datetime.isoformat()
                    if policy.fired_datetime
                    else None,
                    "created_by_uuid": policy.created_by.uuid.hex
                    if policy.created_by
                    else None,
                    "created": policy.created.isoformat() if policy.created else None,
                    "modified": policy.modified.isoformat()
                    if policy.modified
                    else None,
                }
            )
        return policies

    def export_customer_estimated_cost_policies(self):
        """Export customer estimated cost policies."""
        policies = []
        for policy in CustomerEstimatedCostPolicy.objects.select_related(
            "scope", "created_by"
        ).order_by("scope__name"):
            policies.append(
                {
                    "uuid": policy.uuid.hex,
                    "customer_uuid": policy.scope.uuid.hex,
                    "customer_name": policy.scope.name,
                    "limit_cost": policy.limit_cost,
                    "period": policy.period,
                    "actions": policy.actions,
                    "options": policy.options,
                    "has_fired": policy.has_fired,
                    "fired_datetime": policy.fired_datetime.isoformat()
                    if policy.fired_datetime
                    else None,
                    "created_by_uuid": policy.created_by.uuid.hex
                    if policy.created_by
                    else None,
                    "created": policy.created.isoformat() if policy.created else None,
                    "modified": policy.modified.isoformat()
                    if policy.modified
                    else None,
                }
            )
        return policies

    def export_slurm_periodic_policies(self):
        """Export SLURM periodic usage policies with component limits."""
        policies = []
        for policy in (
            SlurmPeriodicUsagePolicy.objects.select_related("scope", "created_by")
            .prefetch_related(
                "component_limits_set__component",
                "organization_groups",
            )
            .order_by("scope__name")
        ):
            component_limits = []
            for cl in policy.component_limits_set.all():
                component_limits.append(
                    {
                        "type": cl.component.type,
                        "limit": cl.limit,
                    }
                )
            policies.append(
                {
                    "uuid": policy.uuid.hex,
                    "offering_uuid": policy.scope.uuid.hex,
                    "offering_name": policy.scope.name,
                    "apply_to_all": policy.apply_to_all,
                    "actions": policy.actions,
                    "options": policy.options,
                    "limit_type": policy.limit_type,
                    "tres_billing_enabled": policy.tres_billing_enabled,
                    "tres_billing_weights": policy.tres_billing_weights,
                    "carryover_factor": policy.carryover_factor,
                    "grace_ratio": policy.grace_ratio,
                    "carryover_enabled": policy.carryover_enabled,
                    "raw_usage_reset": policy.raw_usage_reset,
                    "qos_strategy": policy.qos_strategy,
                    "has_fired": policy.has_fired,
                    "fired_datetime": policy.fired_datetime.isoformat()
                    if policy.fired_datetime
                    else None,
                    "created_by_uuid": policy.created_by.uuid.hex
                    if policy.created_by
                    else None,
                    "organization_group_uuids": [
                        og.uuid.hex for og in policy.organization_groups.all()
                    ],
                    "component_limits": component_limits,
                    "created": policy.created.isoformat() if policy.created else None,
                    "modified": policy.modified.isoformat()
                    if policy.modified
                    else None,
                }
            )
        return policies

    def export_events(self):
        """Export audit log events related to invoicing, credits and policies."""
        event_types = set()
        for group in [EventGroup.INVOICES, EventGroup.CREDITS]:
            event_types.update(
                item.value for item in EVENT_GROUP_MAPPING.get(group, [])
            )
        event_types.update(
            [
                EventType.POLICY_NOTIFICATION.value,
                EventType.SLURM_POLICY_EVALUATION.value,
                EventType.REQUEST_SLURM_RESOURCE_DOWNSCALING.value,
                EventType.REQUEST_SLURM_RESOURCE_PAUSING.value,
            ]
        )
        events = []
        for event in Event.objects.filter(event_type__in=sorted(event_types)).order_by(
            "created"
        ):
            events.append(
                {
                    "uuid": event.uuid.hex,
                    "event_type": event.event_type,
                    "message": event.message,
                    "context": event.context,
                    "created": event.created.isoformat(),
                }
            )
        return events
