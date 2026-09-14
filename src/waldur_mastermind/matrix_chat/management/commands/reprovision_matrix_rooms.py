from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.matrix_chat import matrix_client, models, tasks


class Command(BaseCommand):
    help = (
        "Reset every active Matrix room and provisioned user profile so the "
        "homeserver rebuilds them. Use after moving to a new homeserver, whose "
        "room ids and user tokens are different from the old one's. Do not run "
        "it against the homeserver the rooms already live on: old rooms are not "
        "deleted, so each one keeps its history while Waldur replaces it with "
        "an empty room. "
        "Equivalent to POST /api/admin/matrix/reprovision/, for deployments "
        "where reaching the API as staff is harder than reaching a shell."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be reset without writing anything",
        )
        parser.add_argument(
            "-y",
            "--yes",
            action="store_true",
            help="Do not prompt for confirmation",
        )

    def handle(self, *args, **options):
        rooms = models.MatrixRoom.objects.filter(state=models.RoomStates.ACTIVE).count()
        users = models.MatrixUserProfile.objects.filter(provisioned=True).count()

        # Ahead of the enabled check: a dry run queues nothing, and counting
        # before the new homeserver is switched on is part of a migration.
        if options["dry_run"]:
            self.stdout.write(
                f"Would reprovision {rooms} room(s) and reset {users} user profile(s)."
            )
            return

        # A reprovision against no homeserver leaves every room in 'creating'
        # forever, because the tasks it queues have nothing to talk to.
        if not matrix_client.is_enabled():
            raise CommandError(
                "Matrix chat is disabled or the homeserver is not configured. "
                "Set MATRIX_ENABLED, MATRIX_HOMESERVER_URL and "
                "MATRIX_APPSERVICE_AS_TOKEN before reprovisioning."
            )

        if rooms == 0 and users == 0:
            self.stdout.write(
                self.style.WARNING(
                    "Nothing to reprovision: no active rooms, no provisioned profiles."
                )
            )
            return

        if not options["yes"]:
            # The room ids, aliases and user access tokens are dropped, and only
            # a working homeserver can mint replacements.
            self.stdout.write(
                f"This resets {rooms} room(s) and {users} user profile(s), "
                "discarding their homeserver ids and access tokens. Only run it "
                "after moving to a new homeserver: on the one the rooms already "
                "live on, each old room stays behind with its history and is "
                "replaced by an empty one."
            )
            try:
                answer = input("Continue? [y/N] ")
            except EOFError:
                # A Kubernetes Job or a cron run has no stdin. Failing here is
                # right, but the bare EOFError traceback reads like a bug in
                # the command rather than a missing flag.
                raise CommandError(
                    "No terminal to confirm on. Pass -y to reprovision "
                    "without prompting, or --dry-run to see what it would do."
                )
            if answer.strip().lower() not in ("y", "yes"):
                raise CommandError("Aborted.")

        room_count, user_count = tasks.reprovision_rooms()

        self.stdout.write(
            self.style.SUCCESS(
                f"Reprovisioned {room_count} room(s), reset {user_count} "
                "user profile(s). Room creation runs in the background; watch "
                "the room states to confirm they leave 'creating'."
            )
        )
