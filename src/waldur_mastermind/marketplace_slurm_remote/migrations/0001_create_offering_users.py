import re
import unicodedata
from enum import Enum

from django.db import migrations

USERNAME_ANONYMIZED_POSTFIX_LENGTH = 5

USERNAME_POSTFIX_LENGTH = 2


class OfferingStates:
    DRAFT = 1
    ACTIVE = 2
    PAUSED = 3
    ARCHIVED = 4


class ResourceStates:
    CREATING = 1
    OK = 2
    ERRED = 3
    UPDATING = 4
    TERMINATING = 5
    TERMINATED = 6


class UsernameGenerationPolicy(Enum):
    SERVICE_PROVIDER = (
        "service_provider"  # SP should manually submit username for the offering users
    )
    ANONYMIZED = "anonymized"  # Usernames are generated with <prefix>_<number>, e.g. "anonym_0001".
    # The prefix must be specified in offering.plugin_options as "username_anonymized_prefix"
    FULL_NAME = "full_name"  # Usernames are constructed using first and last name of users, e.g. "john.doe"
    WALDUR_USERNAME = "waldur_username"  # Using username field of User model
    FREEIPA = "freeipa"  # Using username field of waldur_freeipa.Profile model


def sanitize_name(name):
    name = name.strip().lower()
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"\W+", "", name)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return name


def create_offering_users_for_existing_users(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    Resource = apps.get_model("marketplace", "Resource")
    OfferingUser = apps.get_model("marketplace", "OfferingUser")
    Project = apps.get_model("structure", "Project")
    User = apps.get_model("core", "User")
    Profile = apps.get_model("waldur_freeipa", "Profile")

    def create_anonymized_username(offering):
        prefix = offering.plugin_options.get("username_anonymized_prefix")
        previous_users = OfferingUser.objects.filter(offering=offering).order_by(
            "username"
        )

        if previous_users.exists():
            last_username = previous_users.last().username
            last_number = int(last_username[-USERNAME_ANONYMIZED_POSTFIX_LENGTH:])
            number = str(last_number + 1).zfill(USERNAME_ANONYMIZED_POSTFIX_LENGTH)
        else:
            number = "0".zfill(USERNAME_ANONYMIZED_POSTFIX_LENGTH)

        return f"{prefix}{number}"

    def create_username_from_full_name(user, offering):
        first_name = sanitize_name(user.first_name)
        last_name = sanitize_name(user.last_name)

        username_raw = f"{first_name}.{last_name}"
        previous_users = OfferingUser.objects.filter(
            offering=offering, username__istartswith=username_raw
        ).order_by("username")

        if previous_users.exists():
            last_username = previous_users.last().username
            last_number = int(last_username[-USERNAME_POSTFIX_LENGTH:])
            number = str(last_number + 1).zfill(USERNAME_POSTFIX_LENGTH)
        else:
            number = "0".zfill(USERNAME_POSTFIX_LENGTH)

        return f"{username_raw}.{number}"

    def create_username_from_freeipa_profile(user):
        profiles = Profile.objects.filter(user=user)
        if profiles.count() == 0:
            print("There is no FreeIPA profile for user %s", user)
            return ""
        else:
            return profiles.first().username

    def generate_username(user, offering):
        username_generation_policy = offering.plugin_options.get(
            "username_generation_policy"
        )

        if (
            username_generation_policy
            == UsernameGenerationPolicy.SERVICE_PROVIDER.value
        ):
            return ""

        if username_generation_policy == UsernameGenerationPolicy.ANONYMIZED.value:
            return create_anonymized_username(offering)

        if username_generation_policy == UsernameGenerationPolicy.FULL_NAME.value:
            return create_username_from_full_name(user, offering)

        if username_generation_policy == UsernameGenerationPolicy.WALDUR_USERNAME.value:
            return user.username

        if username_generation_policy == UsernameGenerationPolicy.FREEIPA.value:
            return create_username_from_freeipa_profile(user)

        return ""

    def user_offerings_mapping(offerings):
        resources = Resource.objects.filter(
            state=ResourceStates.OK, offering__in=offerings
        )
        resource_ids = resources.values_list("id", flat=True)

        project_ids = resources.values_list("project_id", flat=True)
        projects = Project.objects.filter(id__in=project_ids)

        user_offerings_set = set()

        for project in projects:
            users = User.objects.filter(
                projectpermission__project=project, projectpermission__is_active=True
            )

            project_resources = project.resource_set.filter(id__in=resource_ids)
            project_offering_ids = project_resources.values_list(
                "offering_id", flat=True
            )
            project_offerings = Offering.objects.filter(id__in=project_offering_ids)

            for user in users:
                for offering in project_offerings:
                    user_offerings_set.add((user, offering))

        for user, offering in user_offerings_set:
            if not OfferingUser.objects.filter(user=user, offering=offering).exists():
                username = generate_username(user, offering)
                offering_user = OfferingUser.objects.create(
                    user=user, offering=offering, username=username
                )
                offering_user.set_propagation_date()
                offering_user.save()

    offerings = Offering.objects.filter(
        type="Marketplace.Slurm",
        state__in=[OfferingStates.ACTIVE, OfferingStates.PAUSED],
    )

    for offering in offerings:
        if "username_generation_policy" not in offering.plugin_options:
            offering.plugin_options.update({"username_generation_policy": "freeipa"})
            offering.save(update_fields=["plugin_options"])

    user_offerings_mapping(offerings)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0001_squashed_0076"),
        ("structure", "0001_squashed_0036"),
        ("core", "0001_squashed_0029"),
        ("waldur_freeipa", "0003_is_active_false"),
    ]

    operations = [
        migrations.RunPython(create_offering_users_for_existing_users),
    ]
