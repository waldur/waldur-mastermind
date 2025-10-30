# Scheduled Background Jobs

This document lists all scheduled background jobs (Celery beat tasks) configured in the system.

<style>
table {
  table-layout: fixed;
  width: 100%;
}
td:nth-child(1) {
  width: 25%;
  word-wrap: break-word;
}
td:nth-child(2) {
  width: 25%;
  word-wrap: break-word;
}
td:nth-child(3) {
  width: 15%;
  word-wrap: break-word;
}
td:nth-child(4) {
  width: 35%;
  word-wrap: break-word;
}
</style>

| Job Name | Task | Schedule | Description |
|----------|------|----------|-------------|
| `cancel-expired-invitations` | `waldur_core.users.cancel_expired_invitations` | 1 day | Invitation lifetime must be specified in Waldur Core settings with parameter<br> "INVITATION_LIFETIME". If invitation creation time is less than expiration time, the invitation will set as expired. |
| `cancel_expired_group_invitations` | `waldur_core.users.cancel_expired_group_invitations` | 1 day | Invitation lifetime must be specified in Waldur Core settings with parameter<br> "GROUP_INVITATION_LIFETIME". If invitation creation time is less than expiration time,<br> the invitation will set as expired. |
| `check-expired-permissions` | `waldur_core.permissions.check_expired_permissions` | 1 day | Task not found in registry |
| `check-polices` | `waldur_mastermind.policy.check_polices` | Cron: `* * 1 * * (m/h/dM/MY/d)` | Evaluate all policies across all policy types in the system. |
| `cleanup-orphaned-answers` | `waldur_core.checklist.cleanup_orphaned_answers` | 1 day | Task not found in registry |
| `core-reset-updating-resources` | `waldur_core.reset_updating_resources` | 10 minutes | Reset resources stuck in UPDATING state when their Celery tasks are completed. |
| `create-reviews-if-strategy-is-after-proposal` | `waldur_mastermind.proposal.create_reviews_if_strategy_is_after_proposal` | 1 hour | Create reviews for active rounds with 'after proposal' review strategy. |
| `create-reviews-if-strategy-is-after-round` | `waldur_mastermind.proposal.create_reviews_if_strategy_is_after_round` | 1 hour | Create reviews for active rounds with 'after round' review strategy. |
| `create_customer_permission_reviews` | `waldur_core.structure.create_customer_permission_reviews` | 1 day | Create customer permission reviews for customers that need periodic review of user permissions. |
| `create_project_permission_reviews` | `waldur_core.structure.create_project_permission_reviews` | 1 day | Create project permission reviews for projects that need periodic review of user permissions. |
| `delete-dangling-event-subscriptions` | `waldur_core.logging.delete_dangling_event_subscriptions` | 1 hour | No description available |
| `delete-stale-event-subscriptions` | `waldur_core.logging.delete_stale_event_subscriptions` | 1 day | No description available |
| `expire-stale-verifications` | `waldur_core.onboarding.expire_stale_verifications` | 1 hour | Task not found in registry |
| `expired-reviews-should-be-cancelled` | `waldur_mastermind.proposal.expired_reviews_should_be_cancelled` | 1 hour | Cancel reviews that have expired. |
| `mark-offering-backend-as-disconnected-after-timeout` | `waldur_mastermind.marketplace_site_agent.mark_offering_backend_as_disconnected_after_timeout` | 1 hour | No description available |
| `mark_agent_services_as_inactive` | `waldur_mastermind.marketplace_site_agent.mark_agent_services_as_inactive` | 5 minutes | No description available |
| `mark_resources_as_erred_after_timeout` | `waldur_mastermind.marketplace.mark_resources_as_erred_after_timeout` | 2 hours | Mark stale orders and their resources as erred if they have been executing for more than 2 hours. |
| `marketplace-openstack.create-resources-for-lost-instances-and-volumes` | `waldur_mastermind.marketplace_openstack.create_resources_for_lost_instances_and_volumes` | 6 hours | Create marketplace resources for OpenStack instances and volumes that exist in backend but are missing from marketplace. |
| `marketplace-openstack.refresh-instance-backend-metadata` | `waldur_mastermind.marketplace_openstack.refresh_instance_backend_metadata` | 1 day | Refresh metadata for OpenStack instances from backend to ensure marketplace resources have up-to-date information. |
| `notification_about_project_ending` | `waldur_mastermind.marketplace.notification_about_project_ending` | Cron: `0 10 * * * (m/h/dM/MY/d)` | Send notifications about projects ending in 1 day and 7 days. |
| `notification_about_resource_ending` | `waldur_mastermind.marketplace.notification_about_resource_ending` | Cron: `0 10 * * * (m/h/dM/MY/d)` | Send notifications about resources ending in 1 day and 7 days. |
| `notify_about_stale_resource` | `waldur_mastermind.marketplace.notify_about_stale_resource` | Cron: `0 15 5 * * (m/h/dM/MY/d)` | Notify customers about resources that have not generated invoice items in the last 3 months. |
| `notify_manager_on_round_cutoff` | `waldur_mastermind.proposal.notify_manager_on_round_cutoff` | 1 hour | No description available |
| `notify_reviewer_on_round_start` | `waldur_mastermind.proposal.notify_reviewer_on_round_start` | 1 day | No description available |
| `openstack-delete-expired-backups` | `openstack.DeleteExpiredBackups` | 10 minutes | Delete expired OpenStack backup resources that have reached their retention period. |
| `openstack-delete-expired-snapshots` | `openstack.DeleteExpiredSnapshots` | 10 minutes | Delete expired OpenStack snapshot resources that have reached their retention period. |
| `openstack-tenant-properties-list-pull-task` | `openstack.tenant_properties_list_pull_task` | 1 day | Pull OpenStack tenant properties like flavors, images, and volume types from backend. |
| `openstack-tenant-pull-quotas` | `openstack.TenantPullQuotas` | 12 hours | Pull quota limits and usage information for all OpenStack tenants. |
| `openstack-tenant-resources-list-pull-task` | `openstack.tenant_resources_list_pull_task` | 1 hour | Pull OpenStack tenant resources like instances, volumes, and snapshots from backend. |
| `openstack-tenant-subresources-list-pull-task` | `openstack.tenant_subresources_list_pull_task` | 2 hours | Pull OpenStack tenant subresources like security groups, networks, subnets, and ports from backend. |
| `openstack_mark_as_erred_old_tenants_in_deleting_state` | `openstack.mark_as_erred_old_tenants_in_deleting_state` | 1 day | Mark OpenStack tenants as erred if they have been in deleting state for more than 1 day. |
| `openstack_mark_stuck_updating_tenants_as_erred` | `openstack.mark_stuck_updating_tenants_as_erred` | 1 hour | No description available |
| `process-pending-project-invitations` | `waldur_core.users.process_pending_project_invitations` | 2 hours | Process project invitations for projects that have become active. |
| `process_pending_project_orders` | `waldur_mastermind.marketplace.process_pending_project_orders` | 2 hours | Process orders for projects that have become active. |
| `process_pending_start_date_orders` | `waldur_mastermind.marketplace.process_pending_start_date_orders` | 2 hours | Finds orders that are pending activation due to a future start date<br> and moves them to the EXECUTING state if the start date has been reached. |
| `proposals-for-ended-rounds-should-be-cancelled` | `waldur_mastermind.proposal.proposals_for_ended_rounds_should_be_cancelled` | 1 hour | Cancel proposals for rounds that have ended. |
| `pull-priorities` | `waldur_mastermind.support.pull_priorities` | 1 day | Pull priority levels from the active support backend. |
| `pull-service-properties` | `waldur_core.structure.ServicePropertiesListPullTask` | 1 day | Pull service properties from all active service backends. |
| `pull-service-resources` | `waldur_core.structure.ServiceResourcesListPullTask` | Hourly (at minute 0) | Pull resources from all active service backends. |
| `pull-support-users` | `waldur_mastermind.support.pull_support_users` | 6 hours | Pull support users from the active support backend. |
| `remove_deleted_robot_accounts` | `waldur_mastermind.marketplace.remove_deleted_robot_accounts` | 1 day | Remove robot accounts that are in DELETED state.<br> This task runs daily to clean up robot accounts that have been marked for deletion. |
| `send-messages-about-pending-orders` | `waldur_mastermind.marketplace_site_agent.send_messages_about_pending_orders` | 1 hour | Send a message about pending orders created 1 hour ago to MQTT |
| `send-monthly-invoicing-reports-about-customers` | `invoices.send_monthly_invoicing_reports_about_customers` | Cron: `0 0 2 * * (m/h/dM/MY/d)` | Send monthly invoicing reports via email to configured recipients. |
| `send-notifications-about-upcoming-ends` | `invoices.send_notifications_about_upcoming_ends` | 1 day | Send notifications about upcoming end dates of fixed payment profiles. |
| `send-reminder-for-pending-invitations` | `waldur_core.users.send_reminder_for_pending_invitations` | 1 day | Send reminder emails for pending invitations that are about to expire. |
| `send-scheduled-broadcast-notifications` | `waldur_mastermind.notifications.send_scheduled_broadcast_messages` | 12 hours | Send broadcast messages that have been scheduled for delivery. |
| `send_telemetry` | `waldur_mastermind.marketplace.send_metrics` | 1 day | Send anonymous usage metrics and telemetry data to the Waldur team. |
| `structure-set-erred-stuck-resources` | `waldur_core.structure.SetErredStuckResources` | 1 hour | This task marks all resources which have been provisioning for more than 3 hours as erred. |
| `sync-resources` | `waldur_mastermind.marketplace_site_agent.sync_resources` | 10 minutes | Sync resources that haven't been updated in the last hour.<br> Processes only resources that users have subscribed to receive updates for. |
| `sync_request_types` | `waldur_mastermind.support.sync_request_types` | 1 day | Synchronize request types from the active support backend. |
| `terminate_expired_resources` | `waldur_mastermind.marketplace.terminate_expired_resources` | Cron: `20 1 * * * (m/h/dM/MY/d)` | Terminate marketplace resources that have reached their end date. |
| `terminate_resources_if_project_end_date_has_been_reached` | `waldur_mastermind.marketplace.terminate_resources_if_project_end_date_has_been_reached` | Cron: `40 1 * * * (m/h/dM/MY/d)` | Terminate resources when their project has reached its end date. |
| `terminate_resources_in_state_erred_without_backend_id_and_failed_terminate_order` | `waldur_mastermind.marketplace.terminate_resources_in_state_erred_without_backend_id_and_failed_terminate_order` | 1 day | Clean up erred Slurm resources that failed both creation and termination. |
| `update-custom-quotas` | `waldur_core.quotas.update_custom_quotas` | 1 hour | Task not found in registry |
| `update-invoices-total-cost` | `invoices.update_invoices_total_cost` | 1 day | Update cached total cost for current month invoices. |
| `update-standard-quotas` | `waldur_core.quotas.update_standard_quotas` | 1 day | Task not found in registry |
| `update_daily_consent_history` | `waldur_mastermind.marketplace.update_daily_consent_history` | 1 day | Daily task to update consent history statistics for dashboard reporting.<br> Uses quota system + DailyQuotaHistory for historical tracking. |
| `valimo-auth-cleanup-auth-results` | `waldur_auth_valimo.cleanup_auth_results` | 1 hour | Clean up Valimo authentication results older than 7 days. |
| `waldur-create-invoices` | `invoices.create_monthly_invoices` | Monthly (1st day of month at midnight) | - For every customer change state of the invoices for previous months from "pending" to "billed"<br> and freeze their items.<br> - Create new invoice for every customer in current month if not created yet. |
| `waldur-create-offering-users-for-site-agent-offerings` | `waldur_mastermind.marketplace_site_agent.sync_offering_users` | 1 day | No description available |
| `waldur-firecrest-pull-jobs` | `waldur_firecrest.pull_jobs` | 1 hour | Pull SLURM jobs from Firecrest API for all offering users with valid OAuth tokens. |
| `waldur-freeipa-sync-groups` | `waldur_freeipa.sync_groups` | 10 minutes | This task is used by Celery beat in order to periodically<br> schedule FreeIPA group synchronization. |
| `waldur-freeipa-sync-names` | `waldur_freeipa.sync_names` | 1 day | Synchronize user names between Waldur and FreeIPA backend. |
| `waldur-marketplace-calculate-usage` | `waldur_mastermind.marketplace.calculate_usage_for_current_month` | 1 hour | Calculate marketplace resource usage for the current month across all customers and projects. |
| `waldur-marketplace-script-pull-resources` | `waldur_marketplace_script.pull_resources` | 1 hour | Pull resources from marketplace script offerings by executing configured pull scripts. |
| `waldur-marketplace-script-remove-old-dry-runs` | `waldur_marketplace_script.remove_old_dry_runs` | 1 day | Remove old dry run records that are older than one day. |
| `waldur-mastermind-reject-past-bookings` | `waldur_mastermind.booking.reject_past_bookings` | Cron: `0 10 * * * (m/h/dM/MY/d)` | Reject booking resources that have start times in the past. |
| `waldur-mastermind-send-notifications-about-upcoming-bookings` | `waldur_mastermind.booking.send_notifications_about_upcoming_bookings` | Cron: `0 9 * * * (m/h/dM/MY/d)` | Send email notifications to users about their upcoming bookings. |
| `waldur-pid-update-all-referrables` | `waldur_pid.update_all_referrables` | 1 day | Update DataCite DOI information for all referrable objects with existing DOIs. |
| `waldur-pull-remote-eduteams-ssh-keys` | `waldur_auth_social.pull_remote_eduteams_ssh_keys` | 3 minutes | Task not found in registry |
| `waldur-pull-remote-eduteams-users` | `waldur_auth_social.pull_remote_eduteams_users` | 5 minutes | Task not found in registry |
| `waldur-rancher-delete-leftover-keycloak-groups` | `waldur_rancher.delete_leftover_keycloak_groups` | 1 hour | Delete remote Keycloak groups with no linked groups in Waldur |
| `waldur-rancher-delete-leftover-keycloak-memberships` | `waldur_rancher.delete_leftover_keycloak_memberships` | 1 hour | Delete remote Keycloak user memberships in groups with no linked instances in Waldur |
| `waldur-rancher-sync-keycloak-users` | `waldur_rancher.sync_keycloak_users` | 15 minutes | Synchronize Keycloak users with pending group memberships in Rancher. |
| `waldur-rancher-sync-rancher-group-bindings` | `waldur_rancher.sync_rancher_group_bindings` | 1 hour | Sync group bindings in Rancher with the groups in Waldur. |
| `waldur-rancher-sync-rancher-roles` | `waldur_rancher.sync_rancher_roles` | 1 hour | Synchronize Rancher roles with local role templates for clusters and projects. |
| `waldur-rancher-update-clusters-nodes` | `waldur_rancher.pull_all_clusters_nodes` | 1 day | Pull node information for all Rancher clusters and update their states. |
| `waldur-remote-notify-about-pending-project-update-requests` | `waldur_mastermind.marketplace_remote.notify_about_pending_project_update_requests` | 7 days | Notify about pending project update requests.<br><br> This task sends email notifications to project owners about pending<br> project update requests that have been waiting for more than a week.<br> Runs weekly via celery beat. |
| `waldur-remote-offerings-sync` | `waldur_mastermind.marketplace_remote.remote_offerings_sync` | 1 day | Synchronize remote offerings based on RemoteSynchronisation configurations.<br><br> This task processes active remote synchronization configurations,<br> running synchronization for each configured remote marketplace.<br> Runs daily via celery beat. |
| `waldur-remote-pull-erred-orders` | `waldur_mastermind.marketplace_remote.pull_erred_orders` | 1 day | Pull and synchronize erred remote marketplace orders.<br><br> This task specifically handles erred local orders that may have been<br> resolved in remote Waldur instances. It synchronizes UPDATE and TERMINATE<br> order states and adjusts local resource states accordingly.<br> Runs daily via celery beat. |
| `waldur-remote-pull-invoices` | `waldur_mastermind.marketplace_remote.pull_invoices` | 1 hour | Pull and synchronize remote marketplace resource invoice data.<br><br> This task synchronizes invoice items for marketplace resources from<br> remote Waldur instances, including current and previous month data.<br> Runs every 60 minutes via celery beat. |
| `waldur-remote-pull-maintenance-announcements` | `waldur_mastermind.marketplace_remote.pull_maintenance_announcements` | 1 hour | Pull and synchronize remote maintenance announcements.<br><br> This task synchronizes maintenance announcements from remote Waldur instances,<br> Runs every 60 minutes via celery beat. |
| `waldur-remote-pull-offering-users` | `waldur_mastermind.marketplace_remote.pull_offering_users` | 1 hour | Pull and synchronize remote marketplace offering users.<br><br> This task synchronizes user associations with marketplace offerings from<br> remote Waldur instances, ensuring local user mappings are up to date.<br> Runs every 60 minutes via celery beat. |
| `waldur-remote-pull-offerings` | `waldur_mastermind.marketplace_remote.pull_offerings` | 1 hour | Pull and synchronize remote marketplace offerings.<br><br> This task synchronizes offerings from remote Waldur instances, updating<br> local offering data including components, plans, and access endpoints.<br> Runs every 60 minutes via celery beat. |
| `waldur-remote-pull-orders` | `waldur_mastermind.marketplace_remote.pull_orders` | 1 hour | Pull and synchronize remote marketplace orders.<br><br> This task synchronizes order states from remote Waldur instances,<br> updating local order states and associated resource backend IDs.<br> Only processes non-terminal orders. Runs every 60 minutes via celery beat. |
| `waldur-remote-pull-resources` | `waldur_mastermind.marketplace_remote.pull_resources` | 1 hour | Pull and synchronize remote marketplace resources.<br><br> This task synchronizes resource data from remote Waldur instances,<br> updating local resource states and importing remote orders when needed.<br> Runs every 60 minutes via celery beat. |
| `waldur-remote-pull-robot-accounts` | `waldur_mastermind.marketplace_remote.pull_robot_accounts` | 1 hour | Pull and synchronize remote marketplace resource robot accounts.<br><br> This task synchronizes robot account data for marketplace resources from<br> remote Waldur instances, including account types, usernames, and keys.<br> Runs every 60 minutes via celery beat. |
| `waldur-remote-pull-usage` | `waldur_mastermind.marketplace_remote.pull_usage` | 1 hour | Pull and synchronize remote marketplace resource usage data.<br><br> This task synchronizes component usage data from remote Waldur instances,<br> including both regular usage and user-specific usage metrics.<br> Pulls usage data from the last 4 months. Runs every 60 minutes via celery beat. |
| `waldur-remote-push-project-data` | `waldur_mastermind.marketplace_remote.push_remote_project_data` | 1 day | Push project data to remote Waldur instances.<br><br> This task pushes local project data (name, description, end date, etc.)<br> to remote Waldur instances for projects that have marketplace resources.<br> Runs daily via celery beat. |
| `waldur-remote-sync-remote-project-permissions` | `waldur_mastermind.marketplace_remote.sync_remote_project_permissions` | 6 hours | Synchronize project permissions with remote Waldur instances.<br><br> This task ensures that project permissions are synchronized between<br> local and remote Waldur instances when eduTEAMS sync is enabled.<br> It creates remote projects if needed and manages user role assignments.<br> Runs every 6 hours via celery beat. |
| `waldur-sync-daily-quotas` | `analytics.sync_daily_quotas` | 1 day | Task not found in registry |
| `waldur-update-all-pid` | `waldur_pid.update_all_pid` | 1 day | Update all PID (Persistent Identifier) information for referrable objects with DataCite DOIs. |
| `waldur_mastermind.marketplace_rancher.report_rancher_usage` | `waldur_mastermind.marketplace_rancher.report_rancher_usage` | 1 hour | No description available |

