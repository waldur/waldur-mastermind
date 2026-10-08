from constance import config
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError

from waldur_core.core.models import User
from waldur_mastermind.matrix_chat import matrix_client
from waldur_mastermind.matrix_chat.models import MatrixUserProfile

# The admin API answers the appservice only once the bot is a homeserver admin.
UNVERIFIED_ADMIN = (
    "Could not check for homeserver admin accounts: the bot is not a homeserver "
    "admin. A Waldur user must not hold a homeserver admin's account."
)


class Command(BaseCommand):
    help = (
        "Link a Waldur user to a Matrix account that already exists on the "
        "homeserver. Provisioning refuses to take over accounts this Waldur did "
        "not create; use this once you know the account belongs to the user."
    )

    def add_arguments(self, parser):
        parser.add_argument("username", nargs="?", help="Waldur username")
        parser.add_argument(
            "matrix_user_id", nargs="?", help="e.g. @alice:chat.example.org"
        )
        parser.add_argument(
            "--all",
            dest="link_all",
            action="store_true",
            help=(
                "Link every user without a Matrix profile to the existing account "
                "with their generated Matrix ID. Only for a Waldur database that "
                "was restored or reset against a homeserver whose accounts all "
                "belong to this Waldur's users."
            ),
        )

    def handle(self, username=None, matrix_user_id=None, link_all=False, **options):
        if link_all:
            if username or matrix_user_id:
                raise CommandError("--all takes no username or Matrix ID")
            self._link_all()
            return
        if not (username and matrix_user_id):
            raise CommandError("Give a username and a Matrix ID, or --all")

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"No active Waldur user {username}")

        domain = config.MATRIX_HOMESERVER_DOMAIN
        if not matrix_user_id.startswith("@") or not matrix_user_id.endswith(
            f":{domain}"
        ):
            raise CommandError(f"{matrix_user_id} is not an account on {domain}")
        if matrix_user_id == matrix_client.get_bot_user_id():
            raise CommandError(f"{matrix_user_id} is the bot")

        existing = MatrixUserProfile.objects.filter(user=user).first()
        if existing:
            raise CommandError(
                f"{username} is already linked to {existing.matrix_user_id}"
            )
        holder = (
            MatrixUserProfile.objects.filter(matrix_user_id=matrix_user_id)
            .select_related("user")
            .first()
        )
        if holder:
            raise CommandError(
                f"{matrix_user_id} is already linked to {holder.user.username}"
            )

        try:
            exists = matrix_client.account_exists(matrix_user_id)
            admin = exists and matrix_client.is_homeserver_admin(matrix_user_id)
        except matrix_client.MatrixClientError as e:
            raise CommandError(str(e))
        if not exists:
            raise CommandError(f"{matrix_user_id} does not exist on the homeserver")
        if admin:
            raise CommandError(
                f"{matrix_user_id} is a homeserver admin; a Waldur user may not hold it"
            )
        if admin is None:
            self.stderr.write(UNVERIFIED_ADMIN)

        try:
            self._link(user, matrix_user_id)
        except IntegrityError:
            # A provisioning or another link got there between the checks.
            raise CommandError(f"{username} or {matrix_user_id} was linked meanwhile")
        self.stdout.write(self.style.SUCCESS(f"Linked {username} to {matrix_user_id}"))

    def _link_all(self):
        linked = missing = admins = taken_ids = shared = failed = 0
        unverified = False
        bot_user_id = matrix_client.get_bot_user_id()
        taken = set(MatrixUserProfile.objects.values_list("matrix_user_id", flat=True))
        derived = {}
        for user in User.objects.filter(matrix_profile__isnull=True):
            derived.setdefault(matrix_client.generate_matrix_user_id(user), []).append(
                user
            )
        for matrix_user_id, users in derived.items():
            if len(users) > 1:
                # Nothing here says which of them the account belongs to, and
                # linking whichever comes first would sign them in as another.
                names = ", ".join(u.username for u in users)
                shared += len(users)
                self.stderr.write(
                    f"{matrix_user_id} is derived by {names}; not linked. "
                    "Link its owner with link_matrix_account <username> "
                    f"{matrix_user_id}"
                )
                continue
            user = users[0]
            if matrix_user_id == bot_user_id or matrix_user_id in taken:
                # Another user's, or the bot's: two users derive one ID.
                taken_ids += 1
                self.stderr.write(
                    f"{user.username}: {matrix_user_id} is already taken; not linked"
                )
                continue
            try:
                if not matrix_client.account_exists(matrix_user_id):
                    missing += 1
                    continue
                admin = matrix_client.is_homeserver_admin(matrix_user_id)
                if admin:
                    admins += 1
                    self.stderr.write(
                        f"{user.username}: {matrix_user_id} is a homeserver admin; "
                        "not linked"
                    )
                    continue
                unverified = unverified or admin is None
                self._link(user, matrix_user_id)
            except (matrix_client.MatrixClientError, IntegrityError) as e:
                failed += 1
                self.stderr.write(f"{user.username}: {e}")
                continue
            linked += 1
        if unverified:
            self.stderr.write(UNVERIFIED_ADMIN)
        self.stdout.write(
            f"{linked} linked, {missing} without an account, "
            f"{admins} homeserver admin(s) skipped, {taken_ids} already taken, "
            f"{shared} sharing an ID, {failed} failed"
        )

    def _link(self, user, matrix_user_id):
        profile = MatrixUserProfile.objects.create(
            user=user, matrix_user_id=matrix_user_id
        )
        profile.mark_provisioned()
        try:
            matrix_client.set_display_name(
                matrix_user_id, user.full_name or user.username
            )
        except Exception as e:
            self.stderr.write(
                f"Could not set the display name of {matrix_user_id}: {e}"
            )
