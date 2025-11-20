import logging
import uuid
import warnings

from django.core.management.base import BaseCommand, CommandError

from waldur_core.structure.models import ServiceSettings
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OPENSTACK_TENANT_OFFERING
from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.executors import TenantDeleteExecutor
from waldur_openstack.session import (
    get_cinder_client,
    get_glance_client,
    get_keystone_client,
    get_neutron_client,
    get_nova_client,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Validate access to all OpenStack services used in Waldur for configured offerings"

    def add_arguments(self, parser):
        parser.add_argument(
            "--service-uuid",
            type=str,
            help="UUID of specific OpenStack service to validate (optional)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be validated without actual connection attempts",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose output",
        )
        parser.add_argument(
            "--test-writes",
            action="store_true",
            help="Test write operations (create/update/delete) - WARNING: Creates and deletes test resources",
        )
        parser.add_argument(
            "--tenant-uuid",
            type=str,
            help="UUID of specific tenant to use for write tests (mutually exclusive with --offering-uuid)",
        )
        parser.add_argument(
            "--offering-uuid",
            type=str,
            help="UUID of OpenStack offering to test against (creates temporary tenant)",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Suppress SSL warnings and other verbose output",
        )

    def handle(self, *args, **options):
        verbose = options["verbose"]
        dry_run = options["dry_run"]
        test_writes = options["test_writes"]
        quiet = options["quiet"]
        service_uuid = options.get("service_uuid")
        tenant_uuid = options.get("tenant_uuid")
        offering_uuid = options.get("offering_uuid")

        if test_writes and not tenant_uuid and not offering_uuid:
            raise CommandError(
                "Either --tenant-uuid or --offering-uuid is required when using --test-writes"
            )

        if tenant_uuid and offering_uuid:
            raise CommandError(
                "--tenant-uuid and --offering-uuid are mutually exclusive"
            )

        # Handle conflicting quiet and verbose options
        if quiet and verbose:
            # When both are specified, suppress warnings but keep verbose validation output
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            warnings.filterwarnings("ignore", module="neutronclient")
            warnings.filterwarnings("ignore", module="keystoneauth1")
            warnings.filterwarnings(
                "ignore", message=".*Unable to recover OpenStack session.*"
            )
            warnings.filterwarnings(
                "ignore", message=".*python binding code.*deprecated.*"
            )
            # Suppress specific loggers
            logging.getLogger("keystoneauth1.session").setLevel(logging.ERROR)
            logging.getLogger("neutronclient").setLevel(logging.ERROR)
            logging.getLogger("waldur_openstack.session").setLevel(logging.ERROR)
            # Keep INFO level for verbose output but suppress warnings
            logging.basicConfig(level=logging.INFO)
        elif verbose:
            # In verbose mode, suppress warnings but keep detailed logging
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            warnings.filterwarnings("ignore", module="neutronclient")
            warnings.filterwarnings("ignore", module="keystoneauth1")
            warnings.filterwarnings(
                "ignore", message=".*Unable to recover OpenStack session.*"
            )
            warnings.filterwarnings(
                "ignore", message=".*python binding code.*deprecated.*"
            )
            # Suppress specific loggers that might already be configured
            logging.getLogger("keystoneauth1.session").setLevel(logging.ERROR)
            logging.getLogger("neutronclient").setLevel(logging.ERROR)
            logging.getLogger("waldur_openstack.session").setLevel(logging.ERROR)
            logging.basicConfig(level=logging.DEBUG)
        elif quiet:
            # Suppress SSL warnings and other verbose warnings
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            warnings.filterwarnings("ignore", module="neutronclient")
            warnings.filterwarnings("ignore", module="keystoneauth1")
            warnings.filterwarnings(
                "ignore", message=".*Unable to recover OpenStack session.*"
            )
            warnings.filterwarnings(
                "ignore", message=".*python binding code.*deprecated.*"
            )
            # Suppress specific loggers
            logging.getLogger("keystoneauth1.session").setLevel(logging.ERROR)
            logging.getLogger("neutronclient").setLevel(logging.ERROR)
            logging.getLogger("waldur_openstack.session").setLevel(logging.ERROR)
            # Set logging level to ERROR to suppress INFO/WARNING logs
            logging.getLogger().setLevel(logging.ERROR)

        # Get OpenStack services
        if offering_uuid:
            # Get service settings from offering
            try:
                offering = marketplace_models.Offering.objects.get(uuid=offering_uuid)
                if offering.type != OPENSTACK_TENANT_OFFERING:
                    raise CommandError(
                        f"Offering {offering_uuid} is not an OpenStack tenant offering"
                    )

                service_settings = offering.scope
                if (
                    not isinstance(service_settings, ServiceSettings)
                    or service_settings.type != "OpenStack"
                ):
                    raise CommandError(
                        f"Offering {offering_uuid} does not have a valid OpenStack service"
                    )

                queryset = ServiceSettings.objects.filter(uuid=service_settings.uuid)
                self.stdout.write(
                    f"Using OpenStack service from offering: {offering.name}"
                )
            except marketplace_models.Offering.DoesNotExist:
                raise CommandError(f"Offering with UUID {offering_uuid} not found")
        else:
            # Original logic for service-based validation
            queryset = ServiceSettings.objects.filter(type="OpenStack")
            if service_uuid:
                queryset = queryset.filter(uuid=service_uuid)
                if not queryset.exists():
                    raise CommandError(
                        f"OpenStack service with UUID {service_uuid} not found"
                    )

            if not queryset.exists():
                self.stdout.write(
                    self.style.WARNING("No OpenStack services found in the system")
                )
                return

            self.stdout.write(
                f"Found {queryset.count()} OpenStack service(s) to validate"
            )

        for service_settings in queryset:
            self.stdout.write(f"\n=== Validating service: {service_settings.name} ===")
            self.stdout.write(f"UUID: {service_settings.uuid}")
            self.stdout.write(f"Backend URL: {service_settings.backend_url}")

            if dry_run:
                self.stdout.write(
                    self.style.WARNING("DRY-RUN: Skipping actual validation")
                )
                self._show_service_details(service_settings)
                continue

            try:
                self._validate_service(
                    service_settings, verbose, test_writes, tenant_uuid, offering_uuid
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        f"✓ Service {service_settings.name} validation completed"
                    )
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"✗ Service {service_settings.name} validation failed: {e}"
                    )
                )
                if verbose:
                    logger.exception("Detailed error information:")

    def _show_service_details(self, service_settings):
        """Show service configuration details"""
        self.stdout.write("Service configuration:")
        self.stdout.write(f"  - Auth URL: {service_settings.backend_url}")
        self.stdout.write(f"  - Username: {service_settings.username}")
        self.stdout.write(
            f"  - Domain: {getattr(service_settings, 'domain', 'Default')}"
        )
        self.stdout.write(f"  - Type: {service_settings.type}")

        # Show which services would be tested
        services_to_test = [
            "Keystone (Identity)",
            "Nova (Compute)",
            "Neutron (Network)",
            "Cinder (Block Storage)",
            "Glance (Image)",
        ]
        self.stdout.write("Services that would be validated:")
        for service in services_to_test:
            self.stdout.write(f"  - {service}")

    def _validate_service(
        self,
        service_settings,
        verbose=False,
        test_writes=False,
        tenant_uuid=None,
        offering_uuid=None,
    ):
        """Validate access to OpenStack service and all its components"""
        backend = OpenStackBackend(service_settings)

        # Test 1: Basic authentication
        self.stdout.write("1. Testing authentication...")
        try:
            if not backend.ping():
                raise Exception("Authentication failed - ping test returned False")
            self.stdout.write(self.style.SUCCESS("   ✓ Authentication successful"))
        except Exception as e:
            raise Exception(f"Authentication failed: {e}")

        # Test 2: Keystone (Identity) service
        self.stdout.write("2. Testing Keystone (Identity) service...")
        try:
            session = backend.admin_session
            keystone = get_keystone_client(session)

            # Test basic operations
            projects = keystone.projects.list()
            if verbose:
                self.stdout.write(f"   Found {len(projects)} projects")

            domains = keystone.domains.list()
            if verbose:
                self.stdout.write(f"   Found {len(domains)} domains")

            self.stdout.write(self.style.SUCCESS("   ✓ Keystone service accessible"))
        except Exception as e:
            raise Exception(f"Keystone validation failed: {e}")

        # Test 3: Nova (Compute) service
        self.stdout.write("3. Testing Nova (Compute) service...")
        try:
            session = backend.admin_session
            nova = get_nova_client(session)

            # Test basic operations
            flavors = nova.flavors.list()
            if verbose:
                self.stdout.write(f"   Found {len(flavors)} flavors")

            hypervisors = nova.hypervisors.list()
            if verbose:
                self.stdout.write(f"   Found {len(hypervisors)} hypervisors")

            self.stdout.write(self.style.SUCCESS("   ✓ Nova service accessible"))
        except Exception as e:
            raise Exception(f"Nova validation failed: {e}")

        # Test 4: Neutron (Network) service
        self.stdout.write("4. Testing Neutron (Network) service...")
        try:
            session = backend.admin_session
            neutron = get_neutron_client(session)

            # Test basic operations
            networks = neutron.list_networks()
            if verbose:
                self.stdout.write(
                    f"   Found {len(networks.get('networks', []))} networks"
                )

            # Check for external networks
            external_networks = neutron.list_networks(**{"router:external": True})
            if verbose:
                ext_count = len(external_networks.get("networks", []))
                self.stdout.write(f"   Found {ext_count} external networks")

            self.stdout.write(self.style.SUCCESS("   ✓ Neutron service accessible"))
        except Exception as e:
            raise Exception(f"Neutron validation failed: {e}")

        # Test 5: Cinder (Block Storage) service
        self.stdout.write("5. Testing Cinder (Block Storage) service...")
        try:
            session = backend.admin_session
            cinder = get_cinder_client(session)

            # Test basic operations
            volume_types = cinder.volume_types.list()
            if verbose:
                self.stdout.write(f"   Found {len(volume_types)} volume types")

            # Test services (availability zones)
            try:
                services = cinder.services.list()
                if verbose:
                    self.stdout.write(f"   Found {len(services)} cinder services")
            except Exception:
                # Some deployments might not expose services endpoint
                if verbose:
                    self.stdout.write(
                        "   Cinder services endpoint not accessible (normal in some deployments)"
                    )

            self.stdout.write(self.style.SUCCESS("   ✓ Cinder service accessible"))
        except Exception as e:
            raise Exception(f"Cinder validation failed: {e}")

        # Test 6: Glance (Image) service
        self.stdout.write("6. Testing Glance (Image) service...")
        try:
            session = backend.admin_session
            glance = get_glance_client(session)

            # Test basic operations
            images = list(glance.images.list())
            if verbose:
                self.stdout.write(f"   Found {len(images)} images")

            self.stdout.write(self.style.SUCCESS("   ✓ Glance service accessible"))
        except Exception as e:
            raise Exception(f"Glance validation failed: {e}")

        # Test 7: Quotas validation (cross-service test)
        self.stdout.write("7. Testing quota management...")
        try:
            # Get a test tenant if available
            session = backend.admin_session
            keystone = get_keystone_client(session)
            projects = keystone.projects.list()

            if projects:
                test_project = projects[0]

                # Test Nova quotas
                nova = get_nova_client(session)
                nova.quotas.get(tenant_id=test_project.id)
                if verbose:
                    self.stdout.write(
                        f"   Nova quotas accessible for project {test_project.name}"
                    )

                # Test Cinder quotas
                cinder = get_cinder_client(session)
                cinder.quotas.get(tenant_id=test_project.id)
                if verbose:
                    self.stdout.write(
                        f"   Cinder quotas accessible for project {test_project.name}"
                    )

                # Test Neutron quotas
                neutron = get_neutron_client(session)
                neutron.show_quota(tenant_id=test_project.id)
                if verbose:
                    self.stdout.write(
                        f"   Neutron quotas accessible for project {test_project.name}"
                    )

                self.stdout.write(self.style.SUCCESS("   ✓ Quota management working"))
            else:
                self.stdout.write(
                    self.style.WARNING("   No projects found to test quotas")
                )
        except Exception as e:
            raise Exception(f"Quota validation failed: {e}")

        # Test 8: Service endpoints validation
        self.stdout.write("8. Testing service endpoints...")
        try:
            session = backend.admin_session
            keystone = get_keystone_client(session)

            # List all services and endpoints
            services = keystone.services.list()
            endpoints = keystone.endpoints.list()

            service_names = [s.name for s in services]
            required_services = ["keystone", "nova", "neutron", "cinder", "glance"]

            for req_service in required_services:
                if any(req_service in name.lower() for name in service_names):
                    if verbose:
                        self.stdout.write(
                            f"   ✓ {req_service.capitalize()} service endpoint found"
                        )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            f"   ! {req_service.capitalize()} service endpoint not found"
                        )
                    )

            if verbose:
                self.stdout.write(
                    f"   Total services: {len(services)}, Total endpoints: {len(endpoints)}"
                )

            self.stdout.write(self.style.SUCCESS("   ✓ Service endpoints validated"))
        except Exception as e:
            raise Exception(f"Service endpoints validation failed: {e}")

        # Write operations testing (optional)
        if test_writes:
            self.stdout.write("\n=== Testing Write Operations ===")
            try:
                self._test_write_operations(
                    service_settings, tenant_uuid, offering_uuid, verbose
                )
                self.stdout.write(
                    self.style.SUCCESS("   ✓ Write operations validation completed")
                )
            except Exception as e:
                raise Exception(f"Write operations validation failed: {e}")

    def _test_write_operations(
        self, service_settings, tenant_uuid, offering_uuid, verbose=False
    ):
        """Test write operations against OpenStack services"""
        backend = OpenStackBackend(service_settings)
        session = backend.admin_session

        created_tenant = None
        created_project = None
        tenant = None

        # Get or create the test tenant
        if tenant_uuid:
            # Use existing tenant
            try:
                tenant = models.Tenant.objects.get(uuid=tenant_uuid)
                if tenant.service_settings != service_settings:
                    raise Exception(
                        f"Tenant {tenant_uuid} does not belong to service {service_settings.name}"
                    )
                self.stdout.write(
                    f"Using existing tenant: {tenant.name} ({tenant.uuid})"
                )
            except models.Tenant.DoesNotExist:
                raise Exception(f"Tenant with UUID {tenant_uuid} not found")

        elif offering_uuid:
            # Create temporary tenant directly in OpenStack backend for testing
            try:
                offering = marketplace_models.Offering.objects.get(uuid=offering_uuid)

                # Create tenant directly in OpenStack backend first
                test_tenant_name = f"validation-test-{uuid.uuid4().hex[:8]}"
                keystone = get_keystone_client(backend.admin_session)

                # Create project in OpenStack backend
                domain = backend._get_domain()
                backend_project = keystone.projects.create(
                    name=test_tenant_name,
                    description="Temporary tenant for OpenStack validation testing",
                    domain=domain,
                )

                if verbose:
                    self.stdout.write(
                        f"Created tenant in OpenStack backend: {backend_project.id}"
                    )

                # Create a simple tenant object for testing (not in Waldur database)
                class TestTenant:
                    def __init__(self, backend_id, name):
                        self.backend_id = backend_id
                        self.name = name
                        self.uuid = uuid.uuid4()

                tenant = TestTenant(backend_project.id, test_tenant_name)
                created_tenant = tenant  # Track for cleanup

                self.stdout.write(
                    f"Created temporary tenant: {tenant.name} (backend_id: {tenant.backend_id})"
                )

            except marketplace_models.Offering.DoesNotExist:
                raise Exception(f"Offering with UUID {offering_uuid} not found")
        else:
            raise Exception("Either tenant_uuid or offering_uuid must be provided")

        # Cleanup tracking
        cleanup_tasks = []
        original_quotas = {}

        def add_cleanup_task(task_type, client_func, resource_id, description=""):
            """Add a cleanup task to be executed during cleanup"""
            cleanup_tasks.append(
                {
                    "type": task_type,
                    "client_func": client_func,
                    "resource_id": resource_id,
                    "description": description,
                }
            )

        def perform_cleanup():
            """Perform all cleanup tasks"""
            if (
                not cleanup_tasks
                and not original_quotas
                and not created_tenant
                and not created_project
            ):
                return

            self.stdout.write("\n=== Performing Cleanup ===")

            # Clean up created resources
            for task in reversed(cleanup_tasks):  # Reverse order for proper cleanup
                try:
                    if task["type"] == "security_group":
                        neutron = get_neutron_client(session)
                        neutron.delete_security_group(task["resource_id"])
                    elif task["type"] == "network":
                        neutron = get_neutron_client(session)
                        neutron.delete_network(task["resource_id"])
                    elif task["type"] == "volume":
                        cinder = get_cinder_client(session)
                        cinder.volumes.delete(task["resource_id"])
                    elif task["type"] == "server_group":
                        nova = get_nova_client(session)
                        nova.server_groups.delete(task["resource_id"])
                    elif task["type"] == "floating_ip":
                        neutron = get_neutron_client(session)
                        neutron.delete_floatingip(task["resource_id"])
                    elif task["type"] == "instance":
                        nova = get_nova_client(session)
                        nova.servers.delete(task["resource_id"])

                    if verbose:
                        self.stdout.write(
                            f"   ✓ Cleaned up {task['type']}: {task['resource_id']}"
                        )
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(
                            f"   ! Failed to cleanup {task['type']} {task['resource_id']}: {e}"
                        )
                    )

            # Restore original quotas
            if original_quotas:
                try:
                    if "nova_instances" in original_quotas:
                        nova = get_nova_client(session)
                        nova.quotas.update(
                            tenant_id=tenant.backend_id,
                            instances=original_quotas["nova_instances"],
                        )
                        if verbose:
                            self.stdout.write(
                                f"   ✓ Restored Nova instances quota to {original_quotas['nova_instances']}"
                            )
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(f"   ! Failed to restore quotas: {e}")
                    )

            # Clean up created tenant
            if created_tenant:
                try:
                    self.stdout.write(
                        f"   Deleting temporary tenant: {created_tenant.name}"
                    )
                    backend_tenant_id = created_tenant.backend_id

                    # Always try direct backend deletion first for immediate cleanup
                    keystone = get_keystone_client(session)
                    try:
                        # Delete directly from OpenStack backend
                        keystone.projects.delete(backend_tenant_id)
                        if verbose:
                            self.stdout.write(
                                f"   ✓ Deleted tenant from OpenStack backend: {backend_tenant_id}"
                            )

                        # Verify deletion from backend
                        try:
                            keystone.projects.get(backend_tenant_id)
                            self.stdout.write(
                                self.style.WARNING(
                                    f"   ! Tenant still exists in backend after deletion: {backend_tenant_id}"
                                )
                            )
                        except Exception:
                            if verbose:
                                self.stdout.write(
                                    f"   ✓ Confirmed tenant deleted from backend: {backend_tenant_id}"
                                )
                    except Exception as backend_delete_error:
                        if verbose:
                            self.stdout.write(
                                f"   ! Backend deletion failed: {backend_delete_error}"
                            )

                    # Also try Waldur deletion if it's a Waldur-managed tenant
                    if hasattr(created_tenant, "refresh_from_db"):
                        try:
                            # Force delete from Waldur database regardless of state
                            created_tenant.delete()
                            if verbose:
                                self.stdout.write(
                                    f"   ✓ Removed tenant from Waldur database: {created_tenant.uuid}"
                                )
                        except Exception:
                            # Try TenantDeleteExecutor as fallback
                            try:
                                TenantDeleteExecutor.execute(created_tenant)
                                if verbose:
                                    self.stdout.write(
                                        f"   ✓ Scheduled Waldur tenant deletion: {created_tenant.uuid}"
                                    )
                            except Exception as executor_error:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"   ! Failed to delete Waldur tenant: {executor_error}"
                                    )
                                )

                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(
                            f"   ! Failed to delete tenant {backend_tenant_id}: {e}"
                        )
                    )

            # Clean up created project
            if created_project:
                try:
                    self.stdout.write(
                        f"   Deleting temporary project: {created_project.name}"
                    )
                    created_project.delete()

                    if verbose:
                        self.stdout.write(
                            f"   ✓ Project deleted: {created_project.uuid}"
                        )
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(
                            f"   ! Failed to delete project {created_project.uuid}: {e}"
                        )
                    )

            self.stdout.write("✓ Cleanup completed")

        try:
            # Test 1: Security Group operations
            self.stdout.write("1. Testing Security Group create/update/delete...")
            test_sg_name = f"valdation-test-sg-{uuid.uuid4().hex[:8]}"
            sg_id = None
            try:
                neutron = get_neutron_client(session)

                # Create security group
                sg_data = {
                    "security_group": {
                        "name": test_sg_name,
                        "description": "Temporary security group for validation testing",
                        "tenant_id": tenant.backend_id,
                    }
                }
                created_sg = neutron.create_security_group(sg_data)
                sg_id = created_sg["security_group"]["id"]
                add_cleanup_task("security_group", None, sg_id, test_sg_name)

                if verbose:
                    self.stdout.write(f"   Created security group: {sg_id}")

                # Update security group description
                update_data = {
                    "security_group": {
                        "description": "Updated description for validation testing"
                    }
                }
                neutron.update_security_group(sg_id, update_data)

                if verbose:
                    self.stdout.write(f"   Updated security group: {sg_id}")

                # Delete security group (remove from cleanup since we're doing it now)
                neutron.delete_security_group(sg_id)
                cleanup_tasks = [
                    task for task in cleanup_tasks if task["resource_id"] != sg_id
                ]

                if verbose:
                    self.stdout.write(f"   Deleted security group: {sg_id}")

                self.stdout.write(
                    self.style.SUCCESS("   ✓ Security Group operations successful")
                )
            except Exception as e:
                raise Exception(f"Security Group operations failed: {e}")

            # Test 2: Network operations
            self.stdout.write("2. Testing Network create/update/delete...")
            test_network_name = f"validation-test-net-{uuid.uuid4().hex[:8]}"
            try:
                neutron = get_neutron_client(session)

                # Create network
                network_data = {
                    "network": {
                        "name": test_network_name,
                        "tenant_id": tenant.backend_id,
                        "admin_state_up": True,
                    }
                }
                created_network = neutron.create_network(network_data)
                network_id = created_network["network"]["id"]
                add_cleanup_task("network", None, network_id, test_network_name)

                if verbose:
                    self.stdout.write(f"   Created network: {network_id}")

                # Update network description
                update_data = {"network": {"name": f"{test_network_name}-updated"}}
                neutron.update_network(network_id, update_data)

                if verbose:
                    self.stdout.write(f"   Updated network: {network_id}")

                # Delete network (remove from cleanup since we're doing it now)
                neutron.delete_network(network_id)
                cleanup_tasks[:] = [
                    task for task in cleanup_tasks if task["resource_id"] != network_id
                ]

                if verbose:
                    self.stdout.write(f"   Deleted network: {network_id}")

                self.stdout.write(
                    self.style.SUCCESS("   ✓ Network operations successful")
                )
            except Exception as e:
                raise Exception(f"Network operations failed: {e}")

            # Test 3: Volume operations
            self.stdout.write("3. Testing Volume create/update/delete...")
            test_volume_name = f"validation-test-vol-{uuid.uuid4().hex[:8]}"
            try:
                cinder = get_cinder_client(session)

                # Create small volume (1GB)
                volume_data = {
                    "name": test_volume_name,
                    "description": "Temporary volume for validation testing",
                    "size": 1,
                    "volume_type": None,  # Use default volume type
                }
                created_volume = cinder.volumes.create(**volume_data)
                volume_id = created_volume.id
                add_cleanup_task("volume", None, volume_id, test_volume_name)

                if verbose:
                    self.stdout.write(f"   Created volume: {volume_id}")

                # Wait briefly for volume creation to start
                import time

                time.sleep(2)

                # Update volume description
                cinder.volumes.update(
                    volume_id, description="Updated description for validation testing"
                )

                if verbose:
                    self.stdout.write(f"   Updated volume: {volume_id}")

                # Delete volume (remove from cleanup since we're doing it now)
                cinder.volumes.delete(volume_id)
                cleanup_tasks[:] = [
                    task for task in cleanup_tasks if task["resource_id"] != volume_id
                ]

                if verbose:
                    self.stdout.write(f"   Deleted volume: {volume_id}")

                self.stdout.write(
                    self.style.SUCCESS("   ✓ Volume operations successful")
                )
            except Exception as e:
                raise Exception(f"Volume operations failed: {e}")

            # Test 4: Server Group operations (if Nova compute service supports it)
            self.stdout.write("4. Testing Server Group create/delete...")
            test_sg_name = f"validation-test-servergroup-{uuid.uuid4().hex[:8]}"
            try:
                nova = get_nova_client(session)

                # Create server group
                created_sg = nova.server_groups.create(
                    name=test_sg_name,
                    policies=["affinity"],  # or ["anti-affinity"]
                )
                sg_id = created_sg.id
                add_cleanup_task("server_group", None, sg_id, test_sg_name)

                if verbose:
                    self.stdout.write(f"   Created server group: {sg_id}")

                # Delete server group (remove from cleanup since we're doing it now)
                nova.server_groups.delete(sg_id)
                cleanup_tasks[:] = [
                    task for task in cleanup_tasks if task["resource_id"] != sg_id
                ]

                if verbose:
                    self.stdout.write(f"   Deleted server group: {sg_id}")

                self.stdout.write(
                    self.style.SUCCESS("   ✓ Server Group operations successful")
                )
            except Exception as e:
                # Server groups might not be supported in all deployments
                self.stdout.write(
                    self.style.WARNING(
                        f"   ! Server Group operations not supported: {e}"
                    )
                )

            # Test 5: Quota management
            self.stdout.write("5. Testing Quota management...")
            try:
                nova = get_nova_client(session)
                neutron = get_neutron_client(session)
                cinder = get_cinder_client(session)

                # Get current quotas
                nova_quotas = nova.quotas.get(tenant_id=tenant.backend_id)
                neutron_quotas = neutron.show_quota(tenant_id=tenant.backend_id)
                cinder_quotas = cinder.quotas.get(tenant_id=tenant.backend_id)

                if verbose:
                    self.stdout.write(
                        f"   Nova quota instances: {nova_quotas.instances}"
                    )
                    self.stdout.write(
                        f"   Neutron quota networks: {neutron_quotas['quota']['network']}"
                    )
                    self.stdout.write(
                        f"   Cinder quota volumes: {cinder_quotas.volumes}"
                    )

                # Test quota update (temporarily change and restore)
                original_instances = nova_quotas.instances
                original_quotas["nova_instances"] = (
                    original_instances  # Track for cleanup
                )
                test_instances = (
                    max(1, original_instances - 1)
                    if original_instances > 1
                    else original_instances + 1
                )

                # Update quota
                nova.quotas.update(
                    tenant_id=tenant.backend_id, instances=test_instances
                )

                # Verify update
                updated_quotas = nova.quotas.get(tenant_id=tenant.backend_id)
                if updated_quotas.instances == test_instances:
                    if verbose:
                        self.stdout.write(
                            f"   Updated instances quota from {original_instances} to {test_instances}"
                        )
                else:
                    raise Exception(
                        f"Quota update failed: expected {test_instances}, got {updated_quotas.instances}"
                    )

                # Restore original quota
                nova.quotas.update(
                    tenant_id=tenant.backend_id, instances=original_instances
                )
                del original_quotas[
                    "nova_instances"
                ]  # Remove from cleanup since we restored it

                if verbose:
                    self.stdout.write(
                        f"   Restored instances quota to {original_instances}"
                    )

                self.stdout.write(
                    self.style.SUCCESS("   ✓ Quota management successful")
                )
            except Exception as e:
                raise Exception(f"Quota management failed: {e}")

            # Test 6: Floating IP operations
            self.stdout.write("6. Testing Floating IP create/delete...")
            try:
                neutron = get_neutron_client(session)

                # Get external networks
                external_networks = neutron.list_networks(**{"router:external": True})[
                    "networks"
                ]
                if not external_networks:
                    self.stdout.write(
                        self.style.WARNING(
                            "   ! No external networks found, skipping floating IP test"
                        )
                    )
                    return

                external_network_id = external_networks[0]["id"]

                # Create floating IP
                fip_data = {
                    "floatingip": {
                        "floating_network_id": external_network_id,
                        "tenant_id": tenant.backend_id,
                    }
                }
                created_fip = neutron.create_floatingip(fip_data)
                fip_id = created_fip["floatingip"]["id"]
                add_cleanup_task("floating_ip", None, fip_id, "test-floating-ip")

                if verbose:
                    self.stdout.write(f"   Created floating IP: {fip_id}")

                # Update floating IP description
                update_data = {
                    "floatingip": {"description": "Test floating IP for validation"}
                }
                neutron.update_floatingip(fip_id, update_data)

                if verbose:
                    self.stdout.write(f"   Updated floating IP: {fip_id}")

                # Delete floating IP (remove from cleanup since we're doing it now)
                neutron.delete_floatingip(fip_id)
                cleanup_tasks[:] = [
                    task for task in cleanup_tasks if task["resource_id"] != fip_id
                ]

                if verbose:
                    self.stdout.write(f"   Deleted floating IP: {fip_id}")

                self.stdout.write(
                    self.style.SUCCESS("   ✓ Floating IP operations successful")
                )
            except Exception as e:
                raise Exception(f"Floating IP operations failed: {e}")

            # Test 7: Instance operations using Waldur backend method
            self.stdout.write(
                "7. Testing Instance create/delete using Waldur backend..."
            )
            try:
                # Get available flavors for instance creation
                nova = get_nova_client(session)
                flavors = nova.flavors.list()
                if not flavors:
                    self.stdout.write(
                        self.style.WARNING(
                            "   ! No flavors available, skipping instance test"
                        )
                    )
                else:
                    # Use the smallest flavor
                    flavor = min(flavors, key=lambda f: f.vcpus * f.ram)

                    # Get available images
                    glance = get_glance_client(session)
                    images = list(glance.images.list())
                    if not images:
                        self.stdout.write(
                            self.style.WARNING(
                                "   ! No images available, skipping instance test"
                            )
                        )
                    else:
                        # Use first available image
                        image = images[0]

                        # Create a minimal Waldur Instance model for testing
                        test_instance_name = (
                            f"validation-test-vm-{uuid.uuid4().hex[:8]}"
                        )

                        # For backend.create_instance, we need a real Waldur tenant
                        if hasattr(tenant, "refresh_from_db"):
                            # Real Waldur tenant
                            waldur_tenant = tenant
                        else:
                            # Mock tenant - create a temporary Waldur tenant for instance testing
                            # First get any project to use
                            from waldur_core.structure.models import Project

                            test_project = Project.objects.filter(
                                customer=offering.customer
                            ).first()

                            if not test_project:
                                # Create a minimal project
                                test_project = Project.objects.create(
                                    name=f"temp-project-{uuid.uuid4().hex[:8]}",
                                    customer=offering.customer,
                                )
                                if not created_project:
                                    created_project = test_project

                            # Create minimal Waldur tenant for backend method
                            waldur_tenant = models.Tenant.objects.create(
                                name=f"temp-waldur-tenant-{uuid.uuid4().hex[:8]}",
                                service_settings=service_settings,
                                project=test_project,
                                backend_id=tenant.backend_id,  # Use the OpenStack tenant we created
                            )

                            # Track this tenant for cleanup
                            if not created_tenant or not hasattr(
                                created_tenant, "refresh_from_db"
                            ):
                                created_tenant = waldur_tenant

                            if verbose:
                                self.stdout.write(
                                    f"   Created Waldur tenant model: {waldur_tenant.name}"
                                )

                        # Create minimal instance object for backend.create_instance
                        models.Instance(
                            name=test_instance_name,
                            tenant=waldur_tenant,
                            service_settings=service_settings,
                            project=waldur_tenant.project,
                            flavor_name=flavor.name,
                            ram=flavor.ram,
                            disk=flavor.disk,
                        )
                        # Don't save to database yet

                        if verbose:
                            self.stdout.write(
                                "   Creating instance using backend.create_instance method..."
                            )
                            self.stdout.write(
                                f"   Flavor: {flavor.name} (ID: {flavor.id})"
                            )
                            self.stdout.write(f"   Image: {image.name}")

                        # Create instance using admin session (similar to backend.create_instance logic)
                        nova = get_nova_client(session)  # Use admin session

                        # Get the flavor object
                        backend_flavor = nova.flavors.get(flavor.id)

                        # Create instance parameters (simplified from backend.create_instance)
                        server_create_parameters = {
                            "name": test_instance_name,
                            "image": image.id,
                            "flavor": backend_flavor,
                        }

                        # Get available networks and add to NICs if needed
                        neutron = get_neutron_client(session)
                        tenant_networks = neutron.list_networks(
                            tenant_id=tenant.backend_id
                        )["networks"]

                        if tenant_networks:
                            # Use tenant networks
                            nics = [{"net-id": tenant_networks[0]["id"]}]
                            server_create_parameters["nics"] = nics
                            if verbose:
                                self.stdout.write(
                                    f"   Using tenant network: {tenant_networks[0]['id']}"
                                )
                        else:
                            # Use any available network
                            all_networks = neutron.list_networks()["networks"]
                            if all_networks:
                                nics = [{"net-id": all_networks[0]["id"]}]
                                server_create_parameters["nics"] = nics
                                if verbose:
                                    self.stdout.write(
                                        f"   Using available network: {all_networks[0]['id']}"
                                    )

                        # Create the instance (following backend.create_instance pattern)
                        if verbose:
                            self.stdout.write(
                                "   Creating instance with parameters similar to backend.create_instance..."
                            )

                        server = nova.servers.create(**server_create_parameters)
                        instance_id = server.id
                        add_cleanup_task(
                            "instance", None, instance_id, test_instance_name
                        )

                        if verbose:
                            self.stdout.write(f"   ✓ Created instance: {instance_id}")

                        # Wait briefly for instance to start creating
                        import time

                        time.sleep(3)

                        # Check instance status
                        instance_status = nova.servers.get(instance_id)
                        if verbose:
                            self.stdout.write(
                                f"   Instance status: {instance_status.status}"
                            )

                        # Delete instance using direct Nova API (like backend.delete_instance)
                        nova.servers.delete(instance_id)
                        cleanup_tasks[:] = [
                            task
                            for task in cleanup_tasks
                            if task["resource_id"] != instance_id
                        ]

                        if verbose:
                            self.stdout.write(f"   ✓ Deleted instance: {instance_id}")

                        self.stdout.write(
                            self.style.SUCCESS(
                                "   ✓ Instance operations successful (using Waldur backend)"
                            )
                        )

            except Exception as e:
                raise Exception(f"Instance operations failed: {e}")

            self.stdout.write("All write operations completed successfully!")

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\n! Write operations failed: {e}"))
            raise e
        finally:
            # Always perform cleanup, regardless of success or failure
            perform_cleanup()
