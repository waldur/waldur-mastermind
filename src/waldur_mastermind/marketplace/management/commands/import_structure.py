import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.authtoken.models import Token

from waldur_core.checklist.models import (
    Answer,
    Checklist,
    ChecklistCompletion,
    Question,
    QuestionDependency,
    QuestionOption,
)
from waldur_core.core.features import FEATURES
from waldur_core.core.middleware import skip_side_effects
from waldur_core.core.models import Feature, SshPublicKey, User
from waldur_core.core.serializers import ConstanceSettingsSerializer
from waldur_core.logging.models import Event
from waldur_core.permissions.models import Role, RolePermission, UserRole
from waldur_core.permissions.tasks import sync_user_deactivation_status
from waldur_core.structure.models import (
    Customer,
    Project,
    ServiceSettings,
    UserAgreement,
)
from waldur_core.users.models import GroupInvitation, Invitation, PermissionRequest
from waldur_mastermind.invoices.models import (
    AffiliateFeeAccrual,
    CreditTransaction,
    CustomerAffiliate,
    CustomerCredit,
    Invoice,
    InvoiceItem,
    ProjectCredit,
)
from waldur_mastermind.marketplace.enums import LimitPeriods, RobotAccountStates
from waldur_mastermind.marketplace.models import (
    Category,
    CategoryGroup,
    ComponentUsage,
    ComponentUserUsage,
    CourseAccount,
    CustomerServiceAccount,
    MaintenanceAnnouncement,
    MaintenanceAnnouncementOffering,
    Offering,
    OfferingAccessEndpoint,
    OfferingComponent,
    OfferingPartition,
    OfferingSoftwareCatalog,
    OfferingUser,
    OfferingUserGroup,
    Order,
    Plan,
    PlanComponent,
    PosixIdPool,
    ProjectServiceAccount,
    Resource,
    ResourcePlanPeriod,
    ResourceProject,
    RobotAccount,
    ServiceProvider,
    SoftwareCatalog,
)
from waldur_mastermind.policy.models import (
    CustomerEstimatedCostPolicy,
    OfferingComponentLimit,
    ProjectEstimatedCostPolicy,
    SlurmCommandHistory,
    SlurmPeriodicUsagePolicy,
)
from waldur_mastermind.proposal.models import (
    AssignmentBatch,
    AssignmentItem,
    Call,
    CallCOIConfiguration,
    CallDocument,
    CallManagingOrganisation,
    CallResourceTemplate,
    CallReviewerPool,
    CallWorkflowStep,
    COIDisclosureForm,
    ConflictOfInterest,
    ExpertiseCategory,
    MatchingConfiguration,
    Proposal,
    ProposalProjectRoleMapping,
    ProposalWorkflowStepInstance,
    RequestedOffering,
    RequestedResource,
    Review,
    ReviewerAffiliation,
    ReviewerBid,
    ReviewerExpertise,
    ReviewerProfile,
    ReviewerProposalAffinity,
    ReviewerPublication,
    ReviewerStats,
    ReviewerSuggestion,
    Round,
)
from waldur_openstack.models import Flavor, Image, Instance, Tenant, Volume


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
    - User Management: Invitations, Group Invitations, Permission Requests

    The import maintains dependency order and uses transaction isolation for safety.
    RabbitMQ messages are automatically disabled during import to prevent billing issues.

    Usage:
        waldur import_structure -i structure.json
        waldur import_structure --input structure.json --update
        waldur import_structure -i structure.json --skip-users --dry-run
        waldur import_structure -i structure.json --skip-rabbitmq-messages --skip-roles
    """

    @staticmethod
    def _normalize_uuid(uuid_str):
        """Normalize a UUID string by removing hyphens.

        Waldur uses StringUUID whose __str__ returns hex (no hyphens),
        but exported data may contain hyphenated UUIDs. This ensures
        consistent dict key format for pre-fetched lookup maps.
        """
        if uuid_str is None:
            return None
        return str(uuid_str).replace("-", "")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stats = {
            "features": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
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
            "category_groups": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "categories": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "offerings": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "project_estimated_cost_policies": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "customer_estimated_cost_policies": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "slurm_periodic_policies": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "slurm_command_history": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "roles": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "role_permissions": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "user_roles": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "resource_projects": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "posix_id_pools": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
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
            "component_user_usages": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "orders": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "invoices": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "invoice_items": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "offering_users": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "robot_accounts": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "offering_user_groups": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "checklists": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "questions": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "question_options": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "question_dependencies": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "checklist_completions": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "answers": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "invitations": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "group_invitations": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "permission_requests": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "customer_credits": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "project_credits": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "customer_affiliates": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "credit_transactions": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "affiliate_fee_accruals": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "events": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "ssh_public_keys": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "offering_endpoints": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "user_agreements": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "constance_settings": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "call_managing_organisations": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "calls": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "call_documents": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "requested_offerings": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "call_resource_templates": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "rounds": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "call_workflow_steps": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "proposals": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "proposal_workflow_step_instances": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "requested_resources": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "reviews": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "reviewer_profiles": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "reviewer_affiliations": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "expertise_categories": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "reviewer_expertise": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "reviewer_publications": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "reviewer_stats": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "call_coi_configurations": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "conflicts_of_interest": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "coi_disclosure_forms": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "call_reviewer_pools": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "matching_configurations": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "reviewer_proposal_affinities": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "reviewer_bids": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
            "reviewer_suggestions": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "role_mappings": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "assignment_batches": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "assignment_items": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "maintenance_announcements": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "maintenance_announcement_offerings": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "software_catalogs": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "offering_partitions": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "offering_software_catalogs": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "openstack_service_settings": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "openstack_flavors": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "openstack_images": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "openstack_tenants": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "openstack_instances": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
            "openstack_volumes": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            },
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

        # Features have no entity dependencies; import first so that the
        # rest of the load (and the running API) sees the right flags.
        self._safe_import(
            "features", lambda: self.import_features(data.get("features", []))
        )

        if not skip_users:
            self._safe_import("users", lambda: self.import_users(data.get("users", [])))
            self._safe_import(
                "auth_tokens",
                lambda: self.import_auth_tokens(data.get("auth_tokens", [])),
            )
            self._safe_import(
                "ssh_public_keys",
                lambda: self.import_ssh_public_keys(data.get("ssh_public_keys", [])),
            )

            # Import expertise categories early (no dependencies)
            self._safe_import(
                "expertise_categories",
                lambda: self.import_expertise_categories(
                    data.get("expertise_categories", [])
                ),
            )

            # Import reviewer profiles (depends on users)
            self._safe_import(
                "reviewer_profiles",
                lambda: self.import_reviewer_profiles(
                    data.get("reviewer_profiles", [])
                ),
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

        # Import reviewer-related entities (depends on users, customers, expertise_categories)
        self._safe_import(
            "reviewer_affiliations",
            lambda: self.import_reviewer_affiliations(
                data.get("reviewer_affiliations", [])
            ),
        )
        self._safe_import(
            "reviewer_expertise",
            lambda: self.import_reviewer_expertise(data.get("reviewer_expertise", [])),
        )
        self._safe_import(
            "reviewer_publications",
            lambda: self.import_reviewer_publications(
                data.get("reviewer_publications", [])
            ),
        )
        self._safe_import(
            "reviewer_stats",
            lambda: self.import_reviewer_stats(data.get("reviewer_stats", [])),
        )

        self._safe_import(
            "category_groups",
            lambda: self.import_category_groups(data.get("category_groups", [])),
        )
        self._safe_import(
            "categories", lambda: self.import_categories(data.get("categories", []))
        )

        # Import OpenStack backend models (before offerings, so offering scope linking works)
        self._safe_import(
            "openstack_service_settings",
            lambda: self.import_openstack_service_settings(
                data.get("openstack_service_settings", [])
            ),
        )
        self._safe_import(
            "openstack_flavors",
            lambda: self.import_openstack_flavors(data.get("openstack_flavors", [])),
        )
        self._safe_import(
            "openstack_images",
            lambda: self.import_openstack_images(data.get("openstack_images", [])),
        )
        self._safe_import(
            "openstack_tenants",
            lambda: self.import_openstack_tenants(data.get("openstack_tenants", [])),
        )

        self._safe_import(
            "offerings", lambda: self.import_offerings(data.get("offerings", []))
        )
        self._safe_import(
            "offering_endpoints",
            lambda: self.import_offering_endpoints(data.get("offering_endpoints", [])),
        )

        # Import maintenance announcements (depends on service_providers and users)
        self._safe_import(
            "maintenance_announcements",
            lambda: self.import_maintenance_announcements(
                data.get("maintenance_announcements", [])
            ),
        )
        # Import maintenance announcement offerings (depends on maintenance_announcements and offerings)
        self._safe_import(
            "maintenance_announcement_offerings",
            lambda: self.import_maintenance_announcement_offerings(
                data.get("maintenance_announcement_offerings", [])
            ),
        )

        # Import software catalogs
        self._safe_import(
            "software_catalogs",
            lambda: self.import_software_catalogs(data.get("software_catalogs", [])),
        )

        # Import offering partitions
        self._safe_import(
            "offering_partitions",
            lambda: self.import_offering_partitions(
                data.get("offering_partitions", [])
            ),
        )

        # Import offering-software-catalog links
        self._safe_import(
            "offering_software_catalogs",
            lambda: self.import_offering_software_catalogs(
                data.get("offering_software_catalogs", [])
            ),
        )

        # Import SLURM periodic policies (depends on offerings)
        self._safe_import(
            "slurm_periodic_policies",
            lambda: self.import_slurm_periodic_policies(
                data.get("slurm_periodic_policies", [])
            ),
        )

        # Import cost policies (depends on projects and customers)
        self._safe_import(
            "project_estimated_cost_policies",
            lambda: self.import_project_estimated_cost_policies(
                data.get("project_estimated_cost_policies", [])
            ),
        )
        self._safe_import(
            "customer_estimated_cost_policies",
            lambda: self.import_customer_estimated_cost_policies(
                data.get("customer_estimated_cost_policies", [])
            ),
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

        # Import OpenStack instances and volumes (before resources, so resource scope linking works)
        self._safe_import(
            "openstack_instances",
            lambda: self.import_openstack_instances(
                data.get("openstack_instances", [])
            ),
        )
        self._safe_import(
            "openstack_volumes",
            lambda: self.import_openstack_volumes(data.get("openstack_volumes", [])),
        )

        # Import resources (depends on offerings, plans, projects)
        self._safe_import(
            "resources", lambda: self.import_resources(data.get("resources", []))
        )

        # Import resource projects (sub-projects of a resource; depends on resources)
        self._safe_import(
            "resource_projects",
            lambda: self.import_resource_projects(data.get("resource_projects", [])),
        )

        # Import POSIX ID pools (depends on offerings / service providers)
        self._safe_import(
            "posix_id_pools",
            lambda: self.import_posix_id_pools(data.get("posix_id_pools", [])),
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
        self._safe_import(
            "component_user_usages",
            lambda: self.import_component_user_usages(
                data.get("component_user_usages", [])
            ),
        )

        # Import SLURM command history (depends on resources and slurm_periodic_policies)
        self._safe_import(
            "slurm_command_history",
            lambda: self.import_slurm_command_history(
                data.get("slurm_command_history", [])
            ),
        )

        # Import orders (depends on resources, projects, users, plans)
        self._safe_import("orders", lambda: self.import_orders(data.get("orders", [])))

        if not skip_roles:
            self._safe_import("roles", lambda: self.import_roles(data.get("roles", [])))
            self._safe_import(
                "role_permissions",
                lambda: self.import_role_permissions(data.get("role_permissions", [])),
            )

        # Import checklist data BEFORE calls (calls may reference compliance checklists)
        # Dependency order: checklists -> questions -> options -> dependencies
        self._safe_import(
            "checklists", lambda: self.import_checklists(data.get("checklists", []))
        )
        self._safe_import(
            "questions", lambda: self.import_questions(data.get("questions", []))
        )
        self._safe_import(
            "question_options",
            lambda: self.import_question_options(data.get("question_options", [])),
        )
        self._safe_import(
            "question_dependencies",
            lambda: self.import_question_dependencies(
                data.get("question_dependencies", [])
            ),
        )

        # Import proposal/call management data BEFORE user_roles (user_roles may scope to Calls)
        # Dependency order: CMO -> calls -> offerings -> templates -> rounds -> proposals -> resources -> reviews
        self._safe_import(
            "call_managing_organisations",
            lambda: self.import_call_managing_organisations(
                data.get("call_managing_organisations", [])
            ),
        )
        self._safe_import(
            "calls",
            lambda: self.import_calls(data.get("calls", [])),
        )
        # Documentation files attached to calls (depends on calls)
        self._safe_import(
            "call_documents",
            lambda: self.import_call_documents(data.get("call_documents", [])),
        )

        # Import call configurations (depends on calls)
        self._safe_import(
            "call_coi_configurations",
            lambda: self.import_call_coi_configurations(
                data.get("call_coi_configurations", [])
            ),
        )
        self._safe_import(
            "matching_configurations",
            lambda: self.import_matching_configurations(
                data.get("matching_configurations", [])
            ),
        )

        # Import call reviewer pools (depends on calls and reviewer_profiles)
        self._safe_import(
            "call_reviewer_pools",
            lambda: self.import_call_reviewer_pools(
                data.get("call_reviewer_pools", [])
            ),
        )

        self._safe_import(
            "requested_offerings",
            lambda: self.import_requested_offerings(
                data.get("requested_offerings", [])
            ),
        )
        self._safe_import(
            "call_resource_templates",
            lambda: self.import_call_resource_templates(
                data.get("call_resource_templates", [])
            ),
        )
        self._safe_import(
            "rounds",
            lambda: self.import_rounds(data.get("rounds", [])),
        )
        # Configure the per-call workflow steps. Every call already gets a full
        # set of CallWorkflowStep rows auto-seeded by a post-save signal on
        # creation; this overrides their enable/transition/blind-review config
        # for calls that want a non-default workflow (e.g. enabling the review
        # steps). Matched by (call, step), not uuid.
        self._safe_import(
            "call_workflow_steps",
            lambda: self.import_call_workflow_steps(
                data.get("call_workflow_steps", [])
            ),
        )
        self._safe_import(
            "proposals",
            lambda: self.import_proposals(data.get("proposals", [])),
        )
        self._safe_import(
            "requested_resources",
            lambda: self.import_requested_resources(
                data.get("requested_resources", [])
            ),
        )
        self._safe_import(
            "reviews",
            lambda: self.import_reviews(data.get("reviews", [])),
        )
        # Per-proposal workflow engine state. Created only by the submit action
        # at runtime, so preset proposals (imported directly into their target
        # state) have none — seed them here so the engine is demonstrable.
        # Depends on proposals (FK) and users (completed_by FK).
        self._safe_import(
            "proposal_workflow_step_instances",
            lambda: self.import_proposal_workflow_step_instances(
                data.get("proposal_workflow_step_instances", [])
            ),
        )

        # Import COI and matching data (depends on reviewer_profiles, proposals, calls)
        self._safe_import(
            "conflicts_of_interest",
            lambda: self.import_conflicts_of_interest(
                data.get("conflicts_of_interest", [])
            ),
        )
        self._safe_import(
            "coi_disclosure_forms",
            lambda: self.import_coi_disclosure_forms(
                data.get("coi_disclosure_forms", [])
            ),
        )
        self._safe_import(
            "reviewer_proposal_affinities",
            lambda: self.import_reviewer_proposal_affinities(
                data.get("reviewer_proposal_affinities", [])
            ),
        )
        self._safe_import(
            "reviewer_bids",
            lambda: self.import_reviewer_bids(data.get("reviewer_bids", [])),
        )
        self._safe_import(
            "reviewer_suggestions",
            lambda: self.import_reviewer_suggestions(
                data.get("reviewer_suggestions", [])
            ),
        )

        # Import assignment batches and items (Stage 2 of two-stage reviewer workflow)
        # Depends on: call_reviewer_pools, proposals
        self._safe_import(
            "assignment_batches",
            lambda: self.import_assignment_batches(data.get("assignment_batches", [])),
        )
        self._safe_import(
            "assignment_items",
            lambda: self.import_assignment_items(data.get("assignment_items", [])),
        )

        # Import user_roles AFTER proposal entities (user_roles may scope to Calls)
        self._safe_import(
            "user_roles", lambda: self.import_user_roles(data.get("user_roles", []))
        )

        # Import role mappings AFTER user_roles (user_roles import triggers
        # creation of PROPOSAL.MEMBER and other roles via post-save signals)
        self._safe_import(
            "role_mappings",
            lambda: self.import_role_mappings(data.get("role_mappings", [])),
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

        # Import invoicing (depends on customers, resources, projects).
        # Credits are imported BEFORE invoice items so that compensation rows
        # (InvoiceItem.credit_uuid → CustomerCredit) can resolve the FK during
        # invoice-item creation. Without this order the credit FK ends up null,
        # which makes the loader misclassify compensations as manual refunds.
        self._safe_import(
            "customer_credits",
            lambda: self.import_customer_credits(data.get("customer_credits", [])),
        )
        self._safe_import(
            "project_credits",
            lambda: self.import_project_credits(data.get("project_credits", [])),
        )
        self._safe_import(
            "invoices", lambda: self.import_invoices(data.get("invoices", []))
        )
        self._safe_import(
            "invoice_items",
            lambda: self.import_invoice_items(data.get("invoice_items", [])),
        )

        # Affiliate program (depends on customers, credits and invoices).
        self._safe_import(
            "customer_affiliates",
            lambda: self.import_customer_affiliates(
                data.get("customer_affiliates", [])
            ),
        )
        self._safe_import(
            "credit_transactions",
            lambda: self.import_credit_transactions(
                data.get("credit_transactions", [])
            ),
        )
        self._safe_import(
            "affiliate_fee_accruals",
            lambda: self.import_affiliate_fee_accruals(
                data.get("affiliate_fee_accruals", [])
            ),
        )

        # Import offering users (depends on offerings and users)
        self._safe_import(
            "offering_users",
            lambda: self.import_offering_users(data.get("offering_users", [])),
        )

        # Import robot accounts (depends on resources and users)
        self._safe_import(
            "robot_accounts",
            lambda: self.import_robot_accounts(data.get("robot_accounts", [])),
        )

        # Import offering user groups (depends on offerings and projects)
        self._safe_import(
            "offering_user_groups",
            lambda: self.import_offering_user_groups(
                data.get("offering_user_groups", [])
            ),
        )

        # Import checklist data (dependency order: checklists -> questions -> completions -> answers)
        self._safe_import(
            "checklists", lambda: self.import_checklists(data.get("checklists", []))
        )
        self._safe_import(
            "questions", lambda: self.import_questions(data.get("questions", []))
        )
        self._safe_import(
            "question_options",
            lambda: self.import_question_options(data.get("question_options", [])),
        )
        self._safe_import(
            "question_dependencies",
            lambda: self.import_question_dependencies(
                data.get("question_dependencies", [])
            ),
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

        # Import user management data (after roles and users are ready)
        self._safe_import(
            "group_invitations",
            lambda: self.import_group_invitations(data.get("group_invitations", [])),
        )
        self._safe_import(
            "invitations", lambda: self.import_invitations(data.get("invitations", []))
        )
        self._safe_import(
            "permission_requests",
            lambda: self.import_permission_requests(
                data.get("permission_requests", [])
            ),
        )

        # (customer_credits / project_credits already imported above so
        # invoice_items can resolve the credit FK on compensation rows.)

        # Import events last (after all entities exist)
        self._safe_import(
            "events",
            lambda: self.import_events(data.get("events", [])),
        )

        # Import constance settings (system configuration)
        self._safe_import(
            "constance_settings",
            lambda: self.import_constance_settings(data.get("constance_settings", {})),
        )

        # Import user agreements (terms of service, privacy policy)
        self._safe_import(
            "user_agreements",
            lambda: self.import_user_agreements(data.get("user_agreements", [])),
        )

        # Re-apply authored resource state flags LAST. Importing usages and
        # invoices fires policy re-evaluation, which can clear a resource's
        # paused/downscaled flag when the mid-import spend snapshot differs
        # from the fully-loaded state the preset author intended. A signal-
        # free update at the very end restores the authored values.
        self._safe_import(
            "resource_state_flags",
            lambda: self._apply_resource_state_flags(data.get("resources", [])),
        )

    def _apply_resource_state_flags(self, resources_data):
        """Force authored paused/downscaled/restrict_member_access onto resources.

        Uses queryset ``.update()`` so no post_save signals fire (no policy
        re-evaluation). Only touches resources whose preset entry actually
        carries one of the flags.
        """
        if self.dry_run:
            return
        for resource_data in resources_data:
            uuid = resource_data.get("uuid")
            if not uuid:
                continue
            flags = {
                key: resource_data[key]
                for key in ("paused", "downscaled", "restrict_member_access")
                if key in resource_data
            }
            if not flags:
                continue
            Resource.objects.filter(uuid=uuid).update(**flags)

    def _parse_datetime(self, value):
        """Parse a datetime string, supporting both ISO format and relative offsets.

        Relative offsets use format 'relative:+30days' or 'relative:-15days'.
        Returns a timezone-aware datetime or None if parsing fails.
        """
        if not value or not isinstance(value, str):
            return None

        if value.startswith("relative:"):
            offset_str = value[9:]  # Remove 'relative:' prefix
            try:
                import re

                match = re.match(r"([+-]?\d+)(days?|hours?|minutes?)", offset_str)
                if match:
                    amount = int(match.group(1))
                    unit = match.group(2).rstrip("s")  # Normalize to singular

                    now = timezone.now()
                    if unit == "day":
                        return now + timedelta(days=amount)
                    elif unit == "hour":
                        return now + timedelta(hours=amount)
                    elif unit == "minute":
                        return now + timedelta(minutes=amount)
            except (ValueError, TypeError):
                pass
            return None

        # Try to parse as ISO format
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            return dt
        except (ValueError, TypeError):
            return None

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

    def import_features(self, features_data):
        """Import feature flag values into the Feature model.

        Each entry is ``{"key": "<section>.<feature>", "value": <bool>}``.
        Unknown keys are reported and skipped. After writes, the cached
        ``/api/configuration/`` payload is invalidated so a running API
        picks up the new values immediately.
        """
        if not features_data:
            return

        self.stdout.write("Importing features...")

        valid_keys = {
            f"{section['key']}.{feature['key']}"
            for section in FEATURES
            for feature in section["items"]
        }
        touched = False
        for entry in features_data:
            key = entry.get("key")
            if key is None or "value" not in entry:
                self.stdout.write(
                    self.style.WARNING("Skipping feature without key/value")
                )
                self.stats["features"]["errors"] += 1
                continue
            if key not in valid_keys:
                self.stdout.write(self.style.WARNING(f"Unknown feature key: {key}"))
                self.stats["features"]["errors"] += 1
                continue
            value = bool(entry["value"])
            obj, created = Feature.objects.get_or_create(
                key=key, defaults={"value": value}
            )
            if created:
                self.stats["features"]["created"] += 1
                touched = True
            elif obj.value != value:
                obj.value = value
                obj.save(update_fields=["value"])
                self.stats["features"]["updated"] += 1
                touched = True
            else:
                self.stats["features"]["skipped"] += 1

        if touched:
            # Drop the cached public configuration so consumers see the new
            # flag values without a backend restart.
            from django.core.cache import cache

            cache.delete("API_CONFIGURATION")

    def import_users(self, users_data):
        """Import user data including system_robot."""
        self.stdout.write("Importing users...")

        # Pre-fetch lookup maps to avoid N+1 queries
        user_by_uuid = {str(u.uuid): u for u in User.all_objects.all()}
        user_by_username = {u.username: u for u in user_by_uuid.values()}

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

                # Use pre-fetched maps instead of per-item DB queries
                existing_user = user_by_uuid.get(self._normalize_uuid(uuid))

                # Also check if username already exists (even with different UUID)
                if not existing_user:
                    username_conflict = user_by_username.get(username)
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
                            # Use existing user with same username but different UUID
                            existing_user = username_conflict
                            self.stdout.write(
                                self.style.WARNING(
                                    f"User {uuid}: username '{username}' already exists with UUID {username_conflict.uuid}, will update existing user"
                                )
                            )

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
                        existing_user.deactivation_reason = user_data.get(
                            "deactivation_reason", ""
                        )

                        # Additional fields
                        if "token_lifetime" in user_data:
                            token_lifetime = user_data.get("token_lifetime")
                            existing_user.token_lifetime = (
                                None if token_lifetime == -1 else token_lifetime
                            )
                        existing_user.details = user_data.get("details", {})
                        existing_user.notifications_enabled = user_data.get(
                            "notifications_enabled", True
                        )
                        existing_user.is_identity_manager = user_data.get(
                            "is_identity_manager", False
                        )
                        existing_user.managed_isds = user_data.get("managed_isds", [])
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

                        # AAI (Authentication and Authorization Infrastructure) attributes
                        if "gender" in user_data:
                            existing_user.gender = user_data.get("gender")
                        existing_user.personal_title = user_data.get(
                            "personal_title", ""
                        )
                        existing_user.place_of_birth = user_data.get(
                            "place_of_birth", ""
                        )
                        existing_user.country_of_residence = user_data.get(
                            "country_of_residence", ""
                        )
                        existing_user.nationality = user_data.get("nationality", "")
                        existing_user.nationalities = user_data.get("nationalities", [])
                        existing_user.organization_country = user_data.get(
                            "organization_country", ""
                        )
                        existing_user.organization_type = user_data.get(
                            "organization_type", ""
                        )
                        existing_user.organization_registry_code = user_data.get(
                            "organization_registry_code", ""
                        )
                        existing_user.organization_vat_code = user_data.get(
                            "organization_vat_code", ""
                        )
                        existing_user.organization_address = user_data.get(
                            "organization_address", ""
                        )
                        existing_user.eduperson_assurance = user_data.get(
                            "eduperson_assurance", []
                        )
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
                            with transaction.atomic():
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
                        deactivation_reason=user_data.get("deactivation_reason", ""),
                        # Additional fields
                        details=user_data.get("details", {}),
                        notifications_enabled=user_data.get(
                            "notifications_enabled", True
                        ),
                        is_identity_manager=user_data.get("is_identity_manager", False),
                        managed_isds=user_data.get("managed_isds", []),
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
                        # AAI attributes
                        gender=user_data.get("gender"),
                        personal_title=user_data.get("personal_title", ""),
                        place_of_birth=user_data.get("place_of_birth", ""),
                        country_of_residence=user_data.get("country_of_residence", ""),
                        nationality=user_data.get("nationality", ""),
                        nationalities=user_data.get("nationalities", []),
                        organization_country=user_data.get("organization_country", ""),
                        organization_type=user_data.get("organization_type", ""),
                        organization_registry_code=user_data.get(
                            "organization_registry_code", ""
                        ),
                        organization_vat_code=user_data.get(
                            "organization_vat_code", ""
                        ),
                        organization_address=user_data.get("organization_address", ""),
                        eduperson_assurance=user_data.get("eduperson_assurance", []),
                    )
                    if user_data.get("civil_number"):
                        user.civil_number = user_data.get("civil_number")

                    # Handle token_lifetime - only set if provided in data
                    # -1 means endless (None in DB), mark instance to prevent signal override
                    if "token_lifetime" in user_data:
                        token_lifetime = user_data.get("token_lifetime")
                        user.token_lifetime = (
                            None if token_lifetime == -1 else token_lifetime
                        )
                        user._token_lifetime_explicitly_set = True

                    # Set password if provided, otherwise set unusable password
                    password = user_data.get("password")
                    if password:
                        user.set_password(password)
                    else:
                        user.set_unusable_password()

                    if not self.dry_run:
                        with transaction.atomic():
                            user.save()

                    # Update maps for subsequent lookups
                    user_by_uuid[self._normalize_uuid(uuid)] = user
                    user_by_username[username] = user

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

        # Pre-fetch lookup maps to avoid N+1 queries
        user_map = {str(u.uuid): u for u in User.all_objects.all()}
        token_by_key = {t.key: t for t in Token.objects.all()}
        token_by_user_id = {t.user_id: t for t in token_by_key.values()}

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
                user = user_map.get(self._normalize_uuid(user_uuid))
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
                    existing_token = token_by_key.get(key)

                    if existing_token:
                        if self.update_existing:
                            # Update existing token
                            existing_token.user = user
                            if created:
                                existing_token.created = created
                            with transaction.atomic():
                                existing_token.save()
                            self.stats["auth_tokens"]["updated"] += 1
                        else:
                            self.stats["auth_tokens"]["skipped"] += 1
                    else:
                        # Check if user already has a token. Importing users
                        # auto-creates a token for each of them, so a token
                        # explicitly declared in the input is authoritative
                        # and replaces the auto-generated one even without
                        # the update flag.
                        user_token = token_by_user_id.get(user.id)
                        if user_token:
                            with transaction.atomic():
                                user_token.delete()
                                # Remove old token from maps
                                token_by_key.pop(user_token.key, None)
                                token_by_user_id.pop(user.id, None)
                                token = Token(key=key, user=user)
                                if created:
                                    token.created = created
                                token.save()
                            # Update maps
                            token_by_key[key] = token
                            token_by_user_id[user.id] = token
                            self.stats["auth_tokens"]["updated"] += 1
                        else:
                            # Create new token
                            token = Token(key=key, user=user)
                            if created:
                                token.created = created
                            with transaction.atomic():
                                token.save()
                            # Update maps
                            token_by_key[key] = token
                            token_by_user_id[user.id] = token
                            self.stats["auth_tokens"]["created"] += 1
                else:
                    # Dry run
                    existing = key in token_by_key
                    if existing:
                        if self.update_existing:
                            self.stats["auth_tokens"]["updated"] += 1
                        else:
                            self.stats["auth_tokens"]["skipped"] += 1
                    else:
                        # Check for user token conflict; explicitly declared
                        # tokens replace the user's existing token
                        user_has_token = user.id in token_by_user_id
                        if user_has_token:
                            self.stats["auth_tokens"]["updated"] += 1
                        else:
                            self.stats["auth_tokens"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import auth token {token_data.get('key')}: {e}"
                    )
                )
                self.stats["auth_tokens"]["errors"] += 1

    def import_ssh_public_keys(self, keys_data):
        """Import user SSH public keys."""
        self.stdout.write("Importing SSH public keys...")

        for key_data in keys_data:
            try:
                uuid = key_data.get("uuid")
                user_uuid = key_data.get("user_uuid")
                public_key = key_data.get("public_key")

                if not uuid or not user_uuid or not public_key:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping SSH key without UUID, user_uuid, or public_key"
                        )
                    )
                    self.stats["ssh_public_keys"]["errors"] += 1
                    continue

                # Find user
                user = User.all_objects.filter(uuid=user_uuid).first()
                if not user:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping SSH key {uuid}: user {user_uuid} not found"
                        )
                    )
                    self.stats["ssh_public_keys"]["errors"] += 1
                    continue

                defaults = {
                    "user": user,
                    "name": key_data.get("name", ""),
                    "public_key": public_key,
                    "is_shared": key_data.get("is_shared", False),
                }

                if not self.dry_run:
                    existing_key = SshPublicKey.objects.filter(uuid=uuid).first()

                    if existing_key:
                        if self.update_existing:
                            with transaction.atomic():
                                SshPublicKey.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["ssh_public_keys"]["updated"] += 1
                        else:
                            self.stats["ssh_public_keys"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            SshPublicKey.objects.create(uuid=uuid, **defaults)
                        self.stats["ssh_public_keys"]["created"] += 1
                else:
                    # Dry run
                    existing = SshPublicKey.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["ssh_public_keys"]["updated"] += 1
                        else:
                            self.stats["ssh_public_keys"]["skipped"] += 1
                    else:
                        self.stats["ssh_public_keys"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import SSH key {key_data.get('uuid')}: {e}"
                    )
                )
                self.stats["ssh_public_keys"]["errors"] += 1

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
                    "description": customer_data.get("description", ""),
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
                            with transaction.atomic():
                                Customer.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["customers"]["updated"] += 1
                        else:
                            self.stats["customers"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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
                            with transaction.atomic():
                                ServiceProvider.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["service_providers"]["updated"] += 1
                        else:
                            self.stats["service_providers"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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

    def import_maintenance_announcements(self, announcements_data):
        """Import maintenance announcement data."""
        self.stdout.write("Importing maintenance announcements...")

        for announcement_data in announcements_data:
            try:
                uuid = announcement_data.get("uuid")
                service_provider_uuid = announcement_data.get("service_provider_uuid")

                if not uuid or not service_provider_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping maintenance announcement without UUID or service_provider_uuid"
                        )
                    )
                    self.stats["maintenance_announcements"]["errors"] += 1
                    continue

                # Find service provider
                service_provider = ServiceProvider.objects.filter(
                    uuid=service_provider_uuid
                ).first()
                if not service_provider:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping maintenance announcement {uuid}: service provider {service_provider_uuid} not found"
                        )
                    )
                    self.stats["maintenance_announcements"]["errors"] += 1
                    continue

                # Find created_by user (optional)
                created_by = None
                created_by_uuid = announcement_data.get("created_by_uuid")
                if created_by_uuid:
                    created_by = User.all_objects.filter(uuid=created_by_uuid).first()

                # Parse datetime fields
                scheduled_start = self._parse_datetime(
                    announcement_data.get("scheduled_start")
                )
                scheduled_end = self._parse_datetime(
                    announcement_data.get("scheduled_end")
                )
                actual_start = self._parse_datetime(
                    announcement_data.get("actual_start")
                )
                actual_end = self._parse_datetime(announcement_data.get("actual_end"))

                if not scheduled_start or not scheduled_end:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping maintenance announcement {uuid}: missing scheduled_start or scheduled_end"
                        )
                    )
                    self.stats["maintenance_announcements"]["errors"] += 1
                    continue

                defaults = {
                    "name": announcement_data.get("name", ""),
                    "message": announcement_data.get("message", ""),
                    "internal_notes": announcement_data.get("internal_notes", ""),
                    "maintenance_type": announcement_data.get("maintenance_type", 1),
                    "state": announcement_data.get("state", 1),
                    "scheduled_start": scheduled_start,
                    "scheduled_end": scheduled_end,
                    "actual_start": actual_start,
                    "actual_end": actual_end,
                    "service_provider": service_provider,
                    "created_by": created_by,
                    "external_reference_url": announcement_data.get(
                        "external_reference_url", ""
                    ),
                }

                if not self.dry_run:
                    existing = MaintenanceAnnouncement.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                MaintenanceAnnouncement.objects.filter(
                                    uuid=uuid
                                ).update(**defaults)
                            self.stats["maintenance_announcements"]["updated"] += 1
                        else:
                            self.stats["maintenance_announcements"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            MaintenanceAnnouncement.objects.create(
                                uuid=uuid, **defaults
                            )
                        self.stats["maintenance_announcements"]["created"] += 1
                else:
                    existing = MaintenanceAnnouncement.objects.filter(
                        uuid=uuid
                    ).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["maintenance_announcements"]["updated"] += 1
                        else:
                            self.stats["maintenance_announcements"]["skipped"] += 1
                    else:
                        self.stats["maintenance_announcements"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import maintenance announcement {announcement_data.get('uuid')}: {e}"
                    )
                )
                self.stats["maintenance_announcements"]["errors"] += 1

    def import_maintenance_announcement_offerings(self, offerings_data):
        """Import maintenance announcement offering data."""
        self.stdout.write("Importing maintenance announcement offerings...")

        for offering_data in offerings_data:
            try:
                uuid = offering_data.get("uuid")
                maintenance_uuid = offering_data.get("maintenance_uuid")
                offering_uuid = offering_data.get("offering_uuid")

                if not uuid or not maintenance_uuid or not offering_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping maintenance announcement offering without UUID, maintenance_uuid, or offering_uuid"
                        )
                    )
                    self.stats["maintenance_announcement_offerings"]["errors"] += 1
                    continue

                # Find maintenance announcement
                maintenance = MaintenanceAnnouncement.objects.filter(
                    uuid=maintenance_uuid
                ).first()
                if not maintenance:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping maintenance announcement offering {uuid}: maintenance {maintenance_uuid} not found"
                        )
                    )
                    self.stats["maintenance_announcement_offerings"]["errors"] += 1
                    continue

                # Find offering
                offering = Offering.objects.filter(uuid=offering_uuid).first()
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping maintenance announcement offering {uuid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["maintenance_announcement_offerings"]["errors"] += 1
                    continue

                defaults = {
                    "maintenance": maintenance,
                    "offering": offering,
                    "impact_level": offering_data.get("impact_level", 2),
                    "impact_description": offering_data.get("impact_description", ""),
                }

                if not self.dry_run:
                    existing = MaintenanceAnnouncementOffering.objects.filter(
                        uuid=uuid
                    ).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                MaintenanceAnnouncementOffering.objects.filter(
                                    uuid=uuid
                                ).update(**defaults)
                            self.stats["maintenance_announcement_offerings"][
                                "updated"
                            ] += 1
                        else:
                            self.stats["maintenance_announcement_offerings"][
                                "skipped"
                            ] += 1
                    else:
                        with transaction.atomic():
                            MaintenanceAnnouncementOffering.objects.create(
                                uuid=uuid, **defaults
                            )
                        self.stats["maintenance_announcement_offerings"]["created"] += 1
                else:
                    existing = MaintenanceAnnouncementOffering.objects.filter(
                        uuid=uuid
                    ).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["maintenance_announcement_offerings"][
                                "updated"
                            ] += 1
                        else:
                            self.stats["maintenance_announcement_offerings"][
                                "skipped"
                            ] += 1
                    else:
                        self.stats["maintenance_announcement_offerings"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import maintenance announcement offering {offering_data.get('uuid')}: {e}"
                    )
                )
                self.stats["maintenance_announcement_offerings"]["errors"] += 1

    def import_software_catalogs(self, catalogs_data):
        """Import software catalog definitions (not package content)."""
        self.stdout.write("Importing software catalogs...")

        for catalog_data in catalogs_data:
            try:
                uuid = catalog_data.get("uuid")
                name = catalog_data.get("name")
                catalog_type = catalog_data.get("catalog_type")

                if not uuid or not name:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping software catalog without UUID or name"
                        )
                    )
                    self.stats["software_catalogs"]["errors"] += 1
                    continue

                defaults = {
                    "name": name,
                    "version": catalog_data.get("version", ""),
                    "catalog_type": catalog_type or "binary_runtime",
                    "source_url": catalog_data.get("source_url", ""),
                    "description": catalog_data.get("description", ""),
                    "metadata": catalog_data.get("metadata", {}),
                    "auto_update_enabled": catalog_data.get(
                        "auto_update_enabled", True
                    ),
                    "update_errors": catalog_data.get("update_errors", ""),
                }

                if not self.dry_run:
                    existing = SoftwareCatalog.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                SoftwareCatalog.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["software_catalogs"]["updated"] += 1
                        else:
                            self.stats["software_catalogs"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            SoftwareCatalog.objects.create(uuid=uuid, **defaults)
                        self.stats["software_catalogs"]["created"] += 1
                else:
                    existing = SoftwareCatalog.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["software_catalogs"]["updated"] += 1
                        else:
                            self.stats["software_catalogs"]["skipped"] += 1
                    else:
                        self.stats["software_catalogs"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import software catalog {catalog_data.get('uuid')}: {e}"
                    )
                )
                self.stats["software_catalogs"]["errors"] += 1

    def import_offering_partitions(self, partitions_data):
        """Import offering partition data (SLURM partitions)."""
        self.stdout.write("Importing offering partitions...")

        for partition_data in partitions_data:
            try:
                uuid = partition_data.get("uuid")
                offering_uuid = partition_data.get("offering_uuid")
                partition_name = partition_data.get("partition_name")

                if not uuid or not offering_uuid or not partition_name:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping offering partition without UUID, offering_uuid, or partition_name"
                        )
                    )
                    self.stats["offering_partitions"]["errors"] += 1
                    continue

                # Find offering
                offering = Offering.objects.filter(uuid=offering_uuid).first()
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping partition {uuid}: Offering {offering_uuid} not found"
                        )
                    )
                    self.stats["offering_partitions"]["errors"] += 1
                    continue

                defaults = {
                    "offering": offering,
                    "partition_name": partition_name,
                    "cpu_arch": partition_data.get("cpu_arch", ""),
                    "gpu_arch": partition_data.get("gpu_arch", ""),
                    "cpu_bind": partition_data.get("cpu_bind"),
                    "def_cpu_per_gpu": partition_data.get("def_cpu_per_gpu"),
                    "max_cpus_per_node": partition_data.get("max_cpus_per_node"),
                }

                if not self.dry_run:
                    existing = OfferingPartition.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                OfferingPartition.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["offering_partitions"]["updated"] += 1
                        else:
                            self.stats["offering_partitions"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            OfferingPartition.objects.create(uuid=uuid, **defaults)
                        self.stats["offering_partitions"]["created"] += 1
                else:
                    existing = OfferingPartition.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["offering_partitions"]["updated"] += 1
                        else:
                            self.stats["offering_partitions"]["skipped"] += 1
                    else:
                        self.stats["offering_partitions"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import offering partition {partition_data.get('uuid')}: {e}"
                    )
                )
                self.stats["offering_partitions"]["errors"] += 1

    def import_offering_software_catalogs(self, links_data):
        """Import offering-to-software-catalog links."""
        self.stdout.write("Importing offering software catalog links...")

        for link_data in links_data:
            try:
                uuid = link_data.get("uuid")
                offering_uuid = link_data.get("offering_uuid")
                catalog_uuid = link_data.get("catalog_uuid")

                if not uuid or not offering_uuid or not catalog_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping offering software catalog link without UUID, offering_uuid, or catalog_uuid"
                        )
                    )
                    self.stats["offering_software_catalogs"]["errors"] += 1
                    continue

                # Find offering
                offering = Offering.objects.filter(uuid=offering_uuid).first()
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping link {uuid}: Offering {offering_uuid} not found"
                        )
                    )
                    self.stats["offering_software_catalogs"]["errors"] += 1
                    continue

                # Find catalog
                catalog = SoftwareCatalog.objects.filter(uuid=catalog_uuid).first()
                if not catalog:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping link {uuid}: SoftwareCatalog {catalog_uuid} not found"
                        )
                    )
                    self.stats["offering_software_catalogs"]["errors"] += 1
                    continue

                # Find partition if specified
                partition = None
                partition_uuid = link_data.get("partition_uuid")
                if partition_uuid:
                    partition = OfferingPartition.objects.filter(
                        uuid=partition_uuid
                    ).first()

                defaults = {
                    "offering": offering,
                    "catalog": catalog,
                    "enabled_cpu_family": link_data.get("enabled_cpu_family", []),
                    "enabled_cpu_microarchitectures": link_data.get(
                        "enabled_cpu_microarchitectures", []
                    ),
                    "partition": partition,
                }

                if not self.dry_run:
                    existing = OfferingSoftwareCatalog.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                OfferingSoftwareCatalog.objects.filter(
                                    uuid=uuid
                                ).update(**defaults)
                            self.stats["offering_software_catalogs"]["updated"] += 1
                        else:
                            self.stats["offering_software_catalogs"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            OfferingSoftwareCatalog.objects.create(
                                uuid=uuid, **defaults
                            )
                        self.stats["offering_software_catalogs"]["created"] += 1
                else:
                    existing = OfferingSoftwareCatalog.objects.filter(
                        uuid=uuid
                    ).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["offering_software_catalogs"]["updated"] += 1
                        else:
                            self.stats["offering_software_catalogs"]["skipped"] += 1
                    else:
                        self.stats["offering_software_catalogs"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import offering software catalog link {link_data.get('uuid')}: {e}"
                    )
                )
                self.stats["offering_software_catalogs"]["errors"] += 1

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
                            with transaction.atomic():
                                Project.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["projects"]["updated"] += 1
                        else:
                            self.stats["projects"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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

    def import_category_groups(self, category_groups_data):
        """Import marketplace category group data."""
        self.stdout.write("Importing category groups...")

        for group_data in category_groups_data:
            try:
                uuid = group_data.get("uuid")
                title = group_data.get("title")

                if not uuid or not title:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping category group without UUID or title"
                        )
                    )
                    self.stats["category_groups"]["errors"] += 1
                    continue

                defaults = {
                    "title": title,
                    "description": group_data.get("description", ""),
                }

                if not self.dry_run:
                    existing_group = CategoryGroup.objects.filter(uuid=uuid).first()

                    if existing_group:
                        if self.update_existing:
                            with transaction.atomic():
                                CategoryGroup.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["category_groups"]["updated"] += 1
                        else:
                            self.stats["category_groups"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            CategoryGroup.objects.create(uuid=uuid, **defaults)
                        self.stats["category_groups"]["created"] += 1
                else:
                    existing = CategoryGroup.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["category_groups"]["updated"] += 1
                        else:
                            self.stats["category_groups"]["skipped"] += 1
                    else:
                        self.stats["category_groups"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import category group {group_data.get('uuid')}: {e}"
                    )
                )
                self.stats["category_groups"]["errors"] += 1

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

                # Resolve category group reference
                group = None
                group_uuid = category_data.get("group_uuid")
                if group_uuid:
                    group = CategoryGroup.objects.filter(uuid=group_uuid).first()
                    if not group:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Category group {group_uuid} not found for category {uuid}"
                            )
                        )

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
                    "group": group,
                }

                if not self.dry_run:
                    existing_category = Category.objects.filter(uuid=uuid).first()

                    if existing_category:
                        if self.update_existing:
                            with transaction.atomic():
                                Category.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["categories"]["updated"] += 1
                        else:
                            self.stats["categories"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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
                    "helpdesk_url": offering_data.get("helpdesk_url", ""),
                    "documentation_url": offering_data.get("documentation_url", ""),
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

                # Resolve scope (for offerings linked to backend objects)
                scope_type = offering_data.get("scope_type")
                scope_uuid = offering_data.get("scope_uuid")
                if scope_type and scope_uuid:
                    try:
                        app_label, model_name = scope_type.split(".")
                        ct = ContentType.objects.get(
                            app_label=app_label, model=model_name
                        )
                        scope_obj = (
                            ct.model_class().objects.filter(uuid=scope_uuid).first()
                        )
                        if scope_obj:
                            defaults["content_type"] = ct
                            defaults["object_id"] = scope_obj.id
                    except Exception as e:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Could not resolve scope for offering {uuid}: {e}"
                            )
                        )

                if category:
                    defaults["category"] = category

                if not self.dry_run:
                    existing_offering = Offering.objects.filter(uuid=uuid).first()

                    if existing_offering:
                        if self.update_existing:
                            with transaction.atomic():
                                Offering.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["offerings"]["updated"] += 1
                        else:
                            self.stats["offerings"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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

    def import_offering_endpoints(self, endpoints_data):
        """Import offering access endpoints."""
        self.stdout.write("Importing offering endpoints...")

        for endpoint_data in endpoints_data:
            try:
                uuid = endpoint_data.get("uuid")
                offering_uuid = endpoint_data.get("offering_uuid")
                name = endpoint_data.get("name")
                url = endpoint_data.get("url")

                if not uuid or not offering_uuid or not name or not url:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping endpoint without UUID, offering_uuid, name, or url"
                        )
                    )
                    self.stats["offering_endpoints"]["errors"] += 1
                    continue

                # Find offering
                offering = Offering.objects.filter(uuid=offering_uuid).first()
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping endpoint {uuid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["offering_endpoints"]["errors"] += 1
                    continue

                defaults = {
                    "offering": offering,
                    "name": name,
                    "url": url,
                }

                if not self.dry_run:
                    existing_endpoint = OfferingAccessEndpoint.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_endpoint:
                        if self.update_existing:
                            with transaction.atomic():
                                OfferingAccessEndpoint.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["offering_endpoints"]["updated"] += 1
                        else:
                            self.stats["offering_endpoints"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            OfferingAccessEndpoint.objects.create(uuid=uuid, **defaults)
                        self.stats["offering_endpoints"]["created"] += 1
                else:
                    # Dry run
                    existing = OfferingAccessEndpoint.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["offering_endpoints"]["updated"] += 1
                        else:
                            self.stats["offering_endpoints"]["skipped"] += 1
                    else:
                        self.stats["offering_endpoints"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import endpoint {endpoint_data.get('uuid')}: {e}"
                    )
                )
                self.stats["offering_endpoints"]["errors"] += 1

    def import_project_estimated_cost_policies(self, policies_data):
        """Import project estimated cost policies."""
        self.stdout.write("Importing project estimated cost policies...")

        for policy_data in policies_data:
            try:
                uuid = policy_data.get("uuid")
                project_uuid = policy_data.get("project_uuid")

                if not uuid or not project_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping project cost policy without UUID or project_uuid"
                        )
                    )
                    self.stats["project_estimated_cost_policies"]["errors"] += 1
                    continue

                project = Project.objects.filter(uuid=project_uuid).first()
                if not project:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping project cost policy {uuid}: project {project_uuid} not found"
                        )
                    )
                    self.stats["project_estimated_cost_policies"]["errors"] += 1
                    continue

                defaults = {
                    "scope": project,
                    "limit_cost": policy_data.get("limit_cost", 0),
                    "period": policy_data.get("period", 2),
                    "actions": policy_data.get("actions", "notify_project_team"),
                    "options": policy_data.get("options", {}),
                    "has_fired": policy_data.get("has_fired", False),
                }

                if not self.dry_run:
                    existing = ProjectEstimatedCostPolicy.objects.filter(
                        uuid=uuid
                    ).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ProjectEstimatedCostPolicy.objects.filter(
                                    uuid=uuid
                                ).update(**defaults)
                            self.stats["project_estimated_cost_policies"][
                                "updated"
                            ] += 1
                        else:
                            self.stats["project_estimated_cost_policies"][
                                "skipped"
                            ] += 1
                    else:
                        with transaction.atomic():
                            ProjectEstimatedCostPolicy.objects.create(
                                uuid=uuid, **defaults
                            )
                        self.stats["project_estimated_cost_policies"]["created"] += 1
                else:
                    existing = ProjectEstimatedCostPolicy.objects.filter(
                        uuid=uuid
                    ).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["project_estimated_cost_policies"][
                                "updated"
                            ] += 1
                        else:
                            self.stats["project_estimated_cost_policies"][
                                "skipped"
                            ] += 1
                    else:
                        self.stats["project_estimated_cost_policies"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import project cost policy {policy_data.get('uuid')}: {e}"
                    )
                )
                self.stats["project_estimated_cost_policies"]["errors"] += 1

    def import_customer_estimated_cost_policies(self, policies_data):
        """Import customer estimated cost policies."""
        self.stdout.write("Importing customer estimated cost policies...")

        for policy_data in policies_data:
            try:
                uuid = policy_data.get("uuid")
                customer_uuid = policy_data.get("customer_uuid")

                if not uuid or not customer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping customer cost policy without UUID or customer_uuid"
                        )
                    )
                    self.stats["customer_estimated_cost_policies"]["errors"] += 1
                    continue

                customer = Customer.objects.filter(uuid=customer_uuid).first()
                if not customer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping customer cost policy {uuid}: customer {customer_uuid} not found"
                        )
                    )
                    self.stats["customer_estimated_cost_policies"]["errors"] += 1
                    continue

                defaults = {
                    "scope": customer,
                    "limit_cost": policy_data.get("limit_cost", 0),
                    "period": policy_data.get("period", 2),
                    "actions": policy_data.get("actions", "notify_organization_owners"),
                    "options": policy_data.get("options", {}),
                    "has_fired": policy_data.get("has_fired", False),
                }

                if not self.dry_run:
                    existing = CustomerEstimatedCostPolicy.objects.filter(
                        uuid=uuid
                    ).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                CustomerEstimatedCostPolicy.objects.filter(
                                    uuid=uuid
                                ).update(**defaults)
                            self.stats["customer_estimated_cost_policies"][
                                "updated"
                            ] += 1
                        else:
                            self.stats["customer_estimated_cost_policies"][
                                "skipped"
                            ] += 1
                    else:
                        with transaction.atomic():
                            CustomerEstimatedCostPolicy.objects.create(
                                uuid=uuid, **defaults
                            )
                        self.stats["customer_estimated_cost_policies"]["created"] += 1
                else:
                    existing = CustomerEstimatedCostPolicy.objects.filter(
                        uuid=uuid
                    ).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["customer_estimated_cost_policies"][
                                "updated"
                            ] += 1
                        else:
                            self.stats["customer_estimated_cost_policies"][
                                "skipped"
                            ] += 1
                    else:
                        self.stats["customer_estimated_cost_policies"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import customer cost policy {policy_data.get('uuid')}: {e}"
                    )
                )
                self.stats["customer_estimated_cost_policies"]["errors"] += 1

    def import_slurm_periodic_policies(self, policies_data):
        """Import SLURM periodic usage policies."""
        self.stdout.write("Importing SLURM periodic usage policies...")

        for policy_data in policies_data:
            try:
                uuid = policy_data.get("uuid")
                offering_uuid = policy_data.get("offering_uuid")

                if not uuid or not offering_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping SLURM policy without UUID or offering_uuid"
                        )
                    )
                    self.stats["slurm_periodic_policies"]["errors"] += 1
                    continue

                # Find offering
                offering = Offering.objects.filter(uuid=offering_uuid).first()
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping SLURM policy {uuid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["slurm_periodic_policies"]["errors"] += 1
                    continue

                defaults = {
                    "scope": offering,
                    "apply_to_all": policy_data.get("apply_to_all", True),
                    "actions": policy_data.get("actions", "notify_organization_owners"),
                    "limit_type": policy_data.get("limit_type", "GrpTRESMins"),
                    "tres_billing_enabled": policy_data.get(
                        "tres_billing_enabled", True
                    ),
                    "tres_billing_weights": policy_data.get("tres_billing_weights", {}),
                    "carryover_factor": policy_data.get("carryover_factor", 50),
                    "grace_ratio": policy_data.get("grace_ratio", 0.2),
                    "carryover_enabled": policy_data.get("carryover_enabled", True),
                    "raw_usage_reset": policy_data.get("raw_usage_reset", True),
                    "qos_strategy": policy_data.get("qos_strategy", "threshold"),
                }

                if not self.dry_run:
                    existing_policy = SlurmPeriodicUsagePolicy.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_policy:
                        if self.update_existing:
                            with transaction.atomic():
                                SlurmPeriodicUsagePolicy.objects.filter(
                                    uuid=uuid
                                ).update(**defaults)
                            self.stats["slurm_periodic_policies"]["updated"] += 1
                        else:
                            self.stats["slurm_periodic_policies"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            policy = SlurmPeriodicUsagePolicy.objects.create(
                                uuid=uuid, **defaults
                            )

                            # Handle component limits if provided
                            component_limits = policy_data.get("component_limits", [])
                            for limit_data in component_limits:
                                component_type = limit_data.get("type")
                                limit_value = limit_data.get("limit")
                                component = offering.components.filter(
                                    type=component_type
                                ).first()
                                if component and limit_value is not None:
                                    OfferingComponentLimit.objects.update_or_create(
                                        policy=policy,
                                        component=component,
                                        defaults={"limit": limit_value},
                                    )

                        self.stats["slurm_periodic_policies"]["created"] += 1
                else:
                    # Dry run
                    existing = SlurmPeriodicUsagePolicy.objects.filter(
                        uuid=uuid
                    ).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["slurm_periodic_policies"]["updated"] += 1
                        else:
                            self.stats["slurm_periodic_policies"]["skipped"] += 1
                    else:
                        self.stats["slurm_periodic_policies"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import SLURM policy {policy_data.get('uuid')}: {e}"
                    )
                )
                self.stats["slurm_periodic_policies"]["errors"] += 1

    def import_slurm_command_history(self, history_data):
        """Import SLURM command history records."""
        self.stdout.write("Importing SLURM command history...")

        for record_data in history_data:
            try:
                uuid = record_data.get("uuid")
                resource_uuid = record_data.get("resource_uuid")
                policy_uuid = record_data.get("policy_uuid")

                if not uuid or not resource_uuid or not policy_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping command history without UUID, resource_uuid, or policy_uuid"
                        )
                    )
                    self.stats["slurm_command_history"]["errors"] += 1
                    continue

                # Find resource
                resource = Resource.objects.filter(uuid=resource_uuid).first()
                if not resource:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping command history {uuid}: resource {resource_uuid} not found"
                        )
                    )
                    self.stats["slurm_command_history"]["errors"] += 1
                    continue

                # Find policy
                policy = SlurmPeriodicUsagePolicy.objects.filter(
                    uuid=policy_uuid
                ).first()
                if not policy:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping command history {uuid}: policy {policy_uuid} not found"
                        )
                    )
                    self.stats["slurm_command_history"]["errors"] += 1
                    continue

                # Parse billing_period
                billing_period_str = record_data.get("billing_period")
                if billing_period_str:
                    billing_period = datetime.strptime(
                        billing_period_str, "%Y-%m-%d"
                    ).date()
                else:
                    billing_period = timezone.now().date().replace(day=1)

                # Parse executed_at
                executed_at_str = record_data.get("executed_at")
                if executed_at_str:
                    executed_at = datetime.fromisoformat(
                        executed_at_str.replace("Z", "+00:00")
                    )
                else:
                    executed_at = timezone.now()

                defaults = {
                    "policy": policy,
                    "resource": resource,
                    "billing_period": billing_period,
                    "command_type": record_data.get("command_type", "fairshare"),
                    "description": record_data.get("description", ""),
                    "shell_command": record_data.get("shell_command", ""),
                    "parameters": record_data.get("parameters", {}),
                    "execution_mode": record_data.get("execution_mode", "emulator"),
                    "success": record_data.get("success", True),
                    "error_message": record_data.get("error_message", ""),
                }

                if not self.dry_run:
                    existing = SlurmCommandHistory.objects.filter(uuid=uuid).first()

                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                SlurmCommandHistory.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["slurm_command_history"]["updated"] += 1
                        else:
                            self.stats["slurm_command_history"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            # Create with specific executed_at (can't use auto_now_add)
                            history = SlurmCommandHistory(uuid=uuid, **defaults)
                            history.save()
                            # Update executed_at since auto_now_add overrides it
                            SlurmCommandHistory.objects.filter(uuid=uuid).update(
                                executed_at=executed_at
                            )
                        self.stats["slurm_command_history"]["created"] += 1
                else:
                    # Dry run
                    existing = SlurmCommandHistory.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["slurm_command_history"]["updated"] += 1
                        else:
                            self.stats["slurm_command_history"]["skipped"] += 1
                    else:
                        self.stats["slurm_command_history"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import command history {record_data.get('uuid')}: {e}"
                    )
                )
                self.stats["slurm_command_history"]["errors"] += 1

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
                            with transaction.atomic():
                                Role.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["roles"]["updated"] += 1
                        else:
                            self.stats["roles"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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
                    with transaction.atomic():
                        RolePermission.objects.filter(
                            role=role,
                            permission__in=current_permissions - new_permissions,
                        ).delete()

                    # Add new permissions
                    for permission in new_permissions - current_permissions:
                        with transaction.atomic():
                            RolePermission.objects.create(
                                role=role, permission=permission
                            )
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

    def import_posix_id_pools(self, pools_data):
        """Import POSIX ID pools (offering- or service-provider-scoped), by uuid."""
        if not pools_data:
            return
        self.stdout.write("Importing POSIX ID pools...")
        offering_map = {str(o.uuid): o for o in Offering.objects.all()}
        sp_map = {str(sp.uuid): sp for sp in ServiceProvider.objects.all()}
        for item in pools_data:
            try:
                uuid = item.get("uuid")
                if not uuid:
                    self.stats["posix_id_pools"]["errors"] += 1
                    continue
                offering = (
                    offering_map.get(self._normalize_uuid(item["offering_uuid"]))
                    if item.get("offering_uuid")
                    else None
                )
                service_provider = (
                    sp_map.get(self._normalize_uuid(item["service_provider_uuid"]))
                    if item.get("service_provider_uuid")
                    else None
                )
                if not offering and not service_provider:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping POSIX ID pool {uuid}: scope not found"
                        )
                    )
                    self.stats["posix_id_pools"]["errors"] += 1
                    continue
                _, created = PosixIdPool.objects.update_or_create(
                    uuid=self._normalize_uuid(uuid),
                    defaults={
                        "offering": offering,
                        "service_provider": service_provider,
                        "min_uid": item.get("min_uid"),
                        "max_uid": item.get("max_uid"),
                        "next_uid": item.get("next_uid"),
                        "min_gid": item.get("min_gid"),
                        "max_gid": item.get("max_gid"),
                        "next_gid": item.get("next_gid"),
                        "description": item.get("description", ""),
                    },
                )
                self.stats["posix_id_pools"]["created" if created else "updated"] += 1
            except Exception as e:
                self.stats["posix_id_pools"]["errors"] += 1
                self.stdout.write(
                    self.style.ERROR(f"Error importing POSIX ID pool: {e}")
                )

    def import_resource_projects(self, resource_projects_data):
        """Import resource projects (sub-projects of a resource), matched by uuid."""
        if not resource_projects_data:
            return
        self.stdout.write("Importing resource projects...")
        resource_map = {str(r.uuid): r for r in Resource.objects.all()}
        for item in resource_projects_data:
            try:
                uuid = item.get("uuid")
                resource_uuid = item.get("resource_uuid")
                if not uuid or not resource_uuid:
                    self.stats["resource_projects"]["errors"] += 1
                    continue
                resource = resource_map.get(self._normalize_uuid(resource_uuid))
                if not resource:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping resource project {uuid}: "
                            f"resource {resource_uuid} not found"
                        )
                    )
                    self.stats["resource_projects"]["errors"] += 1
                    continue
                _, created = ResourceProject.objects.update_or_create(
                    uuid=self._normalize_uuid(uuid),
                    defaults={"resource": resource, "name": item.get("name", "")},
                )
                self.stats["resource_projects"][
                    "created" if created else "updated"
                ] += 1
            except Exception as e:
                self.stats["resource_projects"]["errors"] += 1
                self.stdout.write(
                    self.style.ERROR(f"Error importing resource project: {e}")
                )

    def import_user_roles(self, user_roles_data):
        """Import user role assignments."""
        self.stdout.write("Importing user roles...")

        # Pre-fetch lookup maps to avoid N+1 queries
        # Note: str(obj.uuid) may return non-hyphenated hex (StringUUID),
        # but data UUIDs may have hyphens, so we normalize all keys by removing hyphens.
        user_map = {str(u.uuid): u for u in User.all_objects.all()}
        role_by_uuid = {str(r.uuid): r for r in Role.objects.all()}
        role_by_name = {r.name: r for r in role_by_uuid.values()}
        existing_user_roles = {str(ur.uuid): ur for ur in UserRole.objects.all()}

        # Pre-fetch ContentType map
        content_type_map = {
            (ct.app_label, ct.model): ct for ct in ContentType.objects.all()
        }

        # Pre-fetch common scope objects (Customer and Project cover >95% of scopes)
        customer_by_uuid = {str(c.uuid): c for c in Customer.objects.all()}
        project_by_uuid = {str(p.uuid): p for p in Project.objects.all()}
        scope_cache = {
            ("structure", "customer"): customer_by_uuid,
            ("structure", "project"): project_by_uuid,
        }

        for user_role_data in user_roles_data:
            try:
                uuid = user_role_data.get("uuid")
                user_uuid = user_role_data.get("user_uuid")
                role_uuid = user_role_data.get("role_uuid")
                role_name = user_role_data.get("role_name")
                scope_type = user_role_data.get("scope_type")
                scope_uuid = user_role_data.get("scope_uuid")

                if not uuid or not user_uuid or (not role_uuid and not role_name):
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping user role without UUID, user_uuid, or role_uuid/role_name"
                        )
                    )
                    self.stats["user_roles"]["errors"] += 1
                    continue

                # Find user
                user = user_map.get(self._normalize_uuid(user_uuid))
                if not user:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping user role {uuid}: user {user_uuid} not found"
                        )
                    )
                    self.stats["user_roles"]["errors"] += 1
                    continue

                # Resolve scope first (needed for system role creation)
                content_type = None
                object_id = None

                if scope_type and scope_uuid:
                    try:
                        app_label, model = scope_type.split(".")
                        content_type = content_type_map.get((app_label, model))
                        if not content_type:
                            raise ContentType.DoesNotExist
                    except (ValueError, ContentType.DoesNotExist):
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping user role {uuid}: invalid scope_type {scope_type}"
                            )
                        )
                        self.stats["user_roles"]["errors"] += 1
                        continue

                # Find role by UUID or by name (create system role if needed)
                role = None
                if role_uuid:
                    role = role_by_uuid.get(self._normalize_uuid(role_uuid))
                if not role and role_name:
                    role = role_by_name.get(role_name)
                    # If role not found and we have a content_type, create it as a system role
                    if not role and content_type:
                        role = Role.objects.get_system_role(role_name, content_type)
                        # Cache the newly created/fetched role
                        role_by_uuid[str(role.uuid)] = role
                        role_by_name[role.name] = role
                if not role:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping user role {uuid}: role {role_uuid or role_name} not found"
                        )
                    )
                    self.stats["user_roles"]["errors"] += 1
                    continue

                # Resolve scope object
                if scope_type and scope_uuid and content_type:
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
                    # Use pre-fetched scope maps for common types, fall back to DB for rare ones
                    app_label, model = scope_type.split(".")
                    scope_map = scope_cache.get((app_label, model))
                    if scope_map is not None:
                        scope_object = scope_map.get(self._normalize_uuid(scope_uuid))
                    else:
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
                    normalized_uuid = self._normalize_uuid(uuid)
                    existing = existing_user_roles.get(normalized_uuid)

                    if existing:
                        if self.update_existing:
                            for key, value in defaults.items():
                                setattr(existing, key, value)
                            with transaction.atomic():
                                existing.save()
                            self.stats["user_roles"]["updated"] += 1
                        else:
                            self.stats["user_roles"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            obj = UserRole.objects.create(uuid=uuid, **defaults)
                        existing_user_roles[normalized_uuid] = obj
                        self.stats["user_roles"]["created"] += 1
                else:
                    existing = self._normalize_uuid(uuid) in existing_user_roles
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
                            with transaction.atomic():
                                ProjectServiceAccount.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["project_service_accounts"]["updated"] += 1
                        else:
                            self.stats["project_service_accounts"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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
                            with transaction.atomic():
                                CustomerServiceAccount.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["customer_service_accounts"]["updated"] += 1
                        else:
                            self.stats["customer_service_accounts"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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
                            with transaction.atomic():
                                CourseAccount.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["course_accounts"]["updated"] += 1
                        else:
                            self.stats["course_accounts"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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
                            with transaction.atomic():
                                Plan.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["plans"]["updated"] += 1
                        else:
                            self.stats["plans"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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
                    "limit_period": component_data.get("limit_period")
                    or LimitPeriods.MONTH,
                    "limit_amount": component_data.get("limit_amount"),
                    "min_value": component_data.get("min_value"),
                    "max_value": component_data.get("max_value"),
                    "min_prepaid_duration": component_data.get("min_prepaid_duration"),
                    "max_prepaid_duration": component_data.get("max_prepaid_duration"),
                    "prepaid_duration_step": component_data.get(
                        "prepaid_duration_step"
                    ),
                    "min_renewal_duration": component_data.get("min_renewal_duration"),
                    "max_renewal_duration": component_data.get("max_renewal_duration"),
                    "renewal_duration_step": component_data.get(
                        "renewal_duration_step"
                    ),
                    "is_prepaid": component_data.get("is_prepaid", False),
                    "article_code": component_data.get("article_code", ""),
                    "backend_id": component_data.get("backend_id", ""),
                }

                if not self.dry_run:
                    existing_component = OfferingComponent.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_component:
                        if self.update_existing:
                            with transaction.atomic():
                                OfferingComponent.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["offering_components"]["updated"] += 1
                        else:
                            self.stats["offering_components"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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
                    "discount_formula": pc_data.get("discount_formula", ""),
                    "discount_aggregation": pc_data.get(
                        "discount_aggregation", "customer"
                    ),
                }

                if not self.dry_run:
                    existing_pc = PlanComponent.objects.filter(
                        plan=plan, component=component
                    ).first()

                    if existing_pc:
                        if self.update_existing:
                            with transaction.atomic():
                                PlanComponent.objects.filter(
                                    plan=plan, component=component
                                ).update(**defaults)
                            self.stats["plan_components"]["updated"] += 1
                        else:
                            self.stats["plan_components"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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

                # Parse end_date (optional)
                end_date = None
                if resource_data.get("end_date"):
                    try:
                        end_date = datetime.fromisoformat(
                            resource_data["end_date"]
                        ).date()
                    except (ValueError, TypeError):
                        pass

                # Parse created date (optional)
                created = None
                if resource_data.get("created"):
                    try:
                        created = datetime.fromisoformat(resource_data["created"])
                        if timezone.is_naive(created):
                            created = timezone.make_aware(created)
                    except (ValueError, TypeError):
                        pass

                # Resolve scope (generic FK to backend object)
                scope_content_type = None
                scope_object_id = None
                scope_type = resource_data.get("scope_type")
                scope_uuid = resource_data.get("scope_uuid")
                if scope_type and scope_uuid:
                    try:
                        app_label, model_name = scope_type.split(".")
                        ct = ContentType.objects.get(
                            app_label=app_label, model=model_name
                        )
                        scope_obj = (
                            ct.model_class().objects.filter(uuid=scope_uuid).first()
                        )
                        if scope_obj:
                            scope_content_type = ct
                            scope_object_id = scope_obj.id
                        else:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Resource {uuid}: scope {scope_type}:{scope_uuid} not found"
                                )
                            )
                    except (ValueError, ContentType.DoesNotExist):
                        self.stdout.write(
                            self.style.WARNING(
                                f"Resource {uuid}: invalid scope_type '{scope_type}'"
                            )
                        )

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
                    "end_date": end_date,
                    "paused": resource_data.get("paused", False),
                    "downscaled": resource_data.get("downscaled", False),
                    "restrict_member_access": resource_data.get(
                        "restrict_member_access", False
                    ),
                }

                if scope_content_type and scope_object_id:
                    defaults["content_type"] = scope_content_type
                    defaults["object_id"] = scope_object_id

                if not self.dry_run:
                    existing_resource = Resource.objects.filter(uuid=uuid).first()

                    if existing_resource:
                        if self.update_existing:
                            with transaction.atomic():
                                Resource.objects.filter(uuid=uuid).update(**defaults)
                                # Update created date if provided (requires separate update)
                                if created:
                                    Resource.objects.filter(uuid=uuid).update(
                                        created=created
                                    )
                            self.stats["resources"]["updated"] += 1
                        else:
                            self.stats["resources"]["skipped"] += 1
                    else:
                        # Check if a resource with same scope was auto-created by signals
                        if scope_content_type and scope_object_id:
                            existing_by_scope = Resource.objects.filter(
                                content_type=scope_content_type,
                                object_id=scope_object_id,
                            ).first()
                            if existing_by_scope:
                                with transaction.atomic():
                                    Resource.objects.filter(
                                        pk=existing_by_scope.pk
                                    ).update(uuid=uuid, **defaults)
                                    if created:
                                        Resource.objects.filter(uuid=uuid).update(
                                            created=created
                                        )
                                self.stats["resources"]["updated"] += 1
                                continue

                        with transaction.atomic():
                            Resource.objects.create(uuid=uuid, **defaults)
                            # Update created date if provided (auto_now_add prevents setting during create)
                            if created:
                                Resource.objects.filter(uuid=uuid).update(
                                    created=created
                                )
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
                            with transaction.atomic():
                                ResourcePlanPeriod.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["resource_plan_periods"]["updated"] += 1
                        else:
                            self.stats["resource_plan_periods"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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

        # Pre-fetch lookup maps to avoid N+1 queries
        resource_map = {str(r.uuid): r for r in Resource.objects.all()}
        component_map = {str(c.uuid): c for c in OfferingComponent.objects.all()}
        plan_period_map = {str(pp.uuid): pp for pp in ResourcePlanPeriod.objects.all()}
        existing_usages = {str(cu.uuid): cu for cu in ComponentUsage.objects.all()}
        # Build duplicate check set: (resource_id, component_id, billing_period) for null plan_period
        duplicate_keys = set()
        for cu in existing_usages.values():
            if cu.plan_period_id is None:
                duplicate_keys.add((cu.resource_id, cu.component_id, cu.billing_period))

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
                resource = resource_map.get(self._normalize_uuid(resource_uuid))
                if not resource:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping component usage {uuid}: resource {resource_uuid} not found"
                        )
                    )
                    self.stats["component_usages"]["errors"] += 1
                    continue

                # Find component
                component = component_map.get(self._normalize_uuid(component_uuid))
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
                    plan_period = plan_period_map.get(
                        self._normalize_uuid(plan_period_uuid)
                    )
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
                    normalized_uuid = self._normalize_uuid(uuid)
                    existing_usage = existing_usages.get(normalized_uuid)

                    if existing_usage:
                        if self.update_existing:
                            with transaction.atomic():
                                ComponentUsage.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["component_usages"]["updated"] += 1
                        else:
                            self.stats["component_usages"]["skipped"] += 1
                    else:
                        # Check if a record with the same business key exists
                        # (unique constraint on resource, component, billing_period when plan_period is NULL)
                        biz_key = (
                            resource.id,
                            component.id,
                            billing_period or timezone.now().date(),
                        )
                        duplicate_exists = biz_key in duplicate_keys

                        if duplicate_exists:
                            if self.update_existing:
                                # Fall back to DB query to get the actual duplicate for update
                                duplicate_usage = ComponentUsage.objects.filter(
                                    resource=resource,
                                    component=component,
                                    billing_period=billing_period
                                    or timezone.now().date(),
                                    plan_period__isnull=True,
                                ).first()
                                if duplicate_usage:
                                    with transaction.atomic():
                                        ComponentUsage.objects.filter(
                                            pk=duplicate_usage.pk
                                        ).update(uuid=uuid, **defaults)
                                self.stats["component_usages"]["updated"] += 1
                            else:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"Skipping component usage {uuid}: duplicate exists"
                                    )
                                )
                                self.stats["component_usages"]["skipped"] += 1
                        else:
                            with transaction.atomic():
                                obj = ComponentUsage.objects.create(
                                    uuid=uuid, **defaults
                                )
                            existing_usages[normalized_uuid] = obj
                            if plan_period is None:
                                duplicate_keys.add(biz_key)
                            self.stats["component_usages"]["created"] += 1
                else:
                    existing = self._normalize_uuid(uuid) in existing_usages
                    if existing:
                        if self.update_existing:
                            self.stats["component_usages"]["updated"] += 1
                        else:
                            self.stats["component_usages"]["skipped"] += 1
                    else:
                        # Check for duplicate by business key
                        biz_key = (
                            resource.id,
                            component.id,
                            billing_period or timezone.now().date(),
                        )
                        duplicate_exists = biz_key in duplicate_keys

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

    def import_component_user_usages(self, user_usages_data):
        """Import component user usage data."""
        self.stdout.write("Importing component user usages...")

        for user_usage_data in user_usages_data:
            try:
                uuid = user_usage_data.get("uuid")
                component_usage_uuid = user_usage_data.get("component_usage_uuid")
                username = user_usage_data.get("username")

                if not uuid or not component_usage_uuid or not username:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping component user usage without UUID, component_usage_uuid, or username"
                        )
                    )
                    self.stats["component_user_usages"]["errors"] += 1
                    continue

                # Find component usage
                component_usage = ComponentUsage.objects.filter(
                    uuid=component_usage_uuid
                ).first()
                if not component_usage:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping component user usage {uuid}: component_usage {component_usage_uuid} not found"
                        )
                    )
                    self.stats["component_user_usages"]["errors"] += 1
                    continue

                # Find offering user if user_uuid provided
                offering_user = None
                user_uuid = user_usage_data.get("user_uuid")
                if user_uuid:
                    offering_user = OfferingUser.objects.filter(uuid=user_uuid).first()

                defaults = {
                    "component_usage": component_usage,
                    "username": username,
                    "usage": user_usage_data.get("usage", 0),
                    "user": offering_user,
                    "description": user_usage_data.get("description", ""),
                    "backend_id": user_usage_data.get("backend_id", ""),
                }

                if not self.dry_run:
                    existing_user_usage = ComponentUserUsage.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_user_usage:
                        if self.update_existing:
                            with transaction.atomic():
                                ComponentUserUsage.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["component_user_usages"]["updated"] += 1
                        else:
                            self.stats["component_user_usages"]["skipped"] += 1
                    else:
                        # Check for duplicate by unique constraint
                        duplicate = ComponentUserUsage.objects.filter(
                            username=username,
                            component_usage=component_usage,
                        ).first()

                        if duplicate:
                            if self.update_existing:
                                with transaction.atomic():
                                    ComponentUserUsage.objects.filter(
                                        pk=duplicate.pk
                                    ).update(uuid=uuid, **defaults)
                                self.stats["component_user_usages"]["updated"] += 1
                            else:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"Skipping component user usage {uuid}: duplicate exists with UUID {duplicate.uuid}"
                                    )
                                )
                                self.stats["component_user_usages"]["skipped"] += 1
                        else:
                            with transaction.atomic():
                                ComponentUserUsage.objects.create(uuid=uuid, **defaults)
                            self.stats["component_user_usages"]["created"] += 1
                else:
                    existing = ComponentUserUsage.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["component_user_usages"]["updated"] += 1
                        else:
                            self.stats["component_user_usages"]["skipped"] += 1
                    else:
                        duplicate_exists = ComponentUserUsage.objects.filter(
                            username=username,
                            component_usage=component_usage,
                        ).exists()

                        if duplicate_exists:
                            if self.update_existing:
                                self.stats["component_user_usages"]["updated"] += 1
                            else:
                                self.stats["component_user_usages"]["skipped"] += 1
                        else:
                            self.stats["component_user_usages"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import component user usage {user_usage_data.get('uuid')}: {e}"
                    )
                )
                self.stats["component_user_usages"]["errors"] += 1

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
                            with transaction.atomic():
                                Invoice.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["invoices"]["updated"] += 1
                        else:
                            self.stats["invoices"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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

        # Pre-fetch lookup maps to avoid N+1 queries
        invoice_map = {str(i.uuid): i for i in Invoice.objects.all()}
        resource_map = {str(r.uuid): r for r in Resource.objects.all()}
        project_map = {str(p.uuid): p for p in Project.available_objects.all()}
        plan_component_map = {pc.id: pc for pc in PlanComponent.objects.all()}
        existing_items = {str(ii.uuid): ii for ii in InvoiceItem.objects.all()}

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
                invoice = invoice_map.get(self._normalize_uuid(invoice_uuid))
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
                    resource = resource_map.get(self._normalize_uuid(resource_uuid))

                # Find project (optional)
                project = None
                project_uuid = item_data.get("project_uuid")
                if project_uuid:
                    project = project_map.get(self._normalize_uuid(project_uuid))

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
                    plan_component = plan_component_map.get(plan_component_id)

                # Parse backend_uuid
                backend_uuid = None
                if item_data.get("backend_uuid"):
                    try:
                        backend_uuid = UUID(item_data["backend_uuid"])
                    except (ValueError, TypeError):
                        pass

                # Find optional credit FK (set on credit-compensation rows;
                # null on regular charges and manual cost adjustments).
                credit = None
                credit_uuid = item_data.get("credit_uuid")
                if credit_uuid:
                    credit = CustomerCredit.objects.filter(
                        uuid=self._normalize_uuid(credit_uuid)
                    ).first()

                defaults = {
                    "invoice": invoice,
                    "resource": resource,
                    "project": project,
                    "name": item_data.get("name", ""),
                    "quantity": item_data.get("quantity", 0),
                    "measured_unit": item_data.get("measured_unit", ""),
                    "unit_price": item_data.get("unit_price", 0),
                    "article_code": item_data.get("article_code", ""),
                    "backend_uuid": backend_uuid,
                    "details": item_data.get("details", {}),
                    "plan_component": plan_component,
                    "credit": credit,
                }

                # Only set start/end if provided, otherwise let model use defaults
                if start is not None:
                    defaults["start"] = start
                if end is not None:
                    defaults["end"] = end

                if not self.dry_run:
                    normalized_uuid = self._normalize_uuid(uuid)
                    existing_item = existing_items.get(normalized_uuid)

                    if existing_item:
                        if self.update_existing:
                            with transaction.atomic():
                                InvoiceItem.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["invoice_items"]["updated"] += 1
                        else:
                            self.stats["invoice_items"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            obj = InvoiceItem.objects.create(uuid=uuid, **defaults)
                        existing_items[normalized_uuid] = obj
                        self.stats["invoice_items"]["created"] += 1
                else:
                    existing = self._normalize_uuid(uuid) in existing_items
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

        # Pre-fetch lookup maps to avoid N+1 queries
        project_map = {str(p.uuid): p for p in Project.available_objects.all()}
        resource_map = {str(r.uuid): r for r in Resource.objects.all()}
        offering_map = {str(o.uuid): o for o in Offering.objects.all()}
        user_map = {str(u.uuid): u for u in User.all_objects.all()}
        plan_map = {str(p.uuid): p for p in Plan.objects.all()}
        existing_orders = {str(o.uuid): o for o in Order.objects.all()}

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
                project = project_map.get(self._normalize_uuid(project_uuid))
                if not project:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping order {uuid}: project {project_uuid} not found"
                        )
                    )
                    self.stats["orders"]["errors"] += 1
                    continue

                # Find resource
                resource = resource_map.get(self._normalize_uuid(resource_uuid))
                if not resource:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping order {uuid}: resource {resource_uuid} not found"
                        )
                    )
                    self.stats["orders"]["errors"] += 1
                    continue

                # Find offering
                offering = offering_map.get(self._normalize_uuid(offering_uuid))
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping order {uuid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["orders"]["errors"] += 1
                    continue

                # Find created_by user
                created_by = user_map.get(self._normalize_uuid(created_by_uuid))
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
                    plan = plan_map.get(self._normalize_uuid(plan_uuid))

                # Find old_plan (optional)
                old_plan = None
                old_plan_uuid = order_data.get("old_plan_uuid")
                if old_plan_uuid:
                    old_plan = plan_map.get(self._normalize_uuid(old_plan_uuid))

                # Find consumer_reviewed_by (optional)
                consumer_reviewed_by = None
                consumer_reviewed_by_uuid = order_data.get("consumer_reviewed_by_uuid")
                if consumer_reviewed_by_uuid:
                    consumer_reviewed_by = user_map.get(
                        self._normalize_uuid(consumer_reviewed_by_uuid)
                    )

                # Find provider_reviewed_by (optional)
                provider_reviewed_by = None
                provider_reviewed_by_uuid = order_data.get("provider_reviewed_by_uuid")
                if provider_reviewed_by_uuid:
                    provider_reviewed_by = user_map.get(
                        self._normalize_uuid(provider_reviewed_by_uuid)
                    )

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

                # Parse created timestamp (for user actions detection)
                created = None
                if order_data.get("created"):
                    try:
                        created = datetime.fromisoformat(order_data["created"])
                        if timezone.is_naive(created):
                            created = timezone.make_aware(created)
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
                    normalized_uuid = self._normalize_uuid(uuid)
                    existing_order = existing_orders.get(normalized_uuid)

                    if existing_order:
                        if self.update_existing:
                            with transaction.atomic():
                                Order.objects.filter(uuid=uuid).update(**defaults)
                                # Update created timestamp if provided
                                if created:
                                    Order.objects.filter(uuid=uuid).update(
                                        created=created
                                    )
                            self.stats["orders"]["updated"] += 1
                        else:
                            self.stats["orders"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            order = Order.objects.create(uuid=uuid, **defaults)
                            # Update created timestamp if provided
                            if created:
                                Order.objects.filter(pk=order.pk).update(
                                    created=created
                                )
                        existing_orders[normalized_uuid] = order
                        self.stats["orders"]["created"] += 1
                else:
                    existing = self._normalize_uuid(uuid) in existing_orders
                    if existing:
                        if self.update_existing:
                            self.stats["orders"]["updated"] += 1
                        else:
                            self.stats["orders"]["skipped"] += 1
                    else:
                        self.stats["orders"]["created"] += 1

            except Exception as e:
                import traceback

                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import order {order_data.get('uuid')}: {e}"
                    )
                )
                self.stdout.write(self.style.WARNING(traceback.format_exc()))
                self.stats["orders"]["errors"] += 1

    def import_offering_users(self, offering_users_data):
        """Import offering user data."""
        self.stdout.write("Importing offering users...")

        # Pre-fetch lookup maps to avoid N+1 queries
        offering_map = {str(o.uuid): o for o in Offering.objects.all()}
        user_map = {str(u.uuid): u for u in User.all_objects.all()}
        existing_map = {str(ou.uuid): ou for ou in OfferingUser.objects.all()}

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
                offering = offering_map.get(self._normalize_uuid(offering_uuid))
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping offering user {uuid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["offering_users"]["errors"] += 1
                    continue

                # Find user
                user = user_map.get(self._normalize_uuid(user_uuid))
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
                    "backend_metadata": offering_user_data.get("backend_metadata", {}),
                    "service_provider_comment": offering_user_data.get(
                        "service_provider_comment", ""
                    ),
                    "service_provider_comment_url": offering_user_data.get(
                        "service_provider_comment_url", ""
                    ),
                }

                if not self.dry_run:
                    normalized_uuid = self._normalize_uuid(uuid)
                    existing_offering_user = existing_map.get(normalized_uuid)

                    if existing_offering_user:
                        if self.update_existing:
                            with transaction.atomic():
                                OfferingUser.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["offering_users"]["updated"] += 1
                        else:
                            self.stats["offering_users"]["skipped"] += 1
                    else:
                        # Check if an OfferingUser with the same (offering, user) already exists
                        existing_by_pair = OfferingUser.objects.filter(
                            offering=offering, user=user
                        ).first()
                        if existing_by_pair:
                            if self.update_existing:
                                with transaction.atomic():
                                    OfferingUser.objects.filter(
                                        pk=existing_by_pair.pk
                                    ).update(**defaults)
                                self.stats["offering_users"]["updated"] += 1
                            else:
                                self.stats["offering_users"]["skipped"] += 1
                        else:
                            with transaction.atomic():
                                obj = OfferingUser.objects.create(
                                    uuid=UUID(uuid), **defaults
                                )
                            existing_map[normalized_uuid] = obj
                            self.stats["offering_users"]["created"] += 1
                else:
                    existing = self._normalize_uuid(uuid) in existing_map
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

    def import_robot_accounts(self, robot_accounts_data):
        """Import robot account data."""
        self.stdout.write("Importing robot accounts...")

        # Pre-fetch lookup maps to avoid N+1 queries
        resource_map = {str(r.uuid): r for r in Resource.objects.all()}
        user_map = {str(u.uuid): u for u in User.all_objects.all()}
        existing_map = {str(ra.uuid): ra for ra in RobotAccount.objects.all()}

        for robot_account_data in robot_accounts_data:
            try:
                uuid = robot_account_data.get("uuid")
                resource_uuid = robot_account_data.get("resource_uuid")

                if not uuid or not resource_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping robot account without UUID or resource_uuid"
                        )
                    )
                    self.stats["robot_accounts"]["errors"] += 1
                    continue

                # Find resource
                resource = resource_map.get(self._normalize_uuid(resource_uuid))
                if not resource:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping robot account {uuid}: resource {resource_uuid} not found"
                        )
                    )
                    self.stats["robot_accounts"]["errors"] += 1
                    continue

                defaults = {
                    "resource": resource,
                    "username": robot_account_data.get("username", ""),
                    "description": robot_account_data.get("description", ""),
                    "type": robot_account_data.get("type", ""),
                    "keys": robot_account_data.get("keys", []),
                    "state": robot_account_data.get(
                        "state", RobotAccountStates.REQUESTED
                    ),
                    "backend_metadata": robot_account_data.get("backend_metadata", {}),
                    "backend_id": robot_account_data.get("backend_id", ""),
                }

                responsible_user_uuid = robot_account_data.get("responsible_user_uuid")
                if responsible_user_uuid:
                    defaults["responsible_user"] = user_map.get(
                        self._normalize_uuid(responsible_user_uuid)
                    )

                users = [
                    user_map[self._normalize_uuid(user_uuid)]
                    for user_uuid in robot_account_data.get("user_uuids", [])
                    if self._normalize_uuid(user_uuid) in user_map
                ]

                if not self.dry_run:
                    normalized_uuid = self._normalize_uuid(uuid)
                    existing_robot_account = existing_map.get(normalized_uuid)

                    if existing_robot_account:
                        if self.update_existing:
                            with transaction.atomic():
                                RobotAccount.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                                existing_robot_account.users.set(users)
                            self.stats["robot_accounts"]["updated"] += 1
                        else:
                            self.stats["robot_accounts"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            obj = RobotAccount.objects.create(
                                uuid=UUID(uuid), **defaults
                            )
                            obj.users.set(users)
                        existing_map[normalized_uuid] = obj
                        self.stats["robot_accounts"]["created"] += 1
                else:
                    existing = self._normalize_uuid(uuid) in existing_map
                    if existing:
                        if self.update_existing:
                            self.stats["robot_accounts"]["updated"] += 1
                        else:
                            self.stats["robot_accounts"]["skipped"] += 1
                    else:
                        self.stats["robot_accounts"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import robot account {robot_account_data.get('uuid')}: {e}"
                    )
                )
                self.stats["robot_accounts"]["errors"] += 1

    def import_offering_user_groups(self, offering_user_groups_data):
        """Import offering user group data.

        OfferingUserGroup has no UUID field, so groups are matched by the
        natural key (offering, backend_metadata.gid).
        """
        self.stdout.write("Importing offering user groups...")

        # Pre-fetch lookup maps to avoid N+1 queries
        offering_map = {str(o.uuid): o for o in Offering.objects.all()}
        project_map = {str(p.uuid): p for p in Project.objects.all()}

        for group_data in offering_user_groups_data:
            try:
                offering_uuid = group_data.get("offering_uuid")
                backend_metadata = group_data.get("backend_metadata", {})
                gid = backend_metadata.get("gid")

                if not offering_uuid or gid is None:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping offering user group without offering_uuid or backend_metadata.gid"
                        )
                    )
                    self.stats["offering_user_groups"]["errors"] += 1
                    continue

                # Find offering
                offering = offering_map.get(self._normalize_uuid(offering_uuid))
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping offering user group with gid {gid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["offering_user_groups"]["errors"] += 1
                    continue

                projects = [
                    project_map[self._normalize_uuid(project_uuid)]
                    for project_uuid in group_data.get("project_uuids", [])
                    if self._normalize_uuid(project_uuid) in project_map
                ]

                existing_group = OfferingUserGroup.objects.filter(
                    offering=offering, backend_metadata__gid=gid
                ).first()

                if not self.dry_run:
                    if existing_group:
                        if self.update_existing:
                            with transaction.atomic():
                                existing_group.backend_metadata = backend_metadata
                                existing_group.save(update_fields=["backend_metadata"])
                                existing_group.projects.set(projects)
                            self.stats["offering_user_groups"]["updated"] += 1
                        else:
                            self.stats["offering_user_groups"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            group = OfferingUserGroup.objects.create(
                                offering=offering, backend_metadata=backend_metadata
                            )
                            group.projects.set(projects)
                        self.stats["offering_user_groups"]["created"] += 1
                else:
                    if existing_group:
                        if self.update_existing:
                            self.stats["offering_user_groups"]["updated"] += 1
                        else:
                            self.stats["offering_user_groups"]["skipped"] += 1
                    else:
                        self.stats["offering_user_groups"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import offering user group for offering {group_data.get('offering_uuid')}: {e}"
                    )
                )
                self.stats["offering_user_groups"]["errors"] += 1

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
                }

                if not self.dry_run:
                    existing_checklist = Checklist.objects.filter(uuid=uuid).first()

                    if existing_checklist:
                        if self.update_existing:
                            with transaction.atomic():
                                Checklist.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["checklists"]["updated"] += 1
                        else:
                            self.stats["checklists"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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
                            with transaction.atomic():
                                Question.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["questions"]["updated"] += 1
                        else:
                            self.stats["questions"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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

    def import_question_options(self, options_data):
        """Import question option data for select-type questions."""
        self.stdout.write("Importing question options...")

        for option_data in options_data:
            try:
                uuid = option_data.get("uuid")
                question_uuid = option_data.get("question_uuid")

                if not uuid or not question_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping question option without UUID or question_uuid"
                        )
                    )
                    self.stats["question_options"]["errors"] += 1
                    continue

                # Find question
                question = Question.objects.filter(uuid=question_uuid).first()
                if not question:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping option {uuid}: question {question_uuid} not found"
                        )
                    )
                    self.stats["question_options"]["errors"] += 1
                    continue

                defaults = {
                    "question": question,
                    "label": option_data.get("label", ""),
                    "order": option_data.get("order", 0),
                }

                if not self.dry_run:
                    existing_option = QuestionOption.objects.filter(uuid=uuid).first()

                    if existing_option:
                        if self.update_existing:
                            with transaction.atomic():
                                QuestionOption.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["question_options"]["updated"] += 1
                        else:
                            self.stats["question_options"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            QuestionOption.objects.create(uuid=UUID(uuid), **defaults)
                        self.stats["question_options"]["created"] += 1
                else:
                    existing = QuestionOption.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["question_options"]["updated"] += 1
                        else:
                            self.stats["question_options"]["skipped"] += 1
                    else:
                        self.stats["question_options"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import question option {option_data.get('uuid')}: {e}"
                    )
                )
                self.stats["question_options"]["errors"] += 1

    def import_question_dependencies(self, dependencies_data):
        """Import question dependency data for conditional visibility."""
        self.stdout.write("Importing question dependencies...")

        for dep_data in dependencies_data:
            try:
                uuid = dep_data.get("uuid")
                question_uuid = dep_data.get("question_uuid")
                depends_on_uuid = dep_data.get("depends_on_question_uuid")

                if not uuid or not question_uuid or not depends_on_uuid:
                    self.stdout.write(
                        self.style.WARNING("Skipping dependency without required UUIDs")
                    )
                    self.stats["question_dependencies"]["errors"] += 1
                    continue

                # Find both questions
                question = Question.objects.filter(uuid=question_uuid).first()
                depends_on = Question.objects.filter(uuid=depends_on_uuid).first()

                if not question:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping dependency {uuid}: question {question_uuid} not found"
                        )
                    )
                    self.stats["question_dependencies"]["errors"] += 1
                    continue

                if not depends_on:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping dependency {uuid}: depends_on question {depends_on_uuid} not found"
                        )
                    )
                    self.stats["question_dependencies"]["errors"] += 1
                    continue

                defaults = {
                    "question": question,
                    "depends_on_question": depends_on,
                    "required_answer_value": dep_data.get("required_answer_value"),
                    "operator": dep_data.get("operator", "equals"),
                }

                if not self.dry_run:
                    existing_dep = QuestionDependency.objects.filter(uuid=uuid).first()

                    if existing_dep:
                        if self.update_existing:
                            with transaction.atomic():
                                QuestionDependency.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["question_dependencies"]["updated"] += 1
                        else:
                            self.stats["question_dependencies"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            QuestionDependency.objects.create(
                                uuid=UUID(uuid), **defaults
                            )
                        self.stats["question_dependencies"]["created"] += 1
                else:
                    existing = QuestionDependency.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["question_dependencies"]["updated"] += 1
                        else:
                            self.stats["question_dependencies"]["skipped"] += 1
                    else:
                        self.stats["question_dependencies"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import question dependency {dep_data.get('uuid')}: {e}"
                    )
                )
                self.stats["question_dependencies"]["errors"] += 1

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

                # For proposal scope, try to resolve by UUID if provided
                if model == "proposal" and completion_data.get("scope_object_uuid"):
                    try:
                        proposal = Proposal.objects.get(
                            uuid=completion_data["scope_object_uuid"]
                        )
                        scope_object_id = proposal.id
                    except Proposal.DoesNotExist:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping completion {uuid}: proposal {completion_data['scope_object_uuid']} not found"
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
                    # First check if completion with this UUID already exists
                    existing_completion = ChecklistCompletion.objects.filter(
                        uuid=uuid
                    ).first()

                    if existing_completion:
                        if self.update_existing:
                            with transaction.atomic():
                                ChecklistCompletion.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["checklist_completions"]["updated"] += 1
                        else:
                            self.stats["checklist_completions"]["skipped"] += 1
                    else:
                        # Check if a completion already exists for this scope+checklist
                        # (auto-created by proposal creation)
                        existing_by_scope = ChecklistCompletion.objects.filter(
                            scope_content_type=content_type,
                            scope_object_id=scope_object_id,
                            checklist=checklist,
                        ).first()

                        if existing_by_scope:
                            # Update the existing completion's UUID to match preset
                            existing_by_scope.uuid = UUID(uuid)
                            if created:
                                existing_by_scope.created = created
                            if modified:
                                existing_by_scope.modified = modified
                            with transaction.atomic():
                                existing_by_scope.save()
                            self.stats["checklist_completions"]["updated"] += 1
                        else:
                            with transaction.atomic():
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
                            with transaction.atomic():
                                Answer.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["answers"]["updated"] += 1
                        else:
                            self.stats["answers"]["skipped"] += 1
                    else:
                        with transaction.atomic():
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

    def import_group_invitations(self, group_invitations_data):
        """Import group invitation data."""
        self.stdout.write("Importing group invitations...")
        for group_invitation_data in group_invitations_data:
            try:
                uuid = group_invitation_data.get("uuid")
                customer_uuid = group_invitation_data.get("customer_uuid")
                role_uuid = group_invitation_data.get("role_uuid")
                role_name = group_invitation_data.get("role_name")
                if not uuid or not customer_uuid or (not role_uuid and not role_name):
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping group invitation without required fields (uuid, customer_uuid, role_uuid/role_name)"
                        )
                    )
                    self.stats["group_invitations"]["errors"] += 1
                    continue

                # Find customer
                customer = Customer.objects.filter(uuid=customer_uuid).first()
                if not customer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping group invitation {uuid}: customer {customer_uuid} not found"
                        )
                    )
                    self.stats["group_invitations"]["errors"] += 1
                    continue

                # Find role by UUID or by name (create system role if needed)
                role = None
                if role_uuid:
                    role = Role.objects.filter(uuid=role_uuid).first()
                if not role and role_name:
                    role = Role.objects.filter(name=role_name).first()
                    # If role not found, create it as a system role
                    if not role:
                        # Determine content_type from role name prefix
                        role_content_type = None
                        if role_name.startswith("CUSTOMER."):
                            role_content_type = ContentType.objects.get(
                                app_label="structure", model="customer"
                            )
                        elif role_name.startswith("PROJECT."):
                            role_content_type = ContentType.objects.get(
                                app_label="structure", model="project"
                            )
                        if role_content_type:
                            role = Role.objects.get_system_role(
                                role_name, role_content_type
                            )
                if not role:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping group invitation {uuid}: role {role_uuid or role_name} not found"
                        )
                    )
                    self.stats["group_invitations"]["errors"] += 1
                    continue

                # Find project role (optional) by UUID or by name
                project_role = None
                project_role_uuid = group_invitation_data.get("project_role_uuid")
                project_role_name = group_invitation_data.get("project_role_name")
                if project_role_uuid:
                    project_role = Role.objects.filter(uuid=project_role_uuid).first()
                if not project_role and project_role_name:
                    project_role = Role.objects.filter(name=project_role_name).first()
                    # If project role not found, create it as a system role
                    if not project_role:
                        project_role_ct = ContentType.objects.get(
                            app_label="structure", model="project"
                        )
                        project_role = Role.objects.get_system_role(
                            project_role_name, project_role_ct
                        )

                # Find created_by (optional)
                created_by = None
                created_by_uuid = group_invitation_data.get("created_by_uuid")
                if created_by_uuid:
                    created_by = User.all_objects.filter(uuid=created_by_uuid).first()

                # Parse dates
                created = None
                if group_invitation_data.get("created"):
                    try:
                        created = datetime.fromisoformat(
                            group_invitation_data["created"]
                        )
                        if timezone.is_naive(created):
                            created = timezone.make_aware(created)
                    except (ValueError, TypeError):
                        pass

                modified = None
                if group_invitation_data.get("modified"):
                    try:
                        modified = datetime.fromisoformat(
                            group_invitation_data["modified"]
                        )
                        if timezone.is_naive(modified):
                            modified = timezone.make_aware(modified)
                    except (ValueError, TypeError):
                        pass

                # Parse scope information
                scope_content_type = group_invitation_data.get("scope_content_type")
                scope_uuid = group_invitation_data.get("scope_uuid")
                content_type = None
                object_id = None
                if scope_content_type:
                    try:
                        app_label, model = scope_content_type.split(".")
                        content_type = ContentType.objects.get(
                            app_label=app_label, model=model
                        )
                    except (ValueError, ContentType.DoesNotExist):
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping group invitation {uuid}: invalid scope_content_type {scope_content_type}"
                            )
                        )
                        self.stats["group_invitations"]["errors"] += 1
                        continue

                    # Resolve scope object by UUID to get integer object_id
                    if scope_uuid and content_type:
                        model_class = content_type.model_class()
                        if model_class:
                            scope_object = model_class.objects.filter(
                                uuid=scope_uuid
                            ).first()
                            if scope_object:
                                object_id = scope_object.id
                            else:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"Skipping group invitation {uuid}: scope object {scope_uuid} not found"
                                    )
                                )
                                self.stats["group_invitations"]["errors"] += 1
                                continue

                defaults = {
                    "customer": customer,
                    "role": role,
                    "project_role": project_role,
                    "created_by": created_by,
                    "is_active": group_invitation_data.get("is_active", True),
                    "is_public": group_invitation_data.get("is_public", False),
                    "auto_create_project": group_invitation_data.get(
                        "auto_create_project", False
                    ),
                    "content_type": content_type,
                    "object_id": object_id,
                }

                if not self.dry_run:
                    existing_group_invitation = GroupInvitation.objects.filter(
                        uuid=uuid
                    ).first()
                    if existing_group_invitation:
                        if self.update_existing:
                            with transaction.atomic():
                                GroupInvitation.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["group_invitations"]["updated"] += 1
                        else:
                            self.stats["group_invitations"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            group_invitation = GroupInvitation.objects.create(
                                uuid=UUID(uuid), **defaults
                            )
                            # Set timestamps after creation
                            if created:
                                group_invitation.created = created
                            if modified:
                                group_invitation.modified = modified
                            if created or modified:
                                group_invitation.save()
                        self.stats["group_invitations"]["created"] += 1
                else:
                    existing = GroupInvitation.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["group_invitations"]["updated"] += 1
                        else:
                            self.stats["group_invitations"]["skipped"] += 1
                    else:
                        self.stats["group_invitations"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import group invitation {group_invitation_data.get('uuid')}: {e}"
                    )
                )
                self.stats["group_invitations"]["errors"] += 1

    def import_invitations(self, invitations_data):
        """Import invitation data."""
        self.stdout.write("Importing invitations...")

        # Pre-fetch lookup maps to avoid N+1 queries
        customer_map = {str(c.uuid): c for c in Customer.objects.all()}
        role_by_uuid = {str(r.uuid): r for r in Role.objects.all()}
        role_by_name = {r.name: r for r in role_by_uuid.values()}
        user_map = {str(u.uuid): u for u in User.all_objects.all()}
        existing_invitations = {str(inv.uuid): inv for inv in Invitation.objects.all()}

        # Pre-fetch ContentType map
        content_type_map = {
            (ct.app_label, ct.model): ct for ct in ContentType.objects.all()
        }

        # Pre-fetch common scope objects for scope resolution
        project_by_uuid = {str(p.uuid): p for p in Project.objects.all()}
        scope_cache = {
            ("structure", "customer"): customer_map,
            ("structure", "project"): project_by_uuid,
        }

        for invitation_data in invitations_data:
            try:
                uuid = invitation_data.get("uuid")
                customer_uuid = invitation_data.get("customer_uuid")
                role_uuid = invitation_data.get("role_uuid")
                role_name = invitation_data.get("role_name")
                email = invitation_data.get("email")
                if (
                    not uuid
                    or not customer_uuid
                    or (not role_uuid and not role_name)
                    or not email
                ):
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping invitation without required fields (uuid, customer_uuid, role_uuid/role_name, email)"
                        )
                    )
                    self.stats["invitations"]["errors"] += 1
                    continue

                # Find customer
                customer = customer_map.get(self._normalize_uuid(customer_uuid))
                if not customer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping invitation {uuid}: customer {customer_uuid} not found"
                        )
                    )
                    self.stats["invitations"]["errors"] += 1
                    continue

                # Find role by UUID or by name (create system role if needed)
                role = None
                if role_uuid:
                    role = role_by_uuid.get(self._normalize_uuid(role_uuid))
                if not role and role_name:
                    role = role_by_name.get(role_name)
                    # If role not found, create it as a system role
                    if not role:
                        # Determine content_type from role name prefix
                        role_content_type = None
                        if role_name.startswith("CUSTOMER."):
                            role_content_type = content_type_map.get(
                                ("structure", "customer")
                            )
                        elif role_name.startswith("PROJECT."):
                            role_content_type = content_type_map.get(
                                ("structure", "project")
                            )
                        if role_content_type:
                            role = Role.objects.get_system_role(
                                role_name, role_content_type
                            )
                            # Cache the newly created/fetched role
                            role_by_uuid[str(role.uuid)] = role
                            role_by_name[role.name] = role
                if not role:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping invitation {uuid}: role {role_uuid or role_name} not found"
                        )
                    )
                    self.stats["invitations"]["errors"] += 1
                    continue

                # Find created_by (optional)
                created_by = None
                created_by_uuid = invitation_data.get("created_by_uuid")
                if created_by_uuid:
                    created_by = user_map.get(self._normalize_uuid(created_by_uuid))

                # Find approved_by (optional)
                approved_by = None
                approved_by_uuid = invitation_data.get("approved_by_uuid")
                if approved_by_uuid:
                    approved_by = user_map.get(self._normalize_uuid(approved_by_uuid))

                # Parse dates
                created = None
                if invitation_data.get("created"):
                    try:
                        created = datetime.fromisoformat(invitation_data["created"])
                        if timezone.is_naive(created):
                            created = timezone.make_aware(created)
                    except (ValueError, TypeError):
                        pass

                modified = None
                if invitation_data.get("modified"):
                    try:
                        modified = datetime.fromisoformat(invitation_data["modified"])
                        if timezone.is_naive(modified):
                            modified = timezone.make_aware(modified)
                    except (ValueError, TypeError):
                        pass

                # Parse scope information
                scope_content_type = invitation_data.get("scope_content_type")
                scope_uuid = invitation_data.get("scope_uuid")
                content_type = None
                object_id = None
                if scope_content_type:
                    try:
                        app_label, model = scope_content_type.split(".")
                        content_type = content_type_map.get((app_label, model))
                        if not content_type:
                            raise ContentType.DoesNotExist
                    except (ValueError, ContentType.DoesNotExist):
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping invitation {uuid}: invalid scope_content_type {scope_content_type}"
                            )
                        )
                        self.stats["invitations"]["errors"] += 1
                        continue

                    # Resolve scope object by UUID to get integer object_id
                    if scope_uuid and content_type:
                        model_class = content_type.model_class()
                        if model_class:
                            # Use pre-fetched scope maps for common types, fall back to DB for rare ones
                            scope_map = scope_cache.get((app_label, model))
                            if scope_map is not None:
                                scope_object = scope_map.get(
                                    self._normalize_uuid(scope_uuid)
                                )
                            else:
                                scope_object = model_class.objects.filter(
                                    uuid=scope_uuid
                                ).first()
                            if scope_object:
                                object_id = scope_object.id
                            else:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"Skipping invitation {uuid}: scope object {scope_uuid} not found"
                                    )
                                )
                                self.stats["invitations"]["errors"] += 1
                                continue

                defaults = {
                    "customer": customer,
                    "role": role,
                    "created_by": created_by,
                    "approved_by": approved_by,
                    "email": email,
                    "civil_number": invitation_data.get("civil_number", ""),
                    "full_name": invitation_data.get("full_name", ""),
                    "state": invitation_data.get("state", "pending"),
                    "execution_state": invitation_data.get(
                        "execution_state", "Scheduled"
                    ),
                    "extra_invitation_text": invitation_data.get(
                        "extra_invitation_text", ""
                    ),
                    "error_message": invitation_data.get("error_message", ""),
                    "error_traceback": invitation_data.get("error_traceback", ""),
                    "content_type": content_type,
                    "object_id": object_id,
                }

                if not self.dry_run:
                    normalized_uuid = self._normalize_uuid(uuid)
                    existing_invitation = existing_invitations.get(normalized_uuid)
                    if existing_invitation:
                        if self.update_existing:
                            with transaction.atomic():
                                Invitation.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["invitations"]["updated"] += 1
                        else:
                            self.stats["invitations"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            invitation = Invitation.objects.create(
                                uuid=UUID(uuid), **defaults
                            )
                            # Set timestamps after creation
                            if created:
                                invitation.created = created
                            if modified:
                                invitation.modified = modified
                            if created or modified:
                                invitation.save()
                        existing_invitations[normalized_uuid] = invitation
                        self.stats["invitations"]["created"] += 1
                else:
                    existing = self._normalize_uuid(uuid) in existing_invitations
                    if existing:
                        if self.update_existing:
                            self.stats["invitations"]["updated"] += 1
                        else:
                            self.stats["invitations"]["skipped"] += 1
                    else:
                        self.stats["invitations"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import invitation {invitation_data.get('uuid')}: {e}"
                    )
                )
                self.stats["invitations"]["errors"] += 1

    def import_permission_requests(self, permission_requests_data):
        """Import permission request data."""
        self.stdout.write("Importing permission requests...")
        for permission_request_data in permission_requests_data:
            try:
                uuid = permission_request_data.get("uuid")
                invitation_uuid = permission_request_data.get("invitation_uuid")
                created_by_uuid = permission_request_data.get("created_by_uuid")
                if not uuid or not invitation_uuid or not created_by_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping permission request without required fields"
                        )
                    )
                    self.stats["permission_requests"]["errors"] += 1
                    continue

                # Find invitation
                invitation = GroupInvitation.objects.filter(
                    uuid=invitation_uuid
                ).first()
                if not invitation:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping permission request {uuid}: invitation {invitation_uuid} not found"
                        )
                    )
                    self.stats["permission_requests"]["errors"] += 1
                    continue

                # Find created_by
                created_by = User.all_objects.filter(uuid=created_by_uuid).first()
                if not created_by:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping permission request {uuid}: user {created_by_uuid} not found"
                        )
                    )
                    self.stats["permission_requests"]["errors"] += 1
                    continue

                # Find reviewed_by (optional)
                reviewed_by = None
                reviewed_by_uuid = permission_request_data.get("reviewed_by_uuid")
                if reviewed_by_uuid:
                    reviewed_by = User.all_objects.filter(uuid=reviewed_by_uuid).first()

                # Parse dates
                created = None
                if permission_request_data.get("created"):
                    try:
                        created = datetime.fromisoformat(
                            permission_request_data["created"]
                        )
                        if timezone.is_naive(created):
                            created = timezone.make_aware(created)
                    except (ValueError, TypeError):
                        pass

                modified = None
                if permission_request_data.get("modified"):
                    try:
                        modified = datetime.fromisoformat(
                            permission_request_data["modified"]
                        )
                        if timezone.is_naive(modified):
                            modified = timezone.make_aware(modified)
                    except (ValueError, TypeError):
                        pass

                reviewed_at = None
                if permission_request_data.get("reviewed_at"):
                    try:
                        reviewed_at = datetime.fromisoformat(
                            permission_request_data["reviewed_at"]
                        )
                        if timezone.is_naive(reviewed_at):
                            reviewed_at = timezone.make_aware(reviewed_at)
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "invitation": invitation,
                    "created_by": created_by,
                    "reviewed_by": reviewed_by,
                    "reviewed_at": reviewed_at,
                    "state": permission_request_data.get(
                        "state", 2
                    ),  # Default to PENDING
                    "review_comment": permission_request_data.get("review_comment", ""),
                }

                if not self.dry_run:
                    existing_permission_request = PermissionRequest.objects.filter(
                        uuid=uuid
                    ).first()
                    if existing_permission_request:
                        if self.update_existing:
                            with transaction.atomic():
                                PermissionRequest.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["permission_requests"]["updated"] += 1
                        else:
                            self.stats["permission_requests"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            permission_request = PermissionRequest.objects.create(
                                uuid=UUID(uuid), **defaults
                            )
                            # Set timestamps after creation
                            if created:
                                permission_request.created = created
                            if modified:
                                permission_request.modified = modified
                            if created or modified:
                                permission_request.save()
                        self.stats["permission_requests"]["created"] += 1
                else:
                    existing = PermissionRequest.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["permission_requests"]["updated"] += 1
                        else:
                            self.stats["permission_requests"]["skipped"] += 1
                    else:
                        self.stats["permission_requests"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import permission request {permission_request_data.get('uuid')}: {e}"
                    )
                )
                self.stats["permission_requests"]["errors"] += 1

    def import_customer_credits(self, customer_credits_data):
        """Import customer credit data."""
        self.stdout.write("Importing customer credits...")
        for credit_data in customer_credits_data:
            try:
                uuid = credit_data.get("uuid")
                customer_uuid = credit_data.get("customer_uuid")
                if not uuid or not customer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping customer credit without required fields"
                        )
                    )
                    self.stats["customer_credits"]["errors"] += 1
                    continue

                # Find customer
                customer = Customer.objects.filter(uuid=customer_uuid).first()
                if not customer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping customer credit {uuid}: customer {customer_uuid} not found"
                        )
                    )
                    self.stats["customer_credits"]["errors"] += 1
                    continue

                # Parse end_date
                end_date = None
                if credit_data.get("end_date"):
                    try:
                        end_date = datetime.fromisoformat(
                            credit_data["end_date"]
                        ).date()
                        if end_date.day != 1:
                            original = end_date
                            if end_date.month == 12:
                                end_date = end_date.replace(
                                    year=end_date.year + 1, month=1, day=1
                                )
                            else:
                                end_date = end_date.replace(
                                    month=end_date.month + 1, day=1
                                )
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Customer credit {uuid}: end_date adjusted from {original} to {end_date} (must be first day of month)"
                                )
                            )
                    except (ValueError, TypeError):
                        self.stdout.write(
                            self.style.WARNING(
                                f"Invalid end_date for customer credit {uuid}"
                            )
                        )

                # Parse dates
                created = None
                if credit_data.get("created"):
                    try:
                        created = datetime.fromisoformat(
                            credit_data["created"]
                        ).replace(tzinfo=UTC)
                    except (ValueError, TypeError):
                        pass

                modified = None
                if credit_data.get("modified"):
                    try:
                        modified = datetime.fromisoformat(
                            credit_data["modified"]
                        ).replace(tzinfo=UTC)
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "customer": customer,
                    "value": Decimal(credit_data.get("value", "0")),
                    "expected_consumption": Decimal(
                        credit_data.get("expected_consumption", "0")
                    ),
                    "minimal_consumption_logic": credit_data.get(
                        "minimal_consumption_logic", "fixed"
                    ),
                    "grace_coefficient": Decimal(
                        credit_data.get("grace_coefficient", "0")
                    ),
                    "apply_as_minimal_consumption": credit_data.get(
                        "apply_as_minimal_consumption", True
                    ),
                    "end_date": end_date,
                }

                # Set timestamps if provided
                if created:
                    defaults["created"] = created
                if modified:
                    defaults["modified"] = modified

                if not self.dry_run:
                    existing_credit = CustomerCredit.objects.filter(uuid=uuid).first()
                    if existing_credit:
                        if self.update_existing:
                            with transaction.atomic():
                                CustomerCredit.objects.filter(uuid=uuid).update(
                                    **defaults
                                )

                                # Handle many-to-many offerings relationship
                                offering_uuids = credit_data.get("offering_uuids", [])
                                if offering_uuids:
                                    offerings = Offering.objects.filter(
                                        uuid__in=offering_uuids
                                    )
                                    existing_credit.offerings.set(offerings)
                            self.stats["customer_credits"]["updated"] += 1
                        else:
                            self.stats["customer_credits"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            credit = CustomerCredit.objects.create(
                                uuid=uuid, **defaults
                            )

                            # Handle many-to-many offerings relationship
                            offering_uuids = credit_data.get("offering_uuids", [])
                            if offering_uuids:
                                offerings = Offering.objects.filter(
                                    uuid__in=offering_uuids
                                )
                                credit.offerings.set(offerings)

                        self.stats["customer_credits"]["created"] += 1
                else:
                    existing = CustomerCredit.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["customer_credits"]["updated"] += 1
                        else:
                            self.stats["customer_credits"]["skipped"] += 1
                    else:
                        self.stats["customer_credits"]["created"] += 1
            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import customer credit {credit_data.get('uuid')}: {e}"
                    )
                )
                self.stats["customer_credits"]["errors"] += 1

    @staticmethod
    def _parse_iso_date(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).date()
        except (ValueError, TypeError):
            return None

    def import_customer_affiliates(self, affiliates_data):
        """Import affiliate links (CustomerAffiliate)."""
        self.stdout.write("Importing customer affiliates...")
        for item in affiliates_data:
            uuid = item.get("uuid")
            try:
                customer = Customer.objects.filter(
                    uuid=item.get("customer_uuid")
                ).first()
                affiliate = Customer.objects.filter(
                    uuid=item.get("affiliate_uuid")
                ).first()
                if not uuid or not customer or not affiliate:
                    self.stats["customer_affiliates"]["errors"] += 1
                    continue
                defaults = {
                    "customer": customer,
                    "affiliate": affiliate,
                    "fee_percent": Decimal(str(item.get("fee_percent", "0"))),
                    "is_active": item.get("is_active", True),
                    "start_date": self._parse_iso_date(item.get("start_date")),
                    "end_date": self._parse_iso_date(item.get("end_date")),
                }
                if self.dry_run:
                    exists = CustomerAffiliate.objects.filter(uuid=uuid).exists()
                    key = (
                        "updated"
                        if exists and self.update_existing
                        else ("skipped" if exists else "created")
                    )
                    self.stats["customer_affiliates"][key] += 1
                    continue
                existing = CustomerAffiliate.objects.filter(uuid=uuid).first()
                if existing:
                    if self.update_existing:
                        CustomerAffiliate.objects.filter(uuid=uuid).update(**defaults)
                        self.stats["customer_affiliates"]["updated"] += 1
                    else:
                        self.stats["customer_affiliates"]["skipped"] += 1
                else:
                    CustomerAffiliate.objects.create(uuid=uuid, **defaults)
                    self.stats["customer_affiliates"]["created"] += 1
            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(f"Failed to import affiliate {uuid}: {e}")
                )
                self.stats["customer_affiliates"]["errors"] += 1

    def import_credit_transactions(self, transactions_data):
        """Import credit-ledger transactions (CreditTransaction)."""
        self.stdout.write("Importing credit transactions...")
        for item in transactions_data:
            uuid = item.get("uuid")
            try:
                credit = CustomerCredit.objects.filter(
                    uuid=item.get("credit_uuid")
                ).first()
                if not uuid or not credit:
                    self.stats["credit_transactions"]["errors"] += 1
                    continue
                if self.dry_run or CreditTransaction.objects.filter(uuid=uuid).exists():
                    self.stats["credit_transactions"]["skipped"] += 1
                    continue
                amount = Decimal(str(item.get("amount", "0")))
                tx = CreditTransaction.objects.create(
                    uuid=uuid,
                    credit=credit,
                    amount=amount,
                    transaction_type=item.get("transaction_type", "staff_grant"),
                    comment=item.get("comment", ""),
                )
                # Each transaction is a signed delta to the credit value. Apply it
                # via a queryset update so the post_save ledger handler does not
                # record a second (auto staff-grant) transaction for the change.
                CustomerCredit.objects.filter(pk=credit.pk).update(
                    value=F("value") + amount
                )
                created = item.get("created")
                if created:
                    try:
                        CreditTransaction.objects.filter(pk=tx.pk).update(
                            created=datetime.fromisoformat(created).replace(tzinfo=UTC)
                        )
                    except (ValueError, TypeError):
                        pass
                self.stats["credit_transactions"]["created"] += 1
            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import credit transaction {uuid}: {e}"
                    )
                )
                self.stats["credit_transactions"]["errors"] += 1

    def import_affiliate_fee_accruals(self, accruals_data):
        """Import affiliate fee accruals (AffiliateFeeAccrual)."""
        self.stdout.write("Importing affiliate fee accruals...")
        for item in accruals_data:
            uuid = item.get("uuid")
            try:
                link = CustomerAffiliate.objects.filter(
                    uuid=item.get("affiliate_link_uuid")
                ).first()
                invoice = Invoice.objects.filter(uuid=item.get("invoice_uuid")).first()
                if not uuid or not link or not invoice:
                    self.stats["affiliate_fee_accruals"]["errors"] += 1
                    continue
                if (
                    self.dry_run
                    or AffiliateFeeAccrual.objects.filter(uuid=uuid).exists()
                ):
                    self.stats["affiliate_fee_accruals"]["skipped"] += 1
                    continue
                AffiliateFeeAccrual.objects.create(
                    uuid=uuid,
                    affiliate_link=link,
                    invoice=invoice,
                    amount=Decimal(str(item.get("amount", "0"))),
                )
                self.stats["affiliate_fee_accruals"]["created"] += 1
            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(f"Failed to import accrual {uuid}: {e}")
                )
                self.stats["affiliate_fee_accruals"]["errors"] += 1

    def import_project_credits(self, project_credits_data):
        """Import project credit data."""
        self.stdout.write("Importing project credits...")
        for credit_data in project_credits_data:
            try:
                uuid = credit_data.get("uuid")
                project_uuid = credit_data.get("project_uuid")
                if not uuid or not project_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping project credit without required fields"
                        )
                    )
                    self.stats["project_credits"]["errors"] += 1
                    continue

                # Find project
                project = Project.objects.filter(uuid=project_uuid).first()
                if not project:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping project credit {uuid}: project {project_uuid} not found"
                        )
                    )
                    self.stats["project_credits"]["errors"] += 1
                    continue

                # Parse end_date
                end_date = None
                if credit_data.get("end_date"):
                    try:
                        end_date = datetime.fromisoformat(
                            credit_data["end_date"]
                        ).date()
                        if end_date.day != 1:
                            original = end_date
                            if end_date.month == 12:
                                end_date = end_date.replace(
                                    year=end_date.year + 1, month=1, day=1
                                )
                            else:
                                end_date = end_date.replace(
                                    month=end_date.month + 1, day=1
                                )
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Project credit {uuid}: end_date adjusted from {original} to {end_date} (must be first day of month)"
                                )
                            )
                    except (ValueError, TypeError):
                        self.stdout.write(
                            self.style.WARNING(
                                f"Invalid end_date for project credit {uuid}"
                            )
                        )

                # Parse dates
                created = None
                if credit_data.get("created"):
                    try:
                        created = datetime.fromisoformat(
                            credit_data["created"]
                        ).replace(tzinfo=UTC)
                    except (ValueError, TypeError):
                        pass

                modified = None
                if credit_data.get("modified"):
                    try:
                        modified = datetime.fromisoformat(
                            credit_data["modified"]
                        ).replace(tzinfo=UTC)
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "project": project,
                    "value": Decimal(credit_data.get("value", "0")),
                    "expected_consumption": Decimal(
                        credit_data.get("expected_consumption", "0")
                    ),
                    "minimal_consumption_logic": credit_data.get(
                        "minimal_consumption_logic", "fixed"
                    ),
                    "grace_coefficient": Decimal(
                        credit_data.get("grace_coefficient", "0")
                    ),
                    "apply_as_minimal_consumption": credit_data.get(
                        "apply_as_minimal_consumption", True
                    ),
                    "end_date": end_date,
                    "mark_unused_credit_as_spent_on_project_termination": credit_data.get(
                        "mark_unused_credit_as_spent_on_project_termination", False
                    ),
                }

                # Set timestamps if provided
                if created:
                    defaults["created"] = created
                if modified:
                    defaults["modified"] = modified

                if not self.dry_run:
                    existing_credit = ProjectCredit.objects.filter(uuid=uuid).first()
                    if existing_credit:
                        if self.update_existing:
                            with transaction.atomic():
                                ProjectCredit.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["project_credits"]["updated"] += 1
                        else:
                            self.stats["project_credits"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ProjectCredit.objects.create(uuid=uuid, **defaults)
                        self.stats["project_credits"]["created"] += 1
                else:
                    existing = ProjectCredit.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["project_credits"]["updated"] += 1
                        else:
                            self.stats["project_credits"]["skipped"] += 1
                    else:
                        self.stats["project_credits"]["created"] += 1
            except Exception as e:
                customer_name = project.customer.name if project else "N/A"
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import project credit {credit_data.get('uuid')}: {e}"
                        f" | customer: {customer_name}"
                        f" | project: {project.name if project else 'N/A'}"
                        f" | value: {credit_data.get('value')}"
                        f" | end_date: {credit_data.get('end_date')}"
                    )
                )
                self.stats["project_credits"]["errors"] += 1

    def import_events(self, events_data):
        """Import event log data."""
        self.stdout.write("Importing events...")
        for event_data in events_data:
            try:
                uuid = event_data.get("uuid")
                event_type = event_data.get("event_type")
                message = event_data.get("message")

                if not uuid or not event_type or not message:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping event without required fields (uuid, event_type, message)"
                        )
                    )
                    self.stats["events"]["errors"] += 1
                    continue

                # Parse created timestamp
                created = None
                if event_data.get("created"):
                    try:
                        created = datetime.fromisoformat(event_data["created"])
                        if timezone.is_naive(created):
                            created = timezone.make_aware(created)
                    except (ValueError, TypeError):
                        pass

                defaults = {
                    "event_type": event_type,
                    "message": message,
                    "context": event_data.get("context", {}),
                }

                if not self.dry_run:
                    existing_event = Event.objects.filter(uuid=uuid).first()
                    if existing_event:
                        if self.update_existing:
                            with transaction.atomic():
                                Event.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["events"]["updated"] += 1
                        else:
                            self.stats["events"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            event = Event.objects.create(uuid=uuid, **defaults)
                            # Update created timestamp if provided
                            if created:
                                Event.objects.filter(pk=event.pk).update(
                                    created=created
                                )
                        self.stats["events"]["created"] += 1
                else:
                    existing = Event.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["events"]["updated"] += 1
                        else:
                            self.stats["events"]["skipped"] += 1
                    else:
                        self.stats["events"]["created"] += 1
            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import event {event_data.get('uuid')}: {e}"
                    )
                )
                self.stats["events"]["errors"] += 1

    def import_constance_settings(self, settings_data):
        """
        Import constance settings (system configuration).

        Settings are applied using the ConstanceSettingsSerializer which handles
        validation and type conversion for all supported setting types.
        """
        if not settings_data:
            self.stdout.write("No constance settings to import.")
            return

        self.stdout.write("Importing constance settings...")

        # Normalize keys to uppercase (constance uses uppercase keys)
        normalized_settings = {
            key.upper(): value for key, value in settings_data.items()
        }

        if not normalized_settings:
            self.stdout.write("No constance settings to import after normalization.")
            return

        try:
            serializer = ConstanceSettingsSerializer(data=normalized_settings)
            if serializer.is_valid():
                if not self.dry_run:
                    with transaction.atomic():
                        serializer.save()

                for key in normalized_settings:
                    # Count each setting as updated (constance always overwrites)
                    self.stats["constance_settings"]["updated"] += 1
                    value = normalized_settings[key]
                    # Redact sensitive values in output
                    if any(
                        s in key.lower() for s in ["password", "token", "secret", "key"]
                    ):
                        value = "<redacted>"
                    self.stdout.write(f"  Set {key} = {value}")
            else:
                # Handle validation errors - log each one but continue with valid settings
                valid_settings = {}
                for key, value in normalized_settings.items():
                    if key not in serializer.errors:
                        valid_settings[key] = value

                if valid_settings:
                    # Retry with valid settings only
                    retry_serializer = ConstanceSettingsSerializer(data=valid_settings)
                    if retry_serializer.is_valid():
                        if not self.dry_run:
                            with transaction.atomic():
                                retry_serializer.save()

                        for key in valid_settings:
                            self.stats["constance_settings"]["updated"] += 1
                            value = valid_settings[key]
                            if any(
                                s in key.lower()
                                for s in ["password", "token", "secret", "key"]
                            ):
                                value = "<redacted>"
                            self.stdout.write(f"  Set {key} = {value}")

                # Log errors for invalid settings
                for key, errors in serializer.errors.items():
                    self.stats["constance_settings"]["errors"] += 1
                    self.stdout.write(
                        self.style.WARNING(f"  Failed to set {key}: {errors}")
                    )

        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Failed to import constance settings: {e}")
            )
            self.stats["constance_settings"]["errors"] += 1

    def import_call_managing_organisations(self, cmo_data):
        """Import call managing organisation data."""
        self.stdout.write("Importing call managing organisations...")
        for org_data in cmo_data:
            try:
                uuid = org_data.get("uuid")
                customer_uuid = org_data.get("customer_uuid")

                if not uuid or not customer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping call managing organisation without UUID or customer_uuid"
                        )
                    )
                    self.stats["call_managing_organisations"]["errors"] += 1
                    continue

                customer = Customer.objects.filter(uuid=customer_uuid).first()
                if not customer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping call managing organisation {uuid}: customer {customer_uuid} not found"
                        )
                    )
                    self.stats["call_managing_organisations"]["errors"] += 1
                    continue

                # Note: 'name' is a read-only property that returns customer.name
                defaults = {
                    "customer": customer,
                    "description": org_data.get("description", ""),
                }

                if not self.dry_run:
                    existing = CallManagingOrganisation.objects.filter(
                        uuid=uuid
                    ).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                CallManagingOrganisation.objects.filter(
                                    uuid=uuid
                                ).update(**defaults)
                            self.stats["call_managing_organisations"]["updated"] += 1
                        else:
                            self.stats["call_managing_organisations"]["skipped"] += 1
                    else:
                        # Check if customer already has a CMO
                        existing_by_customer = CallManagingOrganisation.objects.filter(
                            customer=customer
                        ).first()
                        if existing_by_customer:
                            if self.update_existing:
                                with transaction.atomic():
                                    CallManagingOrganisation.objects.filter(
                                        customer=customer
                                    ).update(**defaults)
                                self.stats["call_managing_organisations"][
                                    "updated"
                                ] += 1
                            else:
                                self.stats["call_managing_organisations"][
                                    "skipped"
                                ] += 1
                        else:
                            with transaction.atomic():
                                CallManagingOrganisation.objects.create(
                                    uuid=uuid, **defaults
                                )
                            self.stats["call_managing_organisations"]["created"] += 1
                else:
                    existing = CallManagingOrganisation.objects.filter(
                        uuid=uuid
                    ).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["call_managing_organisations"]["updated"] += 1
                        else:
                            self.stats["call_managing_organisations"]["skipped"] += 1
                    else:
                        self.stats["call_managing_organisations"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import call managing organisation {org_data.get('uuid')}: {e}"
                    )
                )
                self.stats["call_managing_organisations"]["errors"] += 1

    def import_calls(self, calls_data):
        """Import call data."""
        self.stdout.write("Importing calls...")
        for call_data in calls_data:
            try:
                uuid = call_data.get("uuid")
                manager_uuid = call_data.get("manager_uuid")

                if not uuid or not manager_uuid:
                    self.stdout.write(
                        self.style.WARNING("Skipping call without UUID or manager_uuid")
                    )
                    self.stats["calls"]["errors"] += 1
                    continue

                manager = CallManagingOrganisation.objects.filter(
                    uuid=manager_uuid
                ).first()
                if not manager:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping call {uuid}: manager {manager_uuid} not found"
                        )
                    )
                    self.stats["calls"]["errors"] += 1
                    continue

                # Resolve created_by user
                created_by = None
                if call_data.get("created_by_uuid"):
                    created_by = User.objects.filter(
                        uuid=call_data["created_by_uuid"]
                    ).first()

                # Resolve compliance checklist (optional)
                compliance_checklist = None
                if call_data.get("compliance_checklist_uuid"):
                    compliance_checklist = Checklist.objects.filter(
                        uuid=call_data["compliance_checklist_uuid"]
                    ).first()

                defaults = {
                    "manager": manager,
                    "name": call_data.get("name", ""),
                    "description": call_data.get("description", ""),
                    "state": call_data.get("state", "draft"),
                    "created_by": created_by,
                    "external_url": call_data.get("external_url", ""),
                    "reviewer_identity_visible_to_submitters": call_data.get(
                        "reviewer_identity_visible_to_submitters", False
                    ),
                    "reviews_visible_to_submitters": call_data.get(
                        "reviews_visible_to_submitters", True
                    ),
                    "fixed_duration_in_days": call_data.get("fixed_duration_in_days"),
                    "compliance_checklist": compliance_checklist,
                }

                if not self.dry_run:
                    existing = Call.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                Call.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["calls"]["updated"] += 1
                        else:
                            self.stats["calls"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            Call.objects.create(uuid=uuid, **defaults)
                        self.stats["calls"]["created"] += 1
                else:
                    existing = Call.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["calls"]["updated"] += 1
                        else:
                            self.stats["calls"]["skipped"] += 1
                    else:
                        self.stats["calls"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import call {call_data.get('uuid')}: {e}"
                    )
                )
                self.stats["calls"]["errors"] += 1

    def import_requested_offerings(self, requested_offerings_data):
        """Import requested offering data."""
        self.stdout.write("Importing requested offerings...")
        for ro_data in requested_offerings_data:
            try:
                uuid = ro_data.get("uuid")
                call_uuid = ro_data.get("call_uuid")
                offering_uuid = ro_data.get("offering_uuid")

                if not uuid or not call_uuid or not offering_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping requested offering without UUID, call_uuid, or offering_uuid"
                        )
                    )
                    self.stats["requested_offerings"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping requested offering {uuid}: call {call_uuid} not found"
                        )
                    )
                    self.stats["requested_offerings"]["errors"] += 1
                    continue

                offering = Offering.objects.filter(uuid=offering_uuid).first()
                if not offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping requested offering {uuid}: offering {offering_uuid} not found"
                        )
                    )
                    self.stats["requested_offerings"]["errors"] += 1
                    continue

                # Resolve optional foreign keys
                plan = None
                if ro_data.get("plan_uuid"):
                    plan = Plan.objects.filter(uuid=ro_data["plan_uuid"]).first()

                created_by = None
                if ro_data.get("created_by_uuid"):
                    created_by = User.objects.filter(
                        uuid=ro_data["created_by_uuid"]
                    ).first()

                approved_by = None
                if ro_data.get("approved_by_uuid"):
                    approved_by = User.objects.filter(
                        uuid=ro_data["approved_by_uuid"]
                    ).first()

                defaults = {
                    "call": call,
                    "offering": offering,
                    "plan": plan,
                    "state": ro_data.get("state", "requested"),
                    "created_by": created_by,
                    "approved_by": approved_by,
                    "description": ro_data.get("description", ""),
                }

                if not self.dry_run:
                    existing = RequestedOffering.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                RequestedOffering.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["requested_offerings"]["updated"] += 1
                        else:
                            self.stats["requested_offerings"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            RequestedOffering.objects.create(uuid=uuid, **defaults)
                        self.stats["requested_offerings"]["created"] += 1
                else:
                    existing = RequestedOffering.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["requested_offerings"]["updated"] += 1
                        else:
                            self.stats["requested_offerings"]["skipped"] += 1
                    else:
                        self.stats["requested_offerings"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import requested offering {ro_data.get('uuid')}: {e}"
                    )
                )
                self.stats["requested_offerings"]["errors"] += 1

    def import_call_resource_templates(self, templates_data):
        """Import call resource template data."""
        self.stdout.write("Importing call resource templates...")
        for template_data in templates_data:
            try:
                uuid = template_data.get("uuid")
                call_uuid = template_data.get("call_uuid")
                requested_offering_uuid = template_data.get("requested_offering_uuid")

                if not uuid or not call_uuid or not requested_offering_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping call resource template without UUID, call_uuid, or requested_offering_uuid"
                        )
                    )
                    self.stats["call_resource_templates"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping call resource template {uuid}: call {call_uuid} not found"
                        )
                    )
                    self.stats["call_resource_templates"]["errors"] += 1
                    continue

                requested_offering = RequestedOffering.objects.filter(
                    uuid=requested_offering_uuid
                ).first()
                if not requested_offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping call resource template {uuid}: requested offering {requested_offering_uuid} not found"
                        )
                    )
                    self.stats["call_resource_templates"]["errors"] += 1
                    continue

                created_by = None
                if template_data.get("created_by_uuid"):
                    created_by = User.objects.filter(
                        uuid=template_data["created_by_uuid"]
                    ).first()

                defaults = {
                    "call": call,
                    "requested_offering": requested_offering,
                    "name": template_data.get("name", ""),
                    "description": template_data.get("description", ""),
                    "attributes": template_data.get("attributes", {}),
                    "limits": template_data.get("limits", {}),
                    "is_required": template_data.get("is_required", False),
                    "created_by": created_by,
                }

                if not self.dry_run:
                    existing = CallResourceTemplate.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                CallResourceTemplate.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["call_resource_templates"]["updated"] += 1
                        else:
                            self.stats["call_resource_templates"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            CallResourceTemplate.objects.create(uuid=uuid, **defaults)
                        self.stats["call_resource_templates"]["created"] += 1
                else:
                    existing = CallResourceTemplate.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["call_resource_templates"]["updated"] += 1
                        else:
                            self.stats["call_resource_templates"]["skipped"] += 1
                    else:
                        self.stats["call_resource_templates"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import call resource template {template_data.get('uuid')}: {e}"
                    )
                )
                self.stats["call_resource_templates"]["errors"] += 1

    def import_rounds(self, rounds_data):
        """Import round data."""
        self.stdout.write("Importing rounds...")
        for round_data in rounds_data:
            try:
                uuid = round_data.get("uuid")
                call_uuid = round_data.get("call_uuid")

                if not uuid or not call_uuid:
                    self.stdout.write(
                        self.style.WARNING("Skipping round without UUID or call_uuid")
                    )
                    self.stats["rounds"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping round {uuid}: call {call_uuid} not found"
                        )
                    )
                    self.stats["rounds"]["errors"] += 1
                    continue

                # Parse datetime fields (supports ISO format and relative offsets)
                start_time = self._parse_datetime(round_data.get("start_time"))
                cutoff_time = self._parse_datetime(round_data.get("cutoff_time"))
                allocation_date = self._parse_datetime(
                    round_data.get("allocation_date")
                )

                if not start_time or not cutoff_time:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping round {uuid}: missing start_time or cutoff_time"
                        )
                    )
                    self.stats["rounds"]["errors"] += 1
                    continue

                defaults = {
                    "call": call,
                    "start_time": start_time,
                    "cutoff_time": cutoff_time,
                    "review_duration_in_days": round_data.get(
                        "review_duration_in_days"
                    ),
                    "allocation_date": allocation_date,
                }

                if not self.dry_run:
                    existing = Round.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                Round.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["rounds"]["updated"] += 1
                        else:
                            self.stats["rounds"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            Round.objects.create(uuid=uuid, **defaults)
                        self.stats["rounds"]["created"] += 1
                else:
                    existing = Round.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["rounds"]["updated"] += 1
                        else:
                            self.stats["rounds"]["skipped"] += 1
                    else:
                        self.stats["rounds"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import round {round_data.get('uuid')}: {e}"
                    )
                )
                self.stats["rounds"]["errors"] += 1

    def import_proposals(self, proposals_data):
        """Import proposal data."""
        self.stdout.write("Importing proposals...")
        for proposal_data in proposals_data:
            try:
                uuid = proposal_data.get("uuid")
                round_uuid = proposal_data.get("round_uuid")

                if not uuid or not round_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping proposal without UUID or round_uuid"
                        )
                    )
                    self.stats["proposals"]["errors"] += 1
                    continue

                round_obj = Round.objects.filter(uuid=round_uuid).first()
                if not round_obj:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping proposal {uuid}: round {round_uuid} not found"
                        )
                    )
                    self.stats["proposals"]["errors"] += 1
                    continue

                # Resolve optional foreign keys
                project = None
                if proposal_data.get("project_uuid"):
                    project = Project.objects.filter(
                        uuid=proposal_data["project_uuid"]
                    ).first()

                created_by = None
                if proposal_data.get("created_by_uuid"):
                    created_by = User.objects.filter(
                        uuid=proposal_data["created_by_uuid"]
                    ).first()

                approved_by = None
                if proposal_data.get("approved_by_uuid"):
                    approved_by = User.objects.filter(
                        uuid=proposal_data["approved_by_uuid"]
                    ).first()

                defaults = {
                    "round": round_obj,
                    "name": proposal_data.get("name", ""),
                    "description": proposal_data.get("description", ""),
                    "state": proposal_data.get("state", "draft"),
                    "project": project,
                    "created_by": created_by,
                    "approved_by": approved_by,
                    "duration_in_days": proposal_data.get("duration_in_days"),
                    "project_summary": proposal_data.get("project_summary", ""),
                    "project_duration": proposal_data.get("project_duration"),
                    "project_is_confidential": proposal_data.get(
                        "project_is_confidential", False
                    ),
                    "project_has_civilian_purpose": proposal_data.get(
                        "project_has_civilian_purpose", False
                    ),
                    "allocation_comment": proposal_data.get("allocation_comment", ""),
                }

                if not self.dry_run:
                    existing = Proposal.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                Proposal.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["proposals"]["updated"] += 1
                        else:
                            self.stats["proposals"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            Proposal.objects.create(uuid=uuid, **defaults)
                        self.stats["proposals"]["created"] += 1
                else:
                    existing = Proposal.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["proposals"]["updated"] += 1
                        else:
                            self.stats["proposals"]["skipped"] += 1
                    else:
                        self.stats["proposals"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import proposal {proposal_data.get('uuid')}: {e}"
                    )
                )
                self.stats["proposals"]["errors"] += 1

    def import_requested_resources(self, requested_resources_data):
        """Import requested resource data."""
        self.stdout.write("Importing requested resources...")
        for rr_data in requested_resources_data:
            try:
                uuid = rr_data.get("uuid")
                proposal_uuid = rr_data.get("proposal_uuid")
                requested_offering_uuid = rr_data.get("requested_offering_uuid")

                if not uuid or not proposal_uuid or not requested_offering_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping requested resource without UUID, proposal_uuid, or requested_offering_uuid"
                        )
                    )
                    self.stats["requested_resources"]["errors"] += 1
                    continue

                proposal = Proposal.objects.filter(uuid=proposal_uuid).first()
                if not proposal:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping requested resource {uuid}: proposal {proposal_uuid} not found"
                        )
                    )
                    self.stats["requested_resources"]["errors"] += 1
                    continue

                requested_offering = RequestedOffering.objects.filter(
                    uuid=requested_offering_uuid
                ).first()
                if not requested_offering:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping requested resource {uuid}: requested offering {requested_offering_uuid} not found"
                        )
                    )
                    self.stats["requested_resources"]["errors"] += 1
                    continue

                # Resolve optional foreign keys
                call_resource_template = None
                if rr_data.get("call_resource_template_uuid"):
                    call_resource_template = CallResourceTemplate.objects.filter(
                        uuid=rr_data["call_resource_template_uuid"]
                    ).first()

                created_by = None
                if rr_data.get("created_by_uuid"):
                    created_by = User.objects.filter(
                        uuid=rr_data["created_by_uuid"]
                    ).first()

                resource = None
                if rr_data.get("resource_uuid"):
                    resource = Resource.objects.filter(
                        uuid=rr_data["resource_uuid"]
                    ).first()

                defaults = {
                    "proposal": proposal,
                    "requested_offering": requested_offering,
                    "call_resource_template": call_resource_template,
                    "created_by": created_by,
                    "resource": resource,
                    "description": rr_data.get("description", ""),
                    "attributes": rr_data.get("attributes", {}),
                    "limits": rr_data.get("limits", {}),
                }

                if not self.dry_run:
                    existing = RequestedResource.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                RequestedResource.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["requested_resources"]["updated"] += 1
                        else:
                            self.stats["requested_resources"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            RequestedResource.objects.create(uuid=uuid, **defaults)
                        self.stats["requested_resources"]["created"] += 1
                else:
                    existing = RequestedResource.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["requested_resources"]["updated"] += 1
                        else:
                            self.stats["requested_resources"]["skipped"] += 1
                    else:
                        self.stats["requested_resources"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import requested resource {rr_data.get('uuid')}: {e}"
                    )
                )
                self.stats["requested_resources"]["errors"] += 1

    def import_call_documents(self, documents_data):
        """Import documentation files attached to calls, seeding a placeholder
        file so the document (and the public-call Documents tab) is shown."""
        from django.core.files.base import ContentFile

        self.stdout.write("Importing call documents...")
        for item in documents_data:
            try:
                uuid = item.get("uuid")
                call_uuid = item.get("call_uuid")
                if not uuid or not call_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping call document without UUID or call_uuid"
                        )
                    )
                    self.stats["call_documents"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping call document {uuid}: call {call_uuid} not found"
                        )
                    )
                    self.stats["call_documents"]["errors"] += 1
                    continue

                document, created = CallDocument.objects.update_or_create(
                    uuid=uuid,
                    defaults={
                        "call": call,
                        "description": item.get("description", ""),
                    },
                )
                if not document.file:
                    content = item.get(
                        "content",
                        f"{item.get('description') or 'Call document'}\n\n"
                        "Sample document for this call for proposals.\n",
                    ).encode()
                    document.file.save(
                        item.get("file_name", "document.txt"),
                        ContentFile(content),
                        save=True,
                    )
                # The call exposes documents via the M2M relation (what the
                # serializer/UI read), in addition to the CallDocument.call FK.
                call.documents.add(document)

                self.stats["call_documents"]["created" if created else "updated"] += 1
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"Error importing call document: {e}")
                )
                self.stats["call_documents"]["errors"] += 1

    def import_reviews(self, reviews_data):
        """Import review data."""
        self.stdout.write("Importing reviews...")
        for review_data in reviews_data:
            try:
                uuid = review_data.get("uuid")
                proposal_uuid = review_data.get("proposal_uuid")
                reviewer_uuid = review_data.get("reviewer_uuid")

                if not uuid or not proposal_uuid or not reviewer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping review without UUID, proposal_uuid, or reviewer_uuid"
                        )
                    )
                    self.stats["reviews"]["errors"] += 1
                    continue

                proposal = Proposal.objects.filter(uuid=proposal_uuid).first()
                if not proposal:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping review {uuid}: proposal {proposal_uuid} not found"
                        )
                    )
                    self.stats["reviews"]["errors"] += 1
                    continue

                reviewer = User.objects.filter(uuid=reviewer_uuid).first()
                if not reviewer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping review {uuid}: reviewer {reviewer_uuid} not found"
                        )
                    )
                    self.stats["reviews"]["errors"] += 1
                    continue

                defaults = {
                    "proposal": proposal,
                    "reviewer": reviewer,
                    "state": review_data.get("state", "created"),
                    "summary_score": review_data.get("summary_score", 0),
                    "summary_public_comment": review_data.get(
                        "summary_public_comment", ""
                    ),
                    "summary_private_comment": review_data.get(
                        "summary_private_comment", ""
                    ),
                    "comment_project_title": review_data.get("comment_project_title"),
                    "comment_project_summary": review_data.get(
                        "comment_project_summary"
                    ),
                    "comment_project_description": review_data.get(
                        "comment_project_description"
                    ),
                    "comment_project_duration": review_data.get(
                        "comment_project_duration"
                    ),
                    "comment_project_is_confidential": review_data.get(
                        "comment_project_is_confidential"
                    ),
                    "comment_project_has_civilian_purpose": review_data.get(
                        "comment_project_has_civilian_purpose"
                    ),
                    "comment_project_supporting_documentation": review_data.get(
                        "comment_project_supporting_documentation"
                    ),
                    "comment_resource_requests": review_data.get(
                        "comment_resource_requests"
                    ),
                    "comment_team": review_data.get("comment_team"),
                }

                if not self.dry_run:
                    existing = Review.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                Review.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["reviews"]["updated"] += 1
                        else:
                            self.stats["reviews"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            Review.objects.create(uuid=uuid, **defaults)
                        self.stats["reviews"]["created"] += 1
                else:
                    existing = Review.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["reviews"]["updated"] += 1
                        else:
                            self.stats["reviews"]["skipped"] += 1
                    else:
                        self.stats["reviews"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import review {review_data.get('uuid')}: {e}"
                    )
                )
                self.stats["reviews"]["errors"] += 1

    def import_call_workflow_steps(self, steps_data):
        """Configure per-call workflow steps.

        Every call auto-seeds a full set of ``CallWorkflowStep`` rows via a
        post-save signal on creation, so the rows this configures already
        exist; matched by ``(call, step)`` it overrides their enable /
        transition / review settings for calls that want a non-default
        workflow (e.g. enabling the review steps). A missing ``(call, step)``
        (such as ``award_response``, which the signal skips) is created.
        The configuration is always applied — unlike most entities these are
        never "skipped" on re-import, since the point is to override defaults.
        """
        self.stdout.write("Importing call workflow steps...")
        config_fields = (
            "is_enabled",
            "duration_in_days",
            "blind_review",
            "requires_coi_confirmation",
            "checklist_required",
            "min_reviewers",
            "min_score_threshold",
            "applicant_visible",
            "responsible_role",
            "transition_mode",
            "display_order",
            "include_award_response",
            "allocation_time",
        )
        for step_data in steps_data:
            try:
                call_uuid = step_data.get("call_uuid")
                step = step_data.get("step")
                if not call_uuid or not step:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping call workflow step without call_uuid or step"
                        )
                    )
                    self.stats["call_workflow_steps"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping call workflow step {step}: call {call_uuid} not found"
                        )
                    )
                    self.stats["call_workflow_steps"]["errors"] += 1
                    continue

                defaults = {
                    field: step_data[field]
                    for field in config_fields
                    if field in step_data
                }

                # Resolve the optional workflow-step checklist (a WORKFLOW_STEP
                # checklist the responsible role fills in during the step).
                checklist_uuid = step_data.get("checklist_uuid")
                if checklist_uuid:
                    checklist = Checklist.objects.filter(uuid=checklist_uuid).first()
                    if checklist:
                        defaults["checklist"] = checklist
                    else:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Call workflow step {step}: "
                                f"checklist {checklist_uuid} not found"
                            )
                        )

                if self.dry_run:
                    exists = CallWorkflowStep.objects.filter(
                        call=call, step=step
                    ).exists()
                    self.stats["call_workflow_steps"][
                        "updated" if exists else "created"
                    ] += 1
                    continue

                with transaction.atomic():
                    _, created = CallWorkflowStep.objects.update_or_create(
                        call=call, step=step, defaults=defaults
                    )
                self.stats["call_workflow_steps"][
                    "created" if created else "updated"
                ] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import call workflow step {step_data.get('step')}: {e}"
                    )
                )
                self.stats["call_workflow_steps"]["errors"] += 1

    def import_proposal_workflow_step_instances(self, instances_data):
        """Seed per-proposal workflow step instances (engine state).

        These are normally created only by the ``submit`` action at runtime, so
        preset proposals — imported directly into their target state — have
        none, leaving the workflow engine invisible. This mirrors the shape
        ``submit`` / ``workflow_service`` produce: exactly one ``active``
        instance per proposal (enforced by a DB constraint), ``pending`` for
        enabled-but-not-yet-reached steps, ``skipped`` for disabled steps, and
        ``completed`` (with an ``outcome``) for finished ones. When an instance
        is ``active`` the parent proposal's ``workflow_step`` is set to match.
        """
        self.stdout.write("Importing proposal workflow step instances...")
        for inst in instances_data:
            try:
                uuid = inst.get("uuid")
                proposal_uuid = inst.get("proposal_uuid")
                step = inst.get("step")
                if not uuid or not proposal_uuid or not step:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping workflow step instance without uuid, proposal_uuid, or step"
                        )
                    )
                    self.stats["proposal_workflow_step_instances"]["errors"] += 1
                    continue

                proposal = Proposal.objects.filter(uuid=proposal_uuid).first()
                if not proposal:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping workflow step instance {uuid}: proposal {proposal_uuid} not found"
                        )
                    )
                    self.stats["proposal_workflow_step_instances"]["errors"] += 1
                    continue

                completed_by = None
                if inst.get("completed_by_uuid"):
                    completed_by = User.objects.filter(
                        uuid=inst["completed_by_uuid"]
                    ).first()

                status = inst.get("status", "pending")
                defaults = {
                    "proposal": proposal,
                    "step": step,
                    "status": status,
                    "outcome": inst.get("outcome"),
                    "outcome_reason": inst.get("outcome_reason", ""),
                    "started_at": self._parse_datetime(inst.get("started_at")),
                    "completed_at": self._parse_datetime(inst.get("completed_at")),
                    "completed_by": completed_by,
                    "internal_notes": inst.get("internal_notes", ""),
                }

                if not self.dry_run:
                    existing = ProposalWorkflowStepInstance.objects.filter(
                        uuid=uuid
                    ).first()
                    if existing and not self.update_existing:
                        self.stats["proposal_workflow_step_instances"]["skipped"] += 1
                        continue
                    with transaction.atomic():
                        if existing:
                            ProposalWorkflowStepInstance.objects.filter(
                                uuid=uuid
                            ).update(**defaults)
                            self.stats["proposal_workflow_step_instances"][
                                "updated"
                            ] += 1
                        else:
                            ProposalWorkflowStepInstance.objects.create(
                                uuid=uuid, **defaults
                            )
                            self.stats["proposal_workflow_step_instances"][
                                "created"
                            ] += 1
                        # Keep the proposal's pointer consistent with its
                        # active step (the submit/advance actions do this).
                        if status == "active" and proposal.workflow_step != step:
                            proposal.workflow_step = step
                            proposal.save(update_fields=["workflow_step"])
                else:
                    existing = ProposalWorkflowStepInstance.objects.filter(
                        uuid=uuid
                    ).exists()
                    if existing and not self.update_existing:
                        self.stats["proposal_workflow_step_instances"]["skipped"] += 1
                    else:
                        self.stats["proposal_workflow_step_instances"][
                            "updated" if existing else "created"
                        ] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import workflow step instance {inst.get('uuid')}: {e}"
                    )
                )
                self.stats["proposal_workflow_step_instances"]["errors"] += 1

    def import_user_agreements(self, user_agreements_data):
        """Import user agreement data (Terms of Service, Privacy Policy)."""
        self.stdout.write("Importing user agreements...")

        for agreement_data in user_agreements_data:
            try:
                uuid = agreement_data.get("uuid")
                agreement_type = agreement_data.get("agreement_type")

                if not uuid or not agreement_type:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping user agreement without UUID or agreement_type"
                        )
                    )
                    self.stats["user_agreements"]["errors"] += 1
                    continue

                # Validate agreement_type
                valid_types = [
                    choice[0] for choice in UserAgreement.UserAgreements.CHOICES
                ]
                if agreement_type not in valid_types:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping user agreement {uuid}: invalid agreement_type '{agreement_type}'"
                        )
                    )
                    self.stats["user_agreements"]["errors"] += 1
                    continue

                defaults = {
                    "content": agreement_data.get("content", ""),
                    "agreement_type": agreement_type,
                }

                if not self.dry_run:
                    # Check if agreement with this type already exists
                    existing_by_type = UserAgreement.objects.filter(
                        agreement_type=agreement_type
                    ).first()
                    existing_by_uuid = UserAgreement.objects.filter(uuid=uuid).first()

                    if existing_by_uuid:
                        if self.update_existing:
                            with transaction.atomic():
                                UserAgreement.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["user_agreements"]["updated"] += 1
                        else:
                            self.stats["user_agreements"]["skipped"] += 1
                    elif existing_by_type:
                        # Agreement type already exists with different UUID
                        if self.update_existing:
                            with transaction.atomic():
                                UserAgreement.objects.filter(
                                    agreement_type=agreement_type
                                ).update(**defaults)
                            self.stats["user_agreements"]["updated"] += 1
                        else:
                            self.stats["user_agreements"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            UserAgreement.objects.create(uuid=uuid, **defaults)
                        self.stats["user_agreements"]["created"] += 1
                else:
                    existing = UserAgreement.objects.filter(uuid=uuid).exists()
                    existing_type = UserAgreement.objects.filter(
                        agreement_type=agreement_type
                    ).exists()
                    if existing or existing_type:
                        if self.update_existing:
                            self.stats["user_agreements"]["updated"] += 1
                        else:
                            self.stats["user_agreements"]["skipped"] += 1
                    else:
                        self.stats["user_agreements"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import user agreement {agreement_data.get('uuid')}: {e}"
                    )
                )
                self.stats["user_agreements"]["errors"] += 1

    def import_expertise_categories(self, categories_data):
        """Import expertise category data."""
        self.stdout.write("Importing expertise categories...")
        for cat_data in categories_data:
            try:
                uuid = cat_data.get("uuid")
                code = cat_data.get("code")

                if not uuid or not code:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping expertise category without UUID or code"
                        )
                    )
                    self.stats["expertise_categories"]["errors"] += 1
                    continue

                parent_uuid = cat_data.get("parent_uuid")
                parent = None
                if parent_uuid:
                    parent = ExpertiseCategory.objects.filter(uuid=parent_uuid).first()

                defaults = {
                    "name": cat_data.get("name", ""),
                    "description": cat_data.get("description", ""),
                    "code": code,
                    "parent": parent,
                    "level": cat_data.get("level", 0),
                }

                if not self.dry_run:
                    existing = ExpertiseCategory.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ExpertiseCategory.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["expertise_categories"]["updated"] += 1
                        else:
                            self.stats["expertise_categories"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ExpertiseCategory.objects.create(uuid=uuid, **defaults)
                        self.stats["expertise_categories"]["created"] += 1
                else:
                    existing = ExpertiseCategory.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["expertise_categories"]["updated"] += 1
                        else:
                            self.stats["expertise_categories"]["skipped"] += 1
                    else:
                        self.stats["expertise_categories"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import expertise category {cat_data.get('uuid')}: {e}"
                    )
                )
                self.stats["expertise_categories"]["errors"] += 1

    def import_reviewer_profiles(self, profiles_data):
        """Import reviewer profile data."""
        self.stdout.write("Importing reviewer profiles...")
        for profile_data in profiles_data:
            try:
                uuid = profile_data.get("uuid")
                user_uuid = profile_data.get("user_uuid")

                if not uuid or not user_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping reviewer profile without UUID or user_uuid"
                        )
                    )
                    self.stats["reviewer_profiles"]["errors"] += 1
                    continue

                user = User.objects.filter(uuid=user_uuid).first()
                if not user:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping reviewer profile {uuid}: user {user_uuid} not found"
                        )
                    )
                    self.stats["reviewer_profiles"]["errors"] += 1
                    continue

                defaults = {
                    "user": user,
                    "orcid_id": profile_data.get("orcid_id"),
                    "biography": profile_data.get("biography", ""),
                    "alternative_names": profile_data.get("alternative_names", []),
                    "description": profile_data.get("description", ""),
                    "is_published": profile_data.get("is_published", False),
                    "available_for_reviews": profile_data.get(
                        "available_for_reviews", True
                    ),
                }

                # Parse published_at if provided
                published_at = profile_data.get("published_at")
                if published_at:
                    defaults["published_at"] = self._parse_datetime(published_at)

                if not self.dry_run:
                    existing = ReviewerProfile.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ReviewerProfile.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["reviewer_profiles"]["updated"] += 1
                        else:
                            self.stats["reviewer_profiles"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ReviewerProfile.objects.create(uuid=uuid, **defaults)
                        self.stats["reviewer_profiles"]["created"] += 1
                else:
                    existing = ReviewerProfile.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["reviewer_profiles"]["updated"] += 1
                        else:
                            self.stats["reviewer_profiles"]["skipped"] += 1
                    else:
                        self.stats["reviewer_profiles"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import reviewer profile {profile_data.get('uuid')}: {e}"
                    )
                )
                self.stats["reviewer_profiles"]["errors"] += 1

    def import_reviewer_affiliations(self, affiliations_data):
        """Import reviewer affiliation data."""
        self.stdout.write("Importing reviewer affiliations...")
        for aff_data in affiliations_data:
            try:
                uuid = aff_data.get("uuid")
                reviewer_uuid = aff_data.get("reviewer_profile_uuid")

                if not uuid or not reviewer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping reviewer affiliation without UUID or reviewer_profile_uuid"
                        )
                    )
                    self.stats["reviewer_affiliations"]["errors"] += 1
                    continue

                reviewer = ReviewerProfile.objects.filter(uuid=reviewer_uuid).first()
                if not reviewer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping affiliation {uuid}: reviewer {reviewer_uuid} not found"
                        )
                    )
                    self.stats["reviewer_affiliations"]["errors"] += 1
                    continue

                organization = None
                org_uuid = aff_data.get("organization_uuid")
                if org_uuid:
                    organization = Customer.objects.filter(uuid=org_uuid).first()

                defaults = {
                    "reviewer_profile": reviewer,
                    "organization": organization,
                    "organization_name": aff_data.get("organization_name", ""),
                    "organization_identifier": aff_data.get(
                        "organization_identifier", ""
                    ),
                    "department": aff_data.get("department", ""),
                    "position_title": aff_data.get("position_title", ""),
                    "start_date": aff_data.get("start_date"),
                    "end_date": aff_data.get("end_date"),
                    "is_primary": aff_data.get("is_primary", False),
                    "affiliation_type": aff_data.get("affiliation_type", "employment"),
                }

                if not self.dry_run:
                    existing = ReviewerAffiliation.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ReviewerAffiliation.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["reviewer_affiliations"]["updated"] += 1
                        else:
                            self.stats["reviewer_affiliations"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ReviewerAffiliation.objects.create(uuid=uuid, **defaults)
                        self.stats["reviewer_affiliations"]["created"] += 1
                else:
                    existing = ReviewerAffiliation.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["reviewer_affiliations"]["updated"] += 1
                        else:
                            self.stats["reviewer_affiliations"]["skipped"] += 1
                    else:
                        self.stats["reviewer_affiliations"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import reviewer affiliation {aff_data.get('uuid')}: {e}"
                    )
                )
                self.stats["reviewer_affiliations"]["errors"] += 1

    def import_reviewer_expertise(self, expertise_data):
        """Import reviewer expertise data."""
        self.stdout.write("Importing reviewer expertise...")
        for exp_data in expertise_data:
            try:
                uuid = exp_data.get("uuid")
                reviewer_uuid = exp_data.get("reviewer_profile_uuid")

                if not uuid or not reviewer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping reviewer expertise without UUID or reviewer_profile_uuid"
                        )
                    )
                    self.stats["reviewer_expertise"]["errors"] += 1
                    continue

                reviewer = ReviewerProfile.objects.filter(uuid=reviewer_uuid).first()
                if not reviewer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping expertise {uuid}: reviewer {reviewer_uuid} not found"
                        )
                    )
                    self.stats["reviewer_expertise"]["errors"] += 1
                    continue

                category = None
                cat_uuid = exp_data.get("expertise_category_uuid")
                if cat_uuid:
                    category = ExpertiseCategory.objects.filter(uuid=cat_uuid).first()

                defaults = {
                    "reviewer_profile": reviewer,
                    "expertise_keyword": exp_data.get("expertise_keyword", ""),
                    "expertise_category": category,
                    "proficiency_level": exp_data.get("proficiency_level", "familiar"),
                    "years_experience": exp_data.get("years_experience"),
                    "last_active_date": exp_data.get("last_active_date"),
                }

                if not self.dry_run:
                    existing = ReviewerExpertise.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ReviewerExpertise.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["reviewer_expertise"]["updated"] += 1
                        else:
                            self.stats["reviewer_expertise"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ReviewerExpertise.objects.create(uuid=uuid, **defaults)
                        self.stats["reviewer_expertise"]["created"] += 1
                else:
                    existing = ReviewerExpertise.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["reviewer_expertise"]["updated"] += 1
                        else:
                            self.stats["reviewer_expertise"]["skipped"] += 1
                    else:
                        self.stats["reviewer_expertise"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import reviewer expertise {exp_data.get('uuid')}: {e}"
                    )
                )
                self.stats["reviewer_expertise"]["errors"] += 1

    def import_reviewer_publications(self, publications_data):
        """Import reviewer publication data."""
        self.stdout.write("Importing reviewer publications...")
        for pub_data in publications_data:
            try:
                uuid = pub_data.get("uuid")
                reviewer_uuid = pub_data.get("reviewer_profile_uuid")

                if not uuid or not reviewer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping reviewer publication without UUID or reviewer_profile_uuid"
                        )
                    )
                    self.stats["reviewer_publications"]["errors"] += 1
                    continue

                reviewer = ReviewerProfile.objects.filter(uuid=reviewer_uuid).first()
                if not reviewer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping publication {uuid}: reviewer {reviewer_uuid} not found"
                        )
                    )
                    self.stats["reviewer_publications"]["errors"] += 1
                    continue

                defaults = {
                    "reviewer_profile": reviewer,
                    "title": pub_data.get("title", ""),
                    "doi": pub_data.get("doi"),
                    "publication_year": pub_data.get("publication_year", 2024),
                    "venue": pub_data.get("venue", ""),
                    "venue_type": pub_data.get("venue_type", "journal"),
                    "abstract": pub_data.get("abstract", ""),
                    "coauthors": pub_data.get("coauthors", []),
                    "external_ids": pub_data.get("external_ids", {}),
                    "is_excluded_from_matching": pub_data.get(
                        "is_excluded_from_matching", False
                    ),
                }

                if not self.dry_run:
                    existing = ReviewerPublication.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ReviewerPublication.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["reviewer_publications"]["updated"] += 1
                        else:
                            self.stats["reviewer_publications"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ReviewerPublication.objects.create(uuid=uuid, **defaults)
                        self.stats["reviewer_publications"]["created"] += 1
                else:
                    existing = ReviewerPublication.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["reviewer_publications"]["updated"] += 1
                        else:
                            self.stats["reviewer_publications"]["skipped"] += 1
                    else:
                        self.stats["reviewer_publications"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import reviewer publication {pub_data.get('uuid')}: {e}"
                    )
                )
                self.stats["reviewer_publications"]["errors"] += 1

    def import_reviewer_stats(self, stats_data):
        """Import reviewer stats data."""
        self.stdout.write("Importing reviewer stats...")
        for stat_data in stats_data:
            try:
                uuid = stat_data.get("uuid")
                reviewer_uuid = stat_data.get("reviewer_profile_uuid")

                if not uuid or not reviewer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping reviewer stats without UUID or reviewer_profile_uuid"
                        )
                    )
                    self.stats["reviewer_stats"]["errors"] += 1
                    continue

                reviewer = ReviewerProfile.objects.filter(uuid=reviewer_uuid).first()
                if not reviewer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping stats {uuid}: reviewer {reviewer_uuid} not found"
                        )
                    )
                    self.stats["reviewer_stats"]["errors"] += 1
                    continue

                defaults = {
                    "reviewer_profile": reviewer,
                    "total_reviews_completed": stat_data.get(
                        "total_reviews_completed", 0
                    ),
                    "total_reviews_declined": stat_data.get(
                        "total_reviews_declined", 0
                    ),
                    "total_reviews_timeout": stat_data.get("total_reviews_timeout", 0),
                    "average_review_time_days": stat_data.get(
                        "average_review_time_days"
                    ),
                    "average_score_given": stat_data.get("average_score_given"),
                    "last_review_date": stat_data.get("last_review_date"),
                    "quality_rating": stat_data.get("quality_rating"),
                    "quality_rating_count": stat_data.get("quality_rating_count", 0),
                }

                if not self.dry_run:
                    existing = ReviewerStats.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ReviewerStats.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["reviewer_stats"]["updated"] += 1
                        else:
                            self.stats["reviewer_stats"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ReviewerStats.objects.create(uuid=uuid, **defaults)
                        self.stats["reviewer_stats"]["created"] += 1
                else:
                    existing = ReviewerStats.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["reviewer_stats"]["updated"] += 1
                        else:
                            self.stats["reviewer_stats"]["skipped"] += 1
                    else:
                        self.stats["reviewer_stats"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import reviewer stats {stat_data.get('uuid')}: {e}"
                    )
                )
                self.stats["reviewer_stats"]["errors"] += 1

    def import_call_coi_configurations(self, configs_data):
        """Import call COI configuration data."""
        self.stdout.write("Importing call COI configurations...")
        for config_data in configs_data:
            try:
                uuid = config_data.get("uuid")
                call_uuid = config_data.get("call_uuid")

                if not uuid or not call_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping call COI config without UUID or call_uuid"
                        )
                    )
                    self.stats["call_coi_configurations"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping COI config {uuid}: call {call_uuid} not found"
                        )
                    )
                    self.stats["call_coi_configurations"]["errors"] += 1
                    continue

                # This path writes the type-handling rules straight to the ORM,
                # so the serializer's checks never run. Apply the model's own
                # invariant here too, otherwise an imported preset can produce a
                # configuration the API would have rejected.
                rules = {
                    field: config_data.get(field, [])
                    for field in CallCOIConfiguration.RULE_FIELDS
                }
                problems = []
                unknown = CallCOIConfiguration.find_unknown_types(rules)
                if unknown:
                    listed = "; ".join(
                        f"{field}: {', '.join(types)}"
                        for field, types in sorted(unknown.items())
                    )
                    problems.append(f"unknown conflict types ({listed})")
                overlaps = CallCOIConfiguration.find_rule_overlaps(rules)
                if overlaps:
                    listed = "; ".join(
                        f"{coi_type} in {', '.join(sorted(fields))}"
                        for coi_type, fields in sorted(overlaps.items())
                    )
                    problems.append(
                        f"each conflict type may only be assigned to one rule ({listed})"
                    )
                if problems:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping COI config {uuid}: {'; '.join(problems)}"
                        )
                    )
                    self.stats["call_coi_configurations"]["errors"] += 1
                    continue

                defaults = {
                    "call": call,
                    **rules,
                    "coauthorship_lookback_years": config_data.get(
                        "coauthorship_lookback_years", 3
                    ),
                    "coauthorship_threshold_papers": config_data.get(
                        "coauthorship_threshold_papers", 1
                    ),
                    "institutional_lookback_years": config_data.get(
                        "institutional_lookback_years", 2
                    ),
                    "include_same_department": config_data.get(
                        "include_same_department", True
                    ),
                    "include_same_institution": config_data.get(
                        "include_same_institution", True
                    ),
                    "auto_detect_coauthorship": config_data.get(
                        "auto_detect_coauthorship", True
                    ),
                    "auto_detect_institutional": config_data.get(
                        "auto_detect_institutional", True
                    ),
                    "auto_detect_named_personnel": config_data.get(
                        "auto_detect_named_personnel", True
                    ),
                }

                if not self.dry_run:
                    existing = CallCOIConfiguration.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                CallCOIConfiguration.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["call_coi_configurations"]["updated"] += 1
                        else:
                            self.stats["call_coi_configurations"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            CallCOIConfiguration.objects.create(uuid=uuid, **defaults)
                        self.stats["call_coi_configurations"]["created"] += 1
                else:
                    existing = CallCOIConfiguration.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["call_coi_configurations"]["updated"] += 1
                        else:
                            self.stats["call_coi_configurations"]["skipped"] += 1
                    else:
                        self.stats["call_coi_configurations"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import call COI config {config_data.get('uuid')}: {e}"
                    )
                )
                self.stats["call_coi_configurations"]["errors"] += 1

    def import_matching_configurations(self, configs_data):
        """Import matching configuration data."""
        self.stdout.write("Importing matching configurations...")
        for config_data in configs_data:
            try:
                uuid = config_data.get("uuid")
                call_uuid = config_data.get("call_uuid")

                if not uuid or not call_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping matching config without UUID or call_uuid"
                        )
                    )
                    self.stats["matching_configurations"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping matching config {uuid}: call {call_uuid} not found"
                        )
                    )
                    self.stats["matching_configurations"]["errors"] += 1
                    continue

                defaults = {
                    "call": call,
                    "affinity_method": config_data.get("affinity_method", "combined"),
                    "keyword_weight": config_data.get("keyword_weight", 0.4),
                    "text_weight": config_data.get("text_weight", 0.6),
                    "min_reviewers_per_proposal": config_data.get(
                        "min_reviewers_per_proposal", 3
                    ),
                    "max_reviewers_per_proposal": config_data.get(
                        "max_reviewers_per_proposal", 5
                    ),
                    "min_proposals_per_reviewer": config_data.get(
                        "min_proposals_per_reviewer", 3
                    ),
                    "max_proposals_per_reviewer": config_data.get(
                        "max_proposals_per_reviewer", 10
                    ),
                    "algorithm": config_data.get("algorithm", "minmax"),
                    "min_affinity_threshold": config_data.get(
                        "min_affinity_threshold", 0.1
                    ),
                    "use_reviewer_bids": config_data.get("use_reviewer_bids", True),
                    "bid_weight": config_data.get("bid_weight", 0.3),
                }

                if not self.dry_run:
                    existing = MatchingConfiguration.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                MatchingConfiguration.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["matching_configurations"]["updated"] += 1
                        else:
                            self.stats["matching_configurations"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            MatchingConfiguration.objects.create(uuid=uuid, **defaults)
                        self.stats["matching_configurations"]["created"] += 1
                else:
                    existing = MatchingConfiguration.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["matching_configurations"]["updated"] += 1
                        else:
                            self.stats["matching_configurations"]["skipped"] += 1
                    else:
                        self.stats["matching_configurations"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import matching config {config_data.get('uuid')}: {e}"
                    )
                )
                self.stats["matching_configurations"]["errors"] += 1

    def import_call_reviewer_pools(self, pools_data):
        """Import call reviewer pool data."""
        self.stdout.write("Importing call reviewer pools...")
        for pool_data in pools_data:
            try:
                uuid = pool_data.get("uuid")
                call_uuid = pool_data.get("call_uuid")
                reviewer_uuid = pool_data.get("reviewer_uuid")
                invited_email = pool_data.get("invited_email", "")

                # Require either reviewer_uuid or invited_email (for email-only invitations)
                if not uuid or not call_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping call reviewer pool without UUID or call_uuid"
                        )
                    )
                    self.stats["call_reviewer_pools"]["errors"] += 1
                    continue

                if not reviewer_uuid and not invited_email:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping pool {uuid}: requires either reviewer_uuid or invited_email"
                        )
                    )
                    self.stats["call_reviewer_pools"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping pool {uuid}: call {call_uuid} not found"
                        )
                    )
                    self.stats["call_reviewer_pools"]["errors"] += 1
                    continue

                # Reviewer is optional for email-only invitations
                reviewer = None
                if reviewer_uuid:
                    reviewer = ReviewerProfile.objects.filter(
                        uuid=reviewer_uuid
                    ).first()
                    if not reviewer:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping pool {uuid}: reviewer {reviewer_uuid} not found"
                            )
                        )
                        self.stats["call_reviewer_pools"]["errors"] += 1
                        continue

                invited_by = None
                invited_by_uuid = pool_data.get("invited_by_uuid")
                if invited_by_uuid:
                    invited_by = User.objects.filter(uuid=invited_by_uuid).first()

                # Look up invited_user if specified
                invited_user = None
                invited_user_uuid = pool_data.get("invited_user_uuid")
                if invited_user_uuid:
                    invited_user = User.objects.filter(uuid=invited_user_uuid).first()

                defaults = {
                    "call": call,
                    "reviewer": reviewer,
                    "invited_email": invited_email,
                    "invited_user": invited_user,
                    "invited_by": invited_by,
                    "invitation_status": pool_data.get("invitation_status", "pending"),
                    "decline_reason": pool_data.get("decline_reason", ""),
                    "max_assignments": pool_data.get("max_assignments", 5),
                    "current_assignments": pool_data.get("current_assignments", 0),
                    "expertise_match_score": pool_data.get("expertise_match_score"),
                }

                # Handle invitation_token separately
                invitation_token = pool_data.get("invitation_token")

                if not self.dry_run:
                    existing = CallReviewerPool.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                CallReviewerPool.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["call_reviewer_pools"]["updated"] += 1
                        else:
                            self.stats["call_reviewer_pools"]["skipped"] += 1
                    else:
                        pool = CallReviewerPool(uuid=uuid, **defaults)
                        if invitation_token:
                            pool.invitation_token = invitation_token
                        with transaction.atomic():
                            pool.save()
                        self.stats["call_reviewer_pools"]["created"] += 1
                else:
                    existing = CallReviewerPool.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["call_reviewer_pools"]["updated"] += 1
                        else:
                            self.stats["call_reviewer_pools"]["skipped"] += 1
                    else:
                        self.stats["call_reviewer_pools"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import call reviewer pool {pool_data.get('uuid')}: {e}"
                    )
                )
                self.stats["call_reviewer_pools"]["errors"] += 1

    def import_conflicts_of_interest(self, conflicts_data):
        """Import conflict of interest data."""
        self.stdout.write("Importing conflicts of interest...")
        for coi_data in conflicts_data:
            try:
                uuid = coi_data.get("uuid")
                reviewer_uuid = coi_data.get("reviewer_uuid")
                call_uuid = coi_data.get("call_uuid")

                if not uuid or not reviewer_uuid or not call_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping COI without UUID, reviewer_uuid, or call_uuid"
                        )
                    )
                    self.stats["conflicts_of_interest"]["errors"] += 1
                    continue

                reviewer = ReviewerProfile.objects.filter(uuid=reviewer_uuid).first()
                if not reviewer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping COI {uuid}: reviewer {reviewer_uuid} not found"
                        )
                    )
                    self.stats["conflicts_of_interest"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping COI {uuid}: call {call_uuid} not found"
                        )
                    )
                    self.stats["conflicts_of_interest"]["errors"] += 1
                    continue

                proposal = None
                proposal_uuid = coi_data.get("proposal_uuid")
                if proposal_uuid:
                    proposal = Proposal.objects.filter(uuid=proposal_uuid).first()

                conflicting_user = None
                conflicting_user_uuid = coi_data.get("conflicting_user_uuid")
                if conflicting_user_uuid:
                    conflicting_user = User.objects.filter(
                        uuid=conflicting_user_uuid
                    ).first()

                conflicting_org = None
                conflicting_org_uuid = coi_data.get("conflicting_organization_uuid")
                if conflicting_org_uuid:
                    conflicting_org = Customer.objects.filter(
                        uuid=conflicting_org_uuid
                    ).first()

                defaults = {
                    "reviewer": reviewer,
                    "proposal": proposal,
                    "call": call,
                    "conflicting_user": conflicting_user,
                    "conflicting_organization": conflicting_org,
                    "coi_type": coi_data.get("coi_type", "coauthorship"),
                    "severity": coi_data.get("severity", "medium"),
                    "detection_method": coi_data.get("detection_method", "manual"),
                    "evidence_description": coi_data.get("evidence_description", ""),
                    "evidence_data": coi_data.get("evidence_data", {}),
                    "status": coi_data.get("status", "pending"),
                    "review_notes": coi_data.get("review_notes", ""),
                    "management_plan": coi_data.get("management_plan", ""),
                }

                if not self.dry_run:
                    existing = ConflictOfInterest.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ConflictOfInterest.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["conflicts_of_interest"]["updated"] += 1
                        else:
                            self.stats["conflicts_of_interest"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ConflictOfInterest.objects.create(uuid=uuid, **defaults)
                        self.stats["conflicts_of_interest"]["created"] += 1
                else:
                    existing = ConflictOfInterest.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["conflicts_of_interest"]["updated"] += 1
                        else:
                            self.stats["conflicts_of_interest"]["skipped"] += 1
                    else:
                        self.stats["conflicts_of_interest"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import COI {coi_data.get('uuid')}: {e}"
                    )
                )
                self.stats["conflicts_of_interest"]["errors"] += 1

    def import_coi_disclosure_forms(self, forms_data):
        """Import COI disclosure form data."""
        self.stdout.write("Importing COI disclosure forms...")
        for form_data in forms_data:
            try:
                uuid = form_data.get("uuid")
                reviewer_uuid = form_data.get("reviewer_uuid")

                if not uuid or not reviewer_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping COI disclosure form without UUID or reviewer_uuid"
                        )
                    )
                    self.stats["coi_disclosure_forms"]["errors"] += 1
                    continue

                reviewer = ReviewerProfile.objects.filter(uuid=reviewer_uuid).first()
                if not reviewer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping disclosure form {uuid}: reviewer {reviewer_uuid} not found"
                        )
                    )
                    self.stats["coi_disclosure_forms"]["errors"] += 1
                    continue

                call = None
                call_uuid = form_data.get("call_uuid")
                if call_uuid:
                    call = Call.objects.filter(uuid=call_uuid).first()

                defaults = {
                    "reviewer": reviewer,
                    "call": call,
                    "certified": form_data.get("certified", False),
                    "certification_date": form_data.get("certification_date"),
                    "certification_statement": form_data.get(
                        "certification_statement", ""
                    ),
                    "has_financial_interests": form_data.get(
                        "has_financial_interests", False
                    ),
                    "has_personal_relationships": form_data.get(
                        "has_personal_relationships", False
                    ),
                    "personal_relationships": form_data.get(
                        "personal_relationships", []
                    ),
                    "has_other_conflicts": form_data.get("has_other_conflicts", False),
                    "other_conflicts_description": form_data.get(
                        "other_conflicts_description", ""
                    ),
                    "valid_until": form_data.get("valid_until"),
                    "is_current": form_data.get("is_current", True),
                }

                if not self.dry_run:
                    existing = COIDisclosureForm.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                COIDisclosureForm.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["coi_disclosure_forms"]["updated"] += 1
                        else:
                            self.stats["coi_disclosure_forms"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            COIDisclosureForm.objects.create(uuid=uuid, **defaults)
                        self.stats["coi_disclosure_forms"]["created"] += 1
                else:
                    existing = COIDisclosureForm.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["coi_disclosure_forms"]["updated"] += 1
                        else:
                            self.stats["coi_disclosure_forms"]["skipped"] += 1
                    else:
                        self.stats["coi_disclosure_forms"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import COI disclosure form {form_data.get('uuid')}: {e}"
                    )
                )
                self.stats["coi_disclosure_forms"]["errors"] += 1

    def import_reviewer_proposal_affinities(self, affinities_data):
        """Import reviewer proposal affinity data."""
        self.stdout.write("Importing reviewer proposal affinities...")
        for aff_data in affinities_data:
            try:
                uuid = aff_data.get("uuid")
                call_uuid = aff_data.get("call_uuid")
                reviewer_uuid = aff_data.get("reviewer_uuid")
                proposal_uuid = aff_data.get("proposal_uuid")

                if not uuid or not call_uuid or not reviewer_uuid or not proposal_uuid:
                    self.stdout.write(
                        self.style.WARNING("Skipping affinity without required UUIDs")
                    )
                    self.stats["reviewer_proposal_affinities"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping affinity {uuid}: call {call_uuid} not found"
                        )
                    )
                    self.stats["reviewer_proposal_affinities"]["errors"] += 1
                    continue

                reviewer = ReviewerProfile.objects.filter(uuid=reviewer_uuid).first()
                if not reviewer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping affinity {uuid}: reviewer {reviewer_uuid} not found"
                        )
                    )
                    self.stats["reviewer_proposal_affinities"]["errors"] += 1
                    continue

                proposal = Proposal.objects.filter(uuid=proposal_uuid).first()
                if not proposal:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping affinity {uuid}: proposal {proposal_uuid} not found"
                        )
                    )
                    self.stats["reviewer_proposal_affinities"]["errors"] += 1
                    continue

                defaults = {
                    "call": call,
                    "reviewer": reviewer,
                    "proposal": proposal,
                    "affinity_score": aff_data.get("affinity_score", 0.0),
                    "keyword_score": aff_data.get("keyword_score"),
                    "text_score": aff_data.get("text_score"),
                }

                if not self.dry_run:
                    existing = ReviewerProposalAffinity.objects.filter(
                        uuid=uuid
                    ).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ReviewerProposalAffinity.objects.filter(
                                    uuid=uuid
                                ).update(**defaults)
                            self.stats["reviewer_proposal_affinities"]["updated"] += 1
                        else:
                            self.stats["reviewer_proposal_affinities"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ReviewerProposalAffinity.objects.create(
                                uuid=uuid, **defaults
                            )
                        self.stats["reviewer_proposal_affinities"]["created"] += 1
                else:
                    existing = ReviewerProposalAffinity.objects.filter(
                        uuid=uuid
                    ).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["reviewer_proposal_affinities"]["updated"] += 1
                        else:
                            self.stats["reviewer_proposal_affinities"]["skipped"] += 1
                    else:
                        self.stats["reviewer_proposal_affinities"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import affinity {aff_data.get('uuid')}: {e}"
                    )
                )
                self.stats["reviewer_proposal_affinities"]["errors"] += 1

    def import_reviewer_bids(self, bids_data):
        """Import reviewer bid data."""
        self.stdout.write("Importing reviewer bids...")
        for bid_data in bids_data:
            try:
                uuid = bid_data.get("uuid")
                call_uuid = bid_data.get("call_uuid")
                reviewer_uuid = bid_data.get("reviewer_uuid")
                proposal_uuid = bid_data.get("proposal_uuid")

                if not uuid or not call_uuid or not reviewer_uuid or not proposal_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping reviewer bid without required UUIDs"
                        )
                    )
                    self.stats["reviewer_bids"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping bid {uuid}: call {call_uuid} not found"
                        )
                    )
                    self.stats["reviewer_bids"]["errors"] += 1
                    continue

                reviewer = ReviewerProfile.objects.filter(uuid=reviewer_uuid).first()
                if not reviewer:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping bid {uuid}: reviewer {reviewer_uuid} not found"
                        )
                    )
                    self.stats["reviewer_bids"]["errors"] += 1
                    continue

                proposal = Proposal.objects.filter(uuid=proposal_uuid).first()
                if not proposal:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping bid {uuid}: proposal {proposal_uuid} not found"
                        )
                    )
                    self.stats["reviewer_bids"]["errors"] += 1
                    continue

                defaults = {
                    "call": call,
                    "reviewer": reviewer,
                    "proposal": proposal,
                    "bid": bid_data.get("bid", "willing"),
                    "comment": bid_data.get("comment", ""),
                }

                if not self.dry_run:
                    existing = ReviewerBid.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ReviewerBid.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["reviewer_bids"]["updated"] += 1
                        else:
                            self.stats["reviewer_bids"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ReviewerBid.objects.create(uuid=uuid, **defaults)
                        self.stats["reviewer_bids"]["created"] += 1
                else:
                    existing = ReviewerBid.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["reviewer_bids"]["updated"] += 1
                        else:
                            self.stats["reviewer_bids"]["skipped"] += 1
                    else:
                        self.stats["reviewer_bids"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import reviewer bid {bid_data.get('uuid')}: {e}"
                    )
                )
                self.stats["reviewer_bids"]["errors"] += 1

    def import_reviewer_suggestions(self, suggestions_data):
        """Import reviewer suggestions (algorithm-generated matches)."""
        self.stdout.write("Importing reviewer suggestions...")
        for item in suggestions_data:
            try:
                uuid = item.get("uuid")
                call_uuid = item.get("call_uuid")
                reviewer_uuid = item.get("reviewer_uuid")

                if not uuid or not call_uuid or not reviewer_uuid:
                    self.stats["reviewer_suggestions"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stats["reviewer_suggestions"]["errors"] += 1
                    continue

                reviewer = ReviewerProfile.objects.filter(uuid=reviewer_uuid).first()
                if not reviewer:
                    self.stats["reviewer_suggestions"]["errors"] += 1
                    continue

                reviewed_by = None
                reviewed_by_uuid = item.get("reviewed_by_uuid")
                if reviewed_by_uuid:
                    from django.contrib.auth import get_user_model

                    User = get_user_model()
                    reviewed_by = User.objects.filter(uuid=reviewed_by_uuid).first()

                defaults = {
                    "call": call,
                    "reviewer": reviewer,
                    "affinity_score": item.get("affinity_score", 0),
                    "keyword_score": item.get("keyword_score"),
                    "text_score": item.get("text_score"),
                    "status": item.get("status", "pending"),
                    "reviewed_by": reviewed_by,
                    "rejection_reason": item.get("rejection_reason", ""),
                    "matched_keywords": item.get("matched_keywords", []),
                    "top_matching_proposals": item.get("top_matching_proposals", []),
                }
                if item.get("reviewed_at"):
                    defaults["reviewed_at"] = item["reviewed_at"]

                if not self.dry_run:
                    existing = ReviewerSuggestion.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ReviewerSuggestion.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["reviewer_suggestions"]["updated"] += 1
                        else:
                            self.stats["reviewer_suggestions"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ReviewerSuggestion.objects.create(uuid=uuid, **defaults)
                        self.stats["reviewer_suggestions"]["created"] += 1
                else:
                    self.stats["reviewer_suggestions"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import reviewer suggestion {item.get('uuid')}: {e}"
                    )
                )
                self.stats["reviewer_suggestions"]["errors"] += 1

    def import_role_mappings(self, mappings_data):
        """Import proposal-to-project role mappings."""
        self.stdout.write("Importing role mappings...")
        for item in mappings_data:
            try:
                uuid = item.get("uuid")
                call_uuid = item.get("call_uuid")
                proposal_role_name = item.get("proposal_role")
                project_role_name = item.get("project_role")

                if (
                    not uuid
                    or not call_uuid
                    or not proposal_role_name
                    or not project_role_name
                ):
                    self.stats["role_mappings"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stats["role_mappings"]["errors"] += 1
                    continue

                proposal_role = Role.objects.filter(
                    name=proposal_role_name, is_active=True
                ).first()
                project_role = Role.objects.filter(
                    name=project_role_name, is_active=True
                ).first()

                if not proposal_role or not project_role:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping role mapping {uuid}: role not found "
                            f"(proposal={proposal_role_name}={proposal_role}, "
                            f"project={project_role_name}={project_role})"
                        )
                    )
                    self.stats["role_mappings"]["errors"] += 1
                    continue

                defaults = {
                    "call": call,
                    "proposal_role": proposal_role,
                    "project_role": project_role,
                }

                if not self.dry_run:
                    existing = ProposalProjectRoleMapping.objects.filter(
                        uuid=uuid
                    ).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ProposalProjectRoleMapping.objects.filter(
                                    uuid=uuid
                                ).update(**defaults)
                            self.stats["role_mappings"]["updated"] += 1
                        else:
                            self.stats["role_mappings"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ProposalProjectRoleMapping.objects.create(
                                uuid=uuid, **defaults
                            )
                        self.stats["role_mappings"]["created"] += 1
                else:
                    self.stats["role_mappings"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import role mapping {item.get('uuid')}: {e}"
                    )
                )
                self.stats["role_mappings"]["errors"] += 1

    def import_assignment_batches(self, batches_data):
        """Import assignment batch data (Stage 2 of two-stage reviewer workflow)."""
        self.stdout.write("Importing assignment batches...")
        for batch_data in batches_data:
            try:
                uuid = batch_data.get("uuid")
                call_uuid = batch_data.get("call_uuid")
                reviewer_pool_entry_uuid = batch_data.get("reviewer_pool_entry_uuid")

                if not uuid or not call_uuid or not reviewer_pool_entry_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping assignment batch without required UUIDs"
                        )
                    )
                    self.stats["assignment_batches"]["errors"] += 1
                    continue

                call = Call.objects.filter(uuid=call_uuid).first()
                if not call:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping batch {uuid}: call {call_uuid} not found"
                        )
                    )
                    self.stats["assignment_batches"]["errors"] += 1
                    continue

                reviewer_pool_entry = CallReviewerPool.objects.filter(
                    uuid=reviewer_pool_entry_uuid
                ).first()
                if not reviewer_pool_entry:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping batch {uuid}: reviewer_pool_entry {reviewer_pool_entry_uuid} not found"
                        )
                    )
                    self.stats["assignment_batches"]["errors"] += 1
                    continue

                created_by = None
                if batch_data.get("created_by_uuid"):
                    created_by = User.objects.filter(
                        uuid=batch_data["created_by_uuid"]
                    ).first()

                defaults = {
                    "call": call,
                    "reviewer_pool_entry": reviewer_pool_entry,
                    "status": batch_data.get("status", "draft"),
                    "source": batch_data.get("source", "algorithm"),
                    "created_by": created_by,
                    "invitation_token": batch_data.get("invitation_token", ""),
                    "manager_notes": batch_data.get("manager_notes", ""),
                }

                # Parse datetime fields
                if batch_data.get("sent_at"):
                    from django.utils.dateparse import parse_datetime

                    defaults["sent_at"] = parse_datetime(batch_data["sent_at"])
                if batch_data.get("expires_at"):
                    from django.utils.dateparse import parse_datetime

                    defaults["expires_at"] = parse_datetime(batch_data["expires_at"])
                if batch_data.get("responded_at"):
                    from django.utils.dateparse import parse_datetime

                    defaults["responded_at"] = parse_datetime(
                        batch_data["responded_at"]
                    )

                if not self.dry_run:
                    existing = AssignmentBatch.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                AssignmentBatch.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["assignment_batches"]["updated"] += 1
                        else:
                            self.stats["assignment_batches"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            AssignmentBatch.objects.create(uuid=uuid, **defaults)
                        self.stats["assignment_batches"]["created"] += 1
                else:
                    existing = AssignmentBatch.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["assignment_batches"]["updated"] += 1
                        else:
                            self.stats["assignment_batches"]["skipped"] += 1
                    else:
                        self.stats["assignment_batches"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import assignment batch {batch_data.get('uuid')}: {e}"
                    )
                )
                self.stats["assignment_batches"]["errors"] += 1

    def import_assignment_items(self, items_data):
        """Import assignment item data."""
        self.stdout.write("Importing assignment items...")
        for item_data in items_data:
            try:
                uuid = item_data.get("uuid")
                batch_uuid = item_data.get("batch_uuid")
                proposal_uuid = item_data.get("proposal_uuid")

                if not uuid or not batch_uuid or not proposal_uuid:
                    self.stdout.write(
                        self.style.WARNING(
                            "Skipping assignment item without required UUIDs"
                        )
                    )
                    self.stats["assignment_items"]["errors"] += 1
                    continue

                batch = AssignmentBatch.objects.filter(uuid=batch_uuid).first()
                if not batch:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping item {uuid}: batch {batch_uuid} not found"
                        )
                    )
                    self.stats["assignment_items"]["errors"] += 1
                    continue

                proposal = Proposal.objects.filter(uuid=proposal_uuid).first()
                if not proposal:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping item {uuid}: proposal {proposal_uuid} not found"
                        )
                    )
                    self.stats["assignment_items"]["errors"] += 1
                    continue

                # Optional: link to review if it exists
                review = None
                if item_data.get("review_uuid"):
                    review = Review.objects.filter(
                        uuid=item_data["review_uuid"]
                    ).first()

                # Optional: link to reassigned_from if it exists
                reassigned_from = None
                if item_data.get("reassigned_from_uuid"):
                    reassigned_from = AssignmentItem.objects.filter(
                        uuid=item_data["reassigned_from_uuid"]
                    ).first()

                defaults = {
                    "batch": batch,
                    "proposal": proposal,
                    "status": item_data.get("status", "pending"),
                    "affinity_score": item_data.get("affinity_score"),
                    "has_coi": item_data.get("has_coi", False),
                    "decline_reason": item_data.get("decline_reason", ""),
                    "review": review,
                    "reassigned_from": reassigned_from,
                    "reassign_count": item_data.get("reassign_count", 0),
                }

                # Parse datetime fields
                if item_data.get("responded_at"):
                    from django.utils.dateparse import parse_datetime

                    defaults["responded_at"] = parse_datetime(item_data["responded_at"])

                if not self.dry_run:
                    existing = AssignmentItem.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                AssignmentItem.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["assignment_items"]["updated"] += 1
                        else:
                            self.stats["assignment_items"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            AssignmentItem.objects.create(uuid=uuid, **defaults)
                        self.stats["assignment_items"]["created"] += 1
                else:
                    existing = AssignmentItem.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["assignment_items"]["updated"] += 1
                        else:
                            self.stats["assignment_items"]["skipped"] += 1
                    else:
                        self.stats["assignment_items"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import assignment item {item_data.get('uuid')}: {e}"
                    )
                )
                self.stats["assignment_items"]["errors"] += 1

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

    def import_openstack_service_settings(self, settings_data):
        """Import OpenStack service settings."""
        self.stdout.write("Importing OpenStack service settings...")

        for item in settings_data:
            try:
                uuid = item.get("uuid")
                if not uuid:
                    self.stats["openstack_service_settings"]["errors"] += 1
                    continue

                customer_uuid = item.get("customer_uuid")
                customer = None
                if customer_uuid:
                    customer = Customer.objects.filter(uuid=customer_uuid).first()
                    if not customer:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping service settings {uuid}: customer {customer_uuid} not found"
                            )
                        )
                        self.stats["openstack_service_settings"]["errors"] += 1
                        continue

                defaults = {
                    "name": item.get("name", ""),
                    "type": item.get("type", "OpenStack"),
                    "backend_url": item.get("backend_url", ""),
                    "username": item.get("username", ""),
                    "password": item.get("password", ""),
                    "domain": item.get("domain", ""),
                    "token": item.get("token", ""),
                    "shared": item.get("shared", False),
                    "options": item.get("options", {}),
                    "is_active": item.get("is_active", True),
                    "state": item.get("state", 2),
                    "customer": customer,
                }

                if not self.dry_run:
                    existing = ServiceSettings.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                ServiceSettings.objects.filter(uuid=uuid).update(
                                    **defaults
                                )
                            self.stats["openstack_service_settings"]["updated"] += 1
                        else:
                            self.stats["openstack_service_settings"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            ServiceSettings.objects.create(uuid=uuid, **defaults)
                        self.stats["openstack_service_settings"]["created"] += 1
                else:
                    existing = ServiceSettings.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["openstack_service_settings"]["updated"] += 1
                        else:
                            self.stats["openstack_service_settings"]["skipped"] += 1
                    else:
                        self.stats["openstack_service_settings"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import service settings {item.get('uuid')}: {e}"
                    )
                )
                self.stats["openstack_service_settings"]["errors"] += 1

    def import_openstack_flavors(self, flavors_data):
        """Import OpenStack flavors."""
        self.stdout.write("Importing OpenStack flavors...")

        for item in flavors_data:
            try:
                settings_uuid = item.get("settings_uuid")
                settings = ServiceSettings.objects.filter(uuid=settings_uuid).first()
                if not settings:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping flavor: settings {settings_uuid} not found"
                        )
                    )
                    self.stats["openstack_flavors"]["errors"] += 1
                    continue

                backend_id = item.get("backend_id", "")
                defaults = {
                    "name": item.get("name", ""),
                    "cores": item.get("cores", 0),
                    "ram": item.get("ram", 0),
                    "disk": item.get("disk", 0),
                }

                if not self.dry_run:
                    existing = Flavor.objects.filter(
                        settings=settings, backend_id=backend_id
                    ).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                Flavor.objects.filter(
                                    settings=settings, backend_id=backend_id
                                ).update(**defaults)
                            self.stats["openstack_flavors"]["updated"] += 1
                        else:
                            self.stats["openstack_flavors"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            Flavor.objects.create(
                                settings=settings, backend_id=backend_id, **defaults
                            )
                        self.stats["openstack_flavors"]["created"] += 1
                else:
                    existing = Flavor.objects.filter(
                        settings=settings, backend_id=backend_id
                    ).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["openstack_flavors"]["updated"] += 1
                        else:
                            self.stats["openstack_flavors"]["skipped"] += 1
                    else:
                        self.stats["openstack_flavors"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import flavor {item.get('name')}: {e}"
                    )
                )
                self.stats["openstack_flavors"]["errors"] += 1

    def import_openstack_images(self, images_data):
        """Import OpenStack images."""
        self.stdout.write("Importing OpenStack images...")

        for item in images_data:
            try:
                settings_uuid = item.get("settings_uuid")
                settings = ServiceSettings.objects.filter(uuid=settings_uuid).first()
                if not settings:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping image: settings {settings_uuid} not found"
                        )
                    )
                    self.stats["openstack_images"]["errors"] += 1
                    continue

                backend_id = item.get("backend_id", "")
                defaults = {
                    "name": item.get("name", ""),
                    "min_disk": item.get("min_disk", 0),
                    "min_ram": item.get("min_ram", 0),
                }

                if not self.dry_run:
                    # Use all_objects to bypass custom manager filtering
                    existing = Image.all_objects.filter(
                        settings=settings, backend_id=backend_id
                    ).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                Image.all_objects.filter(
                                    settings=settings, backend_id=backend_id
                                ).update(**defaults)
                            self.stats["openstack_images"]["updated"] += 1
                        else:
                            self.stats["openstack_images"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            Image(
                                settings=settings, backend_id=backend_id, **defaults
                            ).save()
                        self.stats["openstack_images"]["created"] += 1
                else:
                    existing = Image.all_objects.filter(
                        settings=settings, backend_id=backend_id
                    ).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["openstack_images"]["updated"] += 1
                        else:
                            self.stats["openstack_images"]["skipped"] += 1
                    else:
                        self.stats["openstack_images"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import image {item.get('name')}: {e}"
                    )
                )
                self.stats["openstack_images"]["errors"] += 1

    def import_openstack_tenants(self, tenants_data):
        """Import OpenStack tenants."""
        self.stdout.write("Importing OpenStack tenants...")

        for item in tenants_data:
            try:
                uuid = item.get("uuid")
                if not uuid:
                    self.stats["openstack_tenants"]["errors"] += 1
                    continue

                settings_uuid = item.get("service_settings_uuid")
                settings = ServiceSettings.objects.filter(uuid=settings_uuid).first()
                if not settings:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping tenant {uuid}: settings {settings_uuid} not found"
                        )
                    )
                    self.stats["openstack_tenants"]["errors"] += 1
                    continue

                project_uuid = item.get("project_uuid")
                project = Project.available_objects.filter(uuid=project_uuid).first()
                if not project:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping tenant {uuid}: project {project_uuid} not found"
                        )
                    )
                    self.stats["openstack_tenants"]["errors"] += 1
                    continue

                defaults = {
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "backend_id": item.get("backend_id", ""),
                    "state": item.get("state", 2),
                    "runtime_state": item.get("runtime_state", ""),
                    "service_settings": settings,
                    "project": project,
                    "internal_network_id": item.get("internal_network_id", ""),
                    "external_network_id": item.get("external_network_id", ""),
                    "availability_zone": item.get("availability_zone", ""),
                    "user_username": item.get("user_username", ""),
                    "user_password": item.get("user_password", ""),
                }

                if not self.dry_run:
                    existing = Tenant.objects.filter(uuid=uuid).first()
                    tenant = None
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                Tenant.objects.filter(uuid=uuid).update(**defaults)
                            tenant = Tenant.objects.get(uuid=uuid)
                            self.stats["openstack_tenants"]["updated"] += 1
                        else:
                            self.stats["openstack_tenants"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            tenant = Tenant.objects.create(uuid=uuid, **defaults)
                        self.stats["openstack_tenants"]["created"] += 1

                    if tenant is not None:
                        self.import_tenant_quotas(tenant, item.get("quotas", []))
                else:
                    existing = Tenant.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["openstack_tenants"]["updated"] += 1
                        else:
                            self.stats["openstack_tenants"]["skipped"] += 1
                    else:
                        self.stats["openstack_tenants"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import tenant {item.get('uuid')}: {e}"
                    )
                )
                self.stats["openstack_tenants"]["errors"] += 1

    def import_tenant_quotas(self, tenant, quotas_data):
        """Apply preset quota limits and usages to an imported tenant.

        Without this the quota table is only populated by asynchronous backend
        sync, so UI tests that read quota values immediately after import race
        the sync. Both setters are idempotent (update_or_create for limits,
        delta-to-target for usages), so re-importing is safe.
        """
        for quota in quotas_data:
            name = quota.get("name")
            if not name:
                continue
            if "limit" in quota:
                tenant.set_quota_limit(name, quota["limit"])
            if "usage" in quota:
                tenant.set_quota_usage(name, quota["usage"])

    def import_openstack_instances(self, instances_data):
        """Import OpenStack instances."""
        self.stdout.write("Importing OpenStack instances...")

        for item in instances_data:
            try:
                uuid = item.get("uuid")
                if not uuid:
                    self.stats["openstack_instances"]["errors"] += 1
                    continue

                tenant_uuid = item.get("tenant_uuid")
                tenant = Tenant.objects.filter(uuid=tenant_uuid).first()
                if not tenant:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping instance {uuid}: tenant {tenant_uuid} not found"
                        )
                    )
                    self.stats["openstack_instances"]["errors"] += 1
                    continue

                defaults = {
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "backend_id": item.get("backend_id", ""),
                    "state": item.get("state", 2),
                    "runtime_state": item.get("runtime_state", ""),
                    "tenant": tenant,
                    "service_settings": tenant.service_settings,
                    "project": tenant.project,
                    "cores": item.get("cores", 0),
                    "ram": item.get("ram", 0),
                    "disk": item.get("disk", 0),
                    "image_name": item.get("image_name", ""),
                    "flavor_name": item.get("flavor_name", ""),
                    "flavor_disk": item.get("flavor_disk", 0),
                    "hypervisor_hostname": item.get("hypervisor_hostname", ""),
                    "key_name": item.get("key_name", ""),
                    "key_fingerprint": item.get("key_fingerprint", ""),
                    "directly_connected_ips": item.get("directly_connected_ips", ""),
                }

                if not self.dry_run:
                    existing = Instance.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                Instance.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["openstack_instances"]["updated"] += 1
                        else:
                            self.stats["openstack_instances"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            Instance.objects.create(uuid=uuid, **defaults)
                        self.stats["openstack_instances"]["created"] += 1
                else:
                    existing = Instance.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["openstack_instances"]["updated"] += 1
                        else:
                            self.stats["openstack_instances"]["skipped"] += 1
                    else:
                        self.stats["openstack_instances"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import instance {item.get('uuid')}: {e}"
                    )
                )
                self.stats["openstack_instances"]["errors"] += 1

    def import_openstack_volumes(self, volumes_data):
        """Import OpenStack volumes."""
        self.stdout.write("Importing OpenStack volumes...")

        for item in volumes_data:
            try:
                uuid = item.get("uuid")
                if not uuid:
                    self.stats["openstack_volumes"]["errors"] += 1
                    continue

                tenant_uuid = item.get("tenant_uuid")
                tenant = Tenant.objects.filter(uuid=tenant_uuid).first()
                if not tenant:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping volume {uuid}: tenant {tenant_uuid} not found"
                        )
                    )
                    self.stats["openstack_volumes"]["errors"] += 1
                    continue

                # Resolve optional instance FK
                instance = None
                instance_uuid = item.get("instance_uuid")
                if instance_uuid:
                    instance = Instance.objects.filter(uuid=instance_uuid).first()

                defaults = {
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "backend_id": item.get("backend_id", ""),
                    "state": item.get("state", 2),
                    "runtime_state": item.get("runtime_state", ""),
                    "tenant": tenant,
                    "service_settings": tenant.service_settings,
                    "project": tenant.project,
                    "instance": instance,
                    "size": item.get("size", 0),
                    "bootable": item.get("bootable", False),
                    "device": item.get("device", ""),
                    "image_name": item.get("image_name", ""),
                }

                if not self.dry_run:
                    existing = Volume.objects.filter(uuid=uuid).first()
                    if existing:
                        if self.update_existing:
                            with transaction.atomic():
                                Volume.objects.filter(uuid=uuid).update(**defaults)
                            self.stats["openstack_volumes"]["updated"] += 1
                        else:
                            self.stats["openstack_volumes"]["skipped"] += 1
                    else:
                        with transaction.atomic():
                            Volume.objects.create(uuid=uuid, **defaults)
                        self.stats["openstack_volumes"]["created"] += 1
                else:
                    existing = Volume.objects.filter(uuid=uuid).exists()
                    if existing:
                        if self.update_existing:
                            self.stats["openstack_volumes"]["updated"] += 1
                        else:
                            self.stats["openstack_volumes"]["skipped"] += 1
                    else:
                        self.stats["openstack_volumes"]["created"] += 1

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to import volume {item.get('uuid')}: {e}"
                    )
                )
                self.stats["openstack_volumes"]["errors"] += 1

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
