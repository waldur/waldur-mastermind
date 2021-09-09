# OpenStack backups

Backups allow storing backups of instances outside of an OpenStack
deployment and restoring it on other deployment.

On backup creation Waldur creates cinder backups for each volume of
the instance, stores instance metadata and exports and saves metadata
records of cinder backups.

On backup restoration Waldur creates cinder backups in a new tenant,
based on saved metadata records. After that it creates new volumes and
restores cinder backups into them. Finally, Waldur creates new instance
based on restored volumes and backup metadata.

## REST API

To create new backup, issue POST request with instance, backup name
and description to `/api/openstacktenant-backups/` endpoint. backup
has fields `state` and `runtime_state` that indicate backup creation
progress.

It is possible to update backup name and description with POST
request against `/api/openstacktenant-backups/<uuid>/` endpoint.

To restore backup - issue POST request with backup, new tenant and
new instance flavor against `/api/openstacktenant-backups/<uuid>/restore/`
endpoint. Make sure that flavor is big enough for instance. You can
check backup metadata to get stored instance minimum ram, cores and
storage. On successful start of the restoration, endpoint will return
URL of an instance that should will be created from backup, field
`state` of this instance indicates restoration process progress.

For more detailed endpoints description - please check endpoints
documentation.
