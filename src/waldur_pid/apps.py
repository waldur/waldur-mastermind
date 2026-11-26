from django.apps import AppConfig


def _create_doi(offering) -> None:
    from waldur_pid import utils

    utils.create_doi(offering)


def _update_doi(offering) -> None:
    from waldur_pid import utils

    utils.update_doi(offering)


def _schedule_link_doi_with_collection(serialized_offering: str) -> None:
    from waldur_pid import tasks

    tasks.link_doi_with_collection.delay(serialized_offering)


def _schedule_update_referrable(serialized_offering: str) -> None:
    from waldur_pid import tasks

    tasks.update_referrable.delay(serialized_offering)


class PIDConfig(AppConfig):
    name = "waldur_pid"
    verbose_name = "PID"
    service_name = "PID"

    def ready(self):
        from waldur_mastermind.marketplace import hooks as marketplace_hooks

        marketplace_hooks.register_pid_actions_provider(
            marketplace_hooks.PidActionsProvider(
                create_doi=_create_doi,
                update_doi=_update_doi,
                schedule_link_doi_with_collection=_schedule_link_doi_with_collection,
                schedule_update_referrable=_schedule_update_referrable,
            )
        )
