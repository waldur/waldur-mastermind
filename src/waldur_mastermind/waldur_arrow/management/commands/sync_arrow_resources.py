"""
Management command to sync Arrow IAAS subscriptions to Waldur Resources.

This command:
1. Fetches IAAS billing data from Arrow
2. Aggregates consumption by Vendor Subscription ID
3. Creates/updates Waldur Resources matching backend_id
4. Updates resource report field with drill-down billing data
"""

import logging
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.waldur_arrow import models
from waldur_mastermind.waldur_arrow.backend import ArrowClient, ArrowCredentials

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync Arrow IAAS subscriptions to Waldur Resources"

    def add_arguments(self, parser):
        parser.add_argument(
            "--period-from",
            type=str,
            default=None,
            help="Start period in YYYY-MM format (default: 6 months ago, Arrow max)",
        )
        parser.add_argument(
            "--period-to",
            type=str,
            default=None,
            help="End period in YYYY-MM format (default: current month)",
        )
        parser.add_argument(
            "--customer-uuid",
            type=str,
            help="Waldur Customer UUID to create resources under",
        )
        parser.add_argument(
            "--project-uuid",
            type=str,
            help="Waldur Project UUID to create resources under",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--create-offering",
            action="store_true",
            help="Create Arrow Azure offering if it doesn't exist",
        )
        parser.add_argument(
            "--force-import",
            action="store_true",
            help=(
                "Auto-create Waldur Customers and Projects from Arrow data. "
                "Each Arrow customer becomes a Waldur Customer with an "
                "'Arrow Azure Subscriptions' project."
            ),
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        force_import = options.get("force_import", False)

        self.stdout.write(f"Dry run: {self.dry_run}")
        self.stdout.write(f"Force import: {force_import}")

        # Get Arrow settings
        settings = models.ArrowSettings.get_active()
        if not settings:
            raise CommandError("No active Arrow settings found")

        self.stdout.write(f"Using Arrow settings: {settings.api_url}")

        # Calculate default periods (last 6 months - Arrow's max allowed)
        period_from = options["period_from"]
        period_to = options["period_to"]

        if not period_from or not period_to:
            from datetime import date

            today = date.today()

            if not period_to:
                period_to = f"{today.year:04d}-{today.month:02d}"

            if not period_from:
                # 6 months ago (Arrow max range)
                year = today.year
                month = today.month - 5  # 6 months total
                while month < 1:
                    month += 12
                    year -= 1
                period_from = f"{year:04d}-{month:02d}"

        self.stdout.write(f"Period: {period_from} to {period_to}")

        if self.dry_run:
            # Dry run mode - just show what would happen
            self._dry_run_sync(settings, period_from, period_to, force_import)
            return

        if force_import:
            # Force import mode - use the task which handles customer/project creation
            from waldur_mastermind.waldur_arrow import tasks

            self.stdout.write("\nRunning force import...")
            result = tasks.sync_arrow_resources(
                period_from=period_from,
                period_to=period_to,
                force_import=True,
            )

            if "error" in result:
                raise CommandError(result["error"])

            self.stdout.write(self.style.SUCCESS("\n=== Sync Complete ==="))
            self.stdout.write(f"Resources synced: {result.get('synced', 0)}")
            self.stdout.write(f"Resources created: {result.get('created', 0)}")
            self.stdout.write(f"Resources updated: {result.get('updated', 0)}")
            self.stdout.write(
                f"Customers created: {result.get('customers_created', 0)}"
            )
            self.stdout.write(f"Projects created: {result.get('projects_created', 0)}")
            if result.get("errors"):
                self.stdout.write(
                    self.style.WARNING(f"Errors: {len(result['errors'])}")
                )
                for err in result["errors"]:
                    self.stdout.write(f"  - {err}")
        else:
            # Standard mode - need offering and project
            offering = self._get_or_create_offering(options)
            if not offering:
                raise CommandError(
                    "No offering found/created. Use --create-offering or --force-import"
                )

            self.stdout.write(f"Using offering: {offering.name} ({offering.uuid})")

            project = self._get_project(options, offering)
            if not project:
                raise CommandError(
                    "No project specified or found. Use --project-uuid or --force-import"
                )

            self.stdout.write(f"Using project: {project.name} ({project.uuid})")

            # Fetch and sync
            credentials = ArrowCredentials(
                api_url=settings.api_url,
                api_key=settings.api_key,
            )
            client = ArrowClient(credentials)

            self.stdout.write("Fetching billing data...")

            try:
                export_data = client.export_billing_all_pages(
                    export_type_reference=settings.export_type_reference,
                    period_from=period_from,
                    period_to=period_to,
                )
            except Exception as e:
                raise CommandError(f"Failed to fetch Arrow billing: {e}")

            subscriptions = self._aggregate_subscriptions(export_data)
            self.stdout.write(f"Found {len(subscriptions)} IAAS subscriptions")

            for sub_id, info in subscriptions.items():
                self._sync_resource(sub_id, info, offering, project)

            self.stdout.write(self.style.SUCCESS("Sync completed"))

    def _dry_run_sync(self, settings, period_from, period_to, force_import):
        """Show what would be done in dry run mode."""
        credentials = ArrowCredentials(
            api_url=settings.api_url,
            api_key=settings.api_key,
        )
        client = ArrowClient(credentials)

        self.stdout.write("\nFetching billing data...")
        try:
            export_data = client.export_billing_all_pages(
                export_type_reference=settings.export_type_reference,
                period_from=period_from,
                period_to=period_to,
            )
        except Exception as e:
            raise CommandError(f"Failed to fetch Arrow billing: {e}")

        subscriptions = self._aggregate_subscriptions(export_data)

        # Group by customer
        customers = {}
        for sub_id, info in subscriptions.items():
            cust = info["customer"]
            if cust not in customers:
                customers[cust] = []
            customers[cust].append((sub_id, info))

        self.stdout.write(
            f"\n=== DRY RUN: Would sync {len(subscriptions)} subscriptions ===\n"
        )

        for customer_name, subs in customers.items():
            if force_import:
                self.stdout.write(self.style.WARNING(f"Customer: {customer_name}"))
                self.stdout.write("  Would create/update Waldur Customer")
                self.stdout.write(
                    "  Would create/update Project: 'Arrow Azure Subscriptions'"
                )
            else:
                self.stdout.write(f"Customer: {customer_name}")

            for sub_id, info in subs:
                existing = marketplace_models.Resource.objects.filter(
                    backend_id=sub_id
                ).exists()
                action = "UPDATE" if existing else "CREATE"
                self.stdout.write(
                    f"  [{action}] {info['name']} (backend_id={sub_id[:20]}...)"
                )
                self.stdout.write(f"         Sell: EUR {info['sell_total']:.2f}")
            self.stdout.write("")

    def _get_or_create_offering(self, options):
        """Get or create Arrow Azure offering."""
        offering = marketplace_models.Offering.objects.filter(
            name__icontains="Arrow Azure",
        ).first()

        if offering:
            return offering

        if not options.get("create_offering"):
            self.stdout.write(
                self.style.WARNING(
                    "No Arrow Azure offering found. Use --create-offering to create one."
                )
            )
            return None

        if self.dry_run:
            self.stdout.write("Would create Arrow Azure offering")
            return marketplace_models.Offering.objects.first()

        # Get or create category
        category, _ = marketplace_models.Category.objects.get_or_create(
            title="Cloud Infrastructure",
            defaults={"description": "Cloud infrastructure services"},
        )

        # Get customer for offering
        customer_uuid = options.get("customer_uuid")
        if customer_uuid:
            customer = structure_models.Customer.objects.filter(
                uuid=customer_uuid
            ).first()
        else:
            customer = structure_models.Customer.objects.first()

        if not customer:
            raise CommandError("No customer found for offering")

        offering = marketplace_models.Offering.objects.create(
            name="Arrow Azure Subscriptions",
            description="Azure subscriptions managed through Arrow",
            category=category,
            customer=customer,
            type="Support.OfferingTemplate",
            state=marketplace_models.Offering.States.ACTIVE,
        )

        self.stdout.write(f"Created offering: {offering.name}")
        return offering

    def _get_project(self, options, offering):
        """Get project for resources."""
        project_uuid = options.get("project_uuid")
        if project_uuid:
            return structure_models.Project.objects.filter(uuid=project_uuid).first()

        # Try to find a project under the offering customer
        return structure_models.Project.objects.filter(
            customer=offering.customer
        ).first()

    def _aggregate_subscriptions(self, export_data):
        """
        Aggregate IAAS billing data by Vendor Subscription ID.

        Returns dict: {subscription_id: {name, customer, sell_total, buy_total, periods}}
        """
        headers = export_data.get("headers", [])
        values = export_data.get("values", [])

        cols = {h: i for i, h in enumerate(headers)}

        class_idx = cols.get("Classification", -1)
        vendor_sub_idx = cols.get("Vendor Subscription ID", -1)
        friendly_name_idx = cols.get("Friendly Name", -1)
        sell_price_idx = cols.get("Customer Total Price", -1)
        buy_price_idx = cols.get("Total Wholesale Price", -1)
        customer_idx = cols.get("End User Company Name", -1)
        report_period_idx = cols.get("Report Period", -1)
        description_idx = cols.get("Description", -1)
        vendor_idx = cols.get("Vendor Name", -1)
        offer_name_idx = cols.get("Offer Name", -1)

        subscriptions = {}

        for row in values:
            classification = row[class_idx] if class_idx >= 0 else ""
            if classification != "IAAS":
                continue

            sub_id = row[vendor_sub_idx] if vendor_sub_idx >= 0 else None
            if not sub_id:
                continue

            name = row[friendly_name_idx] if friendly_name_idx >= 0 else "Unknown"
            sell_price = self._parse_decimal(
                row[sell_price_idx] if sell_price_idx >= 0 else 0
            )
            buy_price = self._parse_decimal(
                row[buy_price_idx] if buy_price_idx >= 0 else 0
            )
            customer = row[customer_idx] if customer_idx >= 0 else "Unknown"
            period = row[report_period_idx] if report_period_idx >= 0 else "Unknown"
            description = row[description_idx] if description_idx >= 0 else ""
            vendor = row[vendor_idx] if vendor_idx >= 0 else ""
            offer = row[offer_name_idx] if offer_name_idx >= 0 else ""

            if sub_id not in subscriptions:
                subscriptions[sub_id] = {
                    "name": name,
                    "customer": customer,
                    "vendor": vendor,
                    "sell_total": Decimal("0"),
                    "buy_total": Decimal("0"),
                    "periods": {},
                }

            subscriptions[sub_id]["sell_total"] += sell_price
            subscriptions[sub_id]["buy_total"] += buy_price

            if period not in subscriptions[sub_id]["periods"]:
                subscriptions[sub_id]["periods"][period] = {
                    "sell": Decimal("0"),
                    "buy": Decimal("0"),
                    "items": [],
                }

            subscriptions[sub_id]["periods"][period]["sell"] += sell_price
            subscriptions[sub_id]["periods"][period]["buy"] += buy_price
            subscriptions[sub_id]["periods"][period]["items"].append(
                {
                    "description": description,
                    "offer": offer,
                    "sell": str(sell_price),
                    "buy": str(buy_price),
                }
            )

        return subscriptions

    def _parse_decimal(self, value):
        """Parse a value to Decimal."""
        if isinstance(value, Decimal):
            return value
        if isinstance(value, int | float):
            return Decimal(str(value))
        if isinstance(value, str):
            # Remove currency symbols and thousand separators
            cleaned = value.replace("€", "").replace(",", "").strip()
            try:
                return Decimal(cleaned) if cleaned else Decimal("0")
            except Exception:
                return Decimal("0")
        return Decimal("0")

    def _sync_resource(self, sub_id, info, offering, project):
        """Sync a single subscription to a Waldur Resource."""
        self.stdout.write(f"\nSyncing subscription: {sub_id}")
        self.stdout.write(f"  Name: {info['name']}")
        self.stdout.write(f"  Customer: {info['customer']}")
        self.stdout.write(f"  Sell Total: EUR {info['sell_total']:.2f}")

        # Build report for drill-down
        report = []
        for period, pdata in sorted(info["periods"].items()):
            body_lines = [
                f"Sell: EUR {pdata['sell']:.2f}",
                f"Buy: EUR {pdata['buy']:.2f}",
                f"Vendor: {info['vendor']}",
                "",
                "Line items:",
            ]
            for item in pdata["items"]:
                body_lines.append(f"  - {item['description']}: EUR {item['sell']}")

            report.append(
                {
                    "header": f"Arrow Billing - {period}",
                    "body": "\n".join(body_lines),
                }
            )

        if self.dry_run:
            self.stdout.write(
                f"  Would create/update resource with backend_id={sub_id}"
            )
            self.stdout.write(f"  Report sections: {len(report)}")
            return

        with transaction.atomic():
            # Find existing resource by backend_id
            resource = marketplace_models.Resource.objects.filter(
                backend_id=sub_id
            ).first()

            if resource:
                self.stdout.write(f"  Updating existing resource: {resource.uuid}")
                resource.name = info["name"]
                resource.report = report
                resource.current_usages = {
                    "arrow_sell_total": str(info["sell_total"]),
                    "arrow_buy_total": str(info["buy_total"]),
                }
                resource.save()
            else:
                self.stdout.write("  Creating new resource")
                resource = marketplace_models.Resource.objects.create(
                    name=info["name"],
                    offering=offering,
                    project=project,
                    backend_id=sub_id,
                    state=marketplace_models.Resource.States.OK,
                    report=report,
                    current_usages={
                        "arrow_sell_total": str(info["sell_total"]),
                        "arrow_buy_total": str(info["buy_total"]),
                    },
                )

            self.stdout.write(self.style.SUCCESS(f"  Resource synced: {resource.uuid}"))
