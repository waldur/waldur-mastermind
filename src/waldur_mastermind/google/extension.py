from waldur_core.core import WaldurExtension


class GoogleExtension(WaldurExtension):
    """Stub extension kept solely so the google app stays in
    INSTALLED_APPS for migration history.

    The Google Calendar integration for booking offerings was removed.
    The migration chain must remain so deployments that already applied
    older migrations can run 0005_delete_models cleanly. After enough
    upgrade cycles have passed, this app + its migrations can be
    deleted entirely.
    """

    @staticmethod
    def django_app():
        return "waldur_mastermind.google"
