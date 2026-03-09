from django.core.management.base import BaseCommand
from django.utils import timezone

from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OfferingStates, ResourceStates


class Command(BaseCommand):
    help = (
        "Archive an offering and terminate all its resources (including child offerings' resources), "
        "or clean up invoice items for already-terminated resources."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            choices=["terminate", "cleanup-invoices"],
            help=(
                "terminate: archive offering(s) and terminate all non-terminated resources. "
                "cleanup-invoices: remove current month invoice items for terminated resources."
            ),
        )
        parser.add_argument(
            "offering_uuid",
            help="UUID of the parent offering to process.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="List affected resources/items without making changes.",
        )

    def handle(self, action, offering_uuid, dry_run, *args, **options):
        try:
            offering = marketplace_models.Offering.objects.get(uuid=offering_uuid)
        except marketplace_models.Offering.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(f"Offering with UUID {offering_uuid} not found.")
            )
            return
        except ValueError:
            self.stderr.write(self.style.ERROR(f"Invalid UUID format: {offering_uuid}"))
            return

        # Collect parent + child offerings
        all_offerings = [offering] + list(offering.children.all())

        self.stdout.write(
            f"Parent offering: {offering.uuid} — {offering.name} (type: {offering.type})"
        )
        if len(all_offerings) > 1:
            self.stdout.write(f"Child offerings ({len(all_offerings) - 1}):")
            for child in all_offerings[1:]:
                self.stdout.write(f"  {child.uuid} — {child.name} (type: {child.type})")

        if action == "terminate":
            self._handle_terminate(all_offerings, dry_run)
        elif action == "cleanup-invoices":
            self._handle_cleanup_invoices(all_offerings, dry_run)

    def _handle_terminate(self, all_offerings, dry_run):
        resources = (
            marketplace_models.Resource.objects.filter(
                offering__in=all_offerings,
            )
            .exclude(
                state=ResourceStates.TERMINATED,
            )
            .select_related("offering", "project")
        )

        self.stdout.write(f"\nResources to terminate: {resources.count()}")
        if not resources.exists():
            self.stdout.write(self.style.SUCCESS("No resources to terminate."))
            self._archive_offerings(all_offerings, dry_run)
            return

        for resource in resources:
            self.stdout.write(
                f"  [{resource.get_state_display()}] {resource.uuid} — "
                f"{resource.name} (offering: {resource.offering.name}, "
                f"project: {resource.project.name})"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] No changes made."))
            return

        terminated_count = 0
        for resource in resources:
            try:
                # Two-step transition to trigger billing cleanup via signal handler.
                # The handler process_billing_on_resource_save checks that
                # previous state was TERMINATING before calling _terminate().
                if resource.state != ResourceStates.TERMINATING:
                    resource.set_state_terminating()
                    resource.save(update_fields=["state"])

                resource.set_state_terminated()
                resource.save(update_fields=["state"])
                terminated_count += 1
                self.stdout.write(f"  Terminated: {resource.uuid} — {resource.name}")
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f"  Failed to terminate {resource.uuid}: {e}")
                )

        self._archive_offerings(all_offerings, dry_run)

        self.stdout.write(
            self.style.SUCCESS(f"\nDone. Terminated {terminated_count} resources.")
        )

    def _archive_offerings(self, all_offerings, dry_run):
        self.stdout.write("\nArchiving offerings:")
        for offering in all_offerings:
            if offering.state == OfferingStates.ARCHIVED:
                self.stdout.write(
                    f"  Already archived: {offering.uuid} — {offering.name}"
                )
                continue
            if dry_run:
                self.stdout.write(
                    f"  [DRY RUN] Would archive: {offering.uuid} — {offering.name}"
                )
            else:
                offering.archive()
                offering.save(update_fields=["state"])
                self.stdout.write(f"  Archived: {offering.uuid} — {offering.name}")

    def _handle_cleanup_invoices(self, all_offerings, dry_run):
        now = timezone.now()

        terminated_resource_ids = marketplace_models.Resource.objects.filter(
            offering__in=all_offerings,
            state=ResourceStates.TERMINATED,
        ).values_list("id", flat=True)

        items = invoice_models.InvoiceItem.objects.filter(
            resource_id__in=terminated_resource_ids,
            invoice__state__in=invoice_models.Invoice.States.MUTABLE_STATES,
            invoice__year=now.year,
            invoice__month=now.month,
        ).select_related("invoice", "resource")

        self.stdout.write(f"\nInvoice items to remove: {items.count()}")
        if not items.exists():
            self.stdout.write(self.style.SUCCESS("No invoice items to clean up."))
            return

        for item in items:
            resource_info = (
                f"{item.resource.uuid} — {item.resource.name}"
                if item.resource
                else "N/A"
            )
            self.stdout.write(
                f"  Item: {item.uuid} | price: {item.price} | "
                f"resource: {resource_info} | "
                f"invoice: {item.invoice.customer} {item.invoice.year}-{item.invoice.month:02d}"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] No changes made."))
            return

        affected_invoice_ids = set(items.values_list("invoice_id", flat=True))
        deleted_count, _ = items.delete()

        for invoice in invoice_models.Invoice.objects.filter(
            id__in=affected_invoice_ids
        ):
            invoice.update_cache()

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. Deleted {deleted_count} invoice items, "
                f"updated {len(affected_invoice_ids)} invoices."
            )
        )
