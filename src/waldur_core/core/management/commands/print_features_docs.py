from django.core.management.base import BaseCommand

from waldur_core.core.features import FEATURES, SCOPE_DEPLOYMENT, SCOPE_USER

SCOPE_LABELS = {
    SCOPE_DEPLOYMENT: "deployment (one value for the whole portal)",
    SCOPE_USER: "user (can also be turned on for selected users by feature rules)",
}


class Command(BaseCommand):
    help = """Prints all Waldur feature toggles in markdown format."""

    def handle(self, *args, **options):
        print("# Features", end="\n\n")
        features_output = []
        for section in sorted(FEATURES, key=lambda section: section["key"]):
            for feature in sorted(section["items"], key=lambda section: section["key"]):
                feature_block = (
                    f"## {section['key']}.{feature['key']}\n\n"
                    f"{feature['description']}\n\n"
                    f"Scope: {SCOPE_LABELS[feature['scope']]}\n"
                )
                features_output.append(feature_block)

        # Join all features and remove trailing whitespace to avoid multiple consecutive blank lines
        print("\n".join(features_output).rstrip())
