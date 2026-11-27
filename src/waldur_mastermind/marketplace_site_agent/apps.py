from django.apps import AppConfig
from django.db.models import signals

from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING


def _push_resource_update_message(resource) -> None:
    from waldur_mastermind.marketplace_site_agent import utils

    utils.push_resource_update_message(resource)


def _generate_agent_config(validated_offerings, validated_data, waldur_api_url: str):
    from waldur_mastermind.marketplace_site_agent import config_builder

    source = validated_data["configuration_source"]
    generated = config_builder.GeneratedAgentConfig(
        validated_data["secret_format"], source
    )
    generated.config["timezone"] = validated_data.get("timezone", "UTC")
    include_policy = validated_data.get("include_policy_settings", True)

    for offering in validated_offerings:
        if source == config_builder.WALDUR_SOURCE:
            offering_config = config_builder.build_bootstrap_offering_config(
                offering, waldur_api_url, generated
            )
        else:
            offering_config = config_builder.build_offering_config(
                offering, waldur_api_url, include_policy, generated
            )
        generated.config["offerings"].append(offering_config)
    return generated


def _offering_agent_config(offering):
    from waldur_mastermind.marketplace_site_agent import config_builder

    return config_builder.offering_agent_config(offering)


class MarketplaceSlurmConfig(AppConfig):
    name = "waldur_mastermind.marketplace_site_agent"
    verbose_name = "Marketplace SLURM Remote"
    service_name = "SLURM remote"

    def ready(self):
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.structure import signals as structure_signals
        from waldur_mastermind.marketplace import hooks as marketplace_hooks
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace_site_agent import (
            executors,
            handlers,
            models,
            pat_templates,
            processor,
            utils,
        )

        pat_templates.register_templates()

        manager.register(
            SITE_AGENT_OFFERING,
            create_resource_processor=processor.CreateAllocationProcessor,
            update_resource_processor=processor.UpdateAllocationLimitsProcessor,
            delete_resource_processor=processor.DeleteAllocationProcessor,
            can_update_limits=True,
            enable_remote_support=True,
            pull_resource_executor=executors.AgentResourcePullExecutor,
            supports_order_retry=True,
            # No max_limit_decimal_places: this one offering type fronts every
            # site agent, and its backends disagree about whether a limit can
            # hold a fraction. The advisory says what is known about the
            # individual offering instead of capping the whole type.
            limit_precision_advisory=utils.get_limit_precision_advisory,
        )

        signals.post_save.connect(
            handlers.send_offering_user_username_message,
            sender=marketplace_models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_offering_user_username_message",
        )

        signals.post_save.connect(
            handlers.send_resource_update_message_to_queue,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_resource_update_message_to_queue",
        )

        permission_signals.role_granted.connect(
            handlers.send_role_granted_message_to_queue,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_role_granted_message_to_queue",
        )

        permission_signals.role_revoked.connect(
            handlers.send_role_revoked_message_to_queue,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_role_revoked_message_to_queue",
        )

        signals.post_save.connect(
            handlers.send_project_service_account_info,
            sender=marketplace_models.ProjectServiceAccount,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_project_service_account_info",
        )

        signals.post_save.connect(
            handlers.send_project_service_account_deletion_info,
            sender=marketplace_models.ProjectServiceAccount,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_project_service_account_deletion_info",
        )

        signals.post_save.connect(
            handlers.send_course_account_info,
            sender=marketplace_models.CourseAccount,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_course_account_info",
        )

        signals.post_save.connect(
            handlers.send_course_account_deletion_info,
            sender=marketplace_models.CourseAccount,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_course_account_deletion_info",
        )

        signals.pre_delete.connect(
            handlers.cleanup_agent_identity_queue,
            sender=models.AgentIdentity,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.cleanup_agent_identity_queue",
        )

        structure_signals.project_moved.connect(
            handlers.send_resource_messages_on_project_move,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_resource_messages_on_project_move",
        )

        marketplace_hooks.register_site_agent_provider(
            marketplace_hooks.SiteAgentProvider(
                push_resource_update_message=_push_resource_update_message,
                generate_agent_config=_generate_agent_config,
                offering_agent_config=_offering_agent_config,
            )
        )
