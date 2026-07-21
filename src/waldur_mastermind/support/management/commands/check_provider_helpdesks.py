from django.core.management.base import BaseCommand

from waldur_mastermind.support import models
from waldur_mastermind.support.backend import get_backend_for_provider


class Command(BaseCommand):
    help = "Check connectivity of all active provider helpdesks."

    def handle(self, *args, **options):
        helpdesks = models.ProviderHelpdesk.objects.filter(is_active=True)
        if not helpdesks.exists():
            self.stdout.write("No active provider helpdesks found.")
            return

        for helpdesk in helpdesks:
            provider_name = str(helpdesk.service_provider)
            self.stdout.write(
                f"Checking {provider_name} ({helpdesk.backend_type})...", ending=""
            )
            try:
                backend = get_backend_for_provider(helpdesk)
                # BasicBackend is always healthy; EmailBackend can be checked too
                if hasattr(backend, "health_check"):
                    backend.health_check()
                self.stdout.write(self.style.SUCCESS(" OK"))
                helpdesk.last_health_status = "ok"
            except Exception as e:
                self.stdout.write(self.style.ERROR(f" FAILED: {e}"))
                helpdesk.last_health_status = "error"
                helpdesk.failed_routing_count += 1

            from django.utils import timezone

            helpdesk.last_health_check = timezone.now()
            helpdesk.save(
                update_fields=[
                    "last_health_check",
                    "last_health_status",
                    "failed_routing_count",
                ]
            )
