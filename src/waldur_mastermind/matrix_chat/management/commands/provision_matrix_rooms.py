from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError

from waldur_core.structure.models import Project
from waldur_mastermind.matrix_chat import matrix_client, models, room_provisioning


class Command(BaseCommand):
    help = (
        "Create Matrix rooms for existing projects that do not have one yet. "
        "Provisioning itself runs asynchronously via Celery, and each room "
        "invites every project member, so on large deployments run it in "
        "batches with --limit to avoid homeserver rate limits."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--customer",
            type=str,
            default="",
            help="Limit to projects of a single customer, given by UUID.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after provisioning this many rooms (0 = no limit).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the projects that would get a room, without creating any.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if options["limit"] < 0:
            raise CommandError("--limit must be 0 (no limit) or a positive number.")

        if not matrix_client.is_enabled() and not dry_run:
            raise CommandError(
                "Matrix chat is disabled — enable MATRIX_ENABLED before provisioning, "
                "or re-run with --dry-run to preview."
            )

        projects = Project.available_objects.all().select_related("customer")

        customer_uuid = options["customer"]
        if customer_uuid:
            projects = projects.filter(customer__uuid=customer_uuid)
            if not projects.exists():
                raise CommandError(
                    f"No projects found for customer {customer_uuid}. "
                    "Check the UUID — an unknown customer looks the same as an empty one."
                )

        # Any room, including archived ones, blocks creation: MatrixRoom is
        # unique per scope, matching what the eligible_projects endpoint shows.
        project_ct = ContentType.objects.get_for_model(Project)
        taken_project_ids = models.MatrixRoom.objects.filter(
            content_type=project_ct
        ).values_list("object_id", flat=True)
        projects = projects.exclude(id__in=taken_project_ids).order_by(
            "customer__name", "name"
        )

        limit = options["limit"]
        if limit:
            projects = projects[:limit]

        created = 0
        skipped = 0
        for project in projects:
            label = f"{project.customer.name} / {project.name} ({project.uuid})"
            if dry_run:
                self.stdout.write(f"would create room for {label}")
                created += 1
                continue

            room = room_provisioning.provision_project_room(project)
            if room is None:
                self.stdout.write(self.style.WARNING(f"skipped {label}: room exists"))
                skipped += 1
                continue

            created += 1
            self.stdout.write(f"created room {room.uuid} for {label}")

        verb = "would create" if dry_run else "created"
        summary = f"{verb} {created} room(s)"
        if skipped:
            summary += f", skipped {skipped}"
        self.stdout.write(self.style.SUCCESS(summary))
