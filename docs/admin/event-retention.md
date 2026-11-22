# Event Retention

Every action Waldur records — a resource created, a role granted, a sign-in, an
order approved — is written to the event log as an `Event` row, plus one `Feed`
row for each organization, project or resource the event is filed under.
Nothing removes these rows by default, so on a busy deployment the event
tables grow for the life of the installation and become some of the largest in
the database.

`EVENT_RETENTION_DAYS` bounds them.

## Configuration

| Setting | Environment variable | Default | Description |
|---------|----------------------|---------|-------------|
| `WALDUR_CORE["EVENT_RETENTION_DAYS"]` | `WALDUR_EVENT_RETENTION_DAYS` | `0` | Delete events older than this many days. `0` keeps events forever. |

It is a deployment setting, not a runtime one: it does not appear in the
administration settings, and staff cannot change it from the UI or through
`/api/override-settings/`. The retention period of the audit trail is decided
by whoever runs the deployment, and changing it means changing the
deployment's configuration and restarting the API, Celery worker and Celery
beat containers.

- **Helm** (`waldur-helm`): set `waldur.eventRetentionDays` in your values
  file.
- **Docker Compose** (`waldur-docker-compose`): set
  `WALDUR_EVENT_RETENTION_DAYS` in `.env`.
- **Other image-based deployments**: pass the `WALDUR_EVENT_RETENTION_DAYS`
  environment variable to the containers.
- **Source installs**: set `WALDUR_CORE["EVENT_RETENTION_DAYS"]` in your
  settings module.

The image treats a negative value like `0`, and a value that is not a whole
number, or is above `36500` (a century), stops the container at startup with
an error naming the variable. The Helm chart refuses anything but a whole
number of days when it renders.

## How It Works

The Celery beat entry `delete-expired-events` runs the task
`waldur_core.logging.delete_expired_events` daily at 04:30. When the setting
is above `0`, the task deletes every event created more than that many days
ago, oldest first, together with its feed rows. It deletes 5,000 events per
transaction, so the first run on a deployment with years of history does not
hold one long transaction or lock the event table for its whole duration;
it logs the number of events deleted and the cutoff it used.

A run stops after 10 minutes, well inside the Celery soft time limit, and
queues a fresh run for the events still left, so the backlog of a first run
is cleared in a chain of runs rather than one that is cut off. Two runs that
overlap (one started by hand while the scheduled one is running, say) skip the
rows the other has locked instead of waiting for them.

Newer events, the event list (`/api/events/`) and the scoped feeds of
organizations, projects and resources are unaffected apart from the expired
rows no longer being listed.

## Choosing a Retention Period

**Deletion is permanent.** Nothing is archived or exported first; take a
database backup or export the events beforehand if they must be kept
elsewhere.

The event log is the audit trail that users, organization owners and auditors
read: it answers who granted a role, who deleted a resource, and when. The
retention period is therefore a policy decision for the deployment, not a
tuning parameter. Before setting it, check:

- your organization's audit and record-keeping requirements, and any contract
  or regulation that names a minimum period;
- how far back support staff usually look when investigating an incident or a
  billing dispute;
- that the period is longer than any reporting cycle (for example a yearly
  allocation review) that relies on events.

If events are also shipped to an external log store that meets those
requirements, the period here can be shorter. The setting applies to all event
types alike: security and permission events are deleted on the same schedule
as the rest.

To see how fast the event tables grow before choosing, use
[table growth monitoring](table-growth-monitoring.md).

## Related Files

- Task: `src/waldur_core/logging/tasks.py`
- Schedule: `src/waldur_core/server/celery_settings.py`
- Setting: `src/waldur_core/core/metadata.py` (`WaldurCore.EVENT_RETENTION_DAYS`)
- Environment variable: `docker/rootfs/etc/waldur/settings.py`
- Tests: `src/waldur_core/logging/tests/test_event_retention.py`
