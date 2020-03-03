Disaster recovery backups (DR backup)
-------------------------------------

DR backups allow storing backups of instances outside of an OpenStack
deployment and restoring it on other deployment.

On DR backup creation Waldur creates cinder backups for each volume of
the instance, stores instance metadata and exports and saves metadata records
of cinder backups.

On DR backup restoration Waldur creates cinder backups in a new tenant,
based on saved metadata records. After that it creates new volumes and
restores cinder backups into them. Finally, Waldur creates new instance
based on restored volumes and backup metadata.


REST API
^^^^^^^^

To create new DR backup, issue POST request with instance, backup name and
description to **/api/openstack-dr-backups/** endpoint. DR backup has fields
"state" and "runtime_state" that indicate backup creation progress.

It is possible to update DR backup name and description with POST request
against **/api/openstack-dr-backups/<uuid>/** endpoint.

To restore DR backup - issue POST request with DR backup, new tenant and new
instance flavor against **/api/openstack-dr-backup-restorations/** endpoint.
Make sure that flavor is big enough for instance. You can check DR backup
metadata to get stored instance minimum ram, cores and storage. On successful
start of the restoration, endpoint will return URL of an instance that
should will be created from DR backup, field "state" of this instance indicates
restoration process progress.

To create a schedule of DR backups, use the same endpoint as for regular backups
(**/api/openstack-backup-schedules/**) and additionally pass parameter
"backup type" as "DR".

For more detailed endpoints description - please check endpoints documentation.

Backups endpoints migration
^^^^^^^^^^^^^^^^^^^^^^^^^^^

1. States migrations:
     * READY -> OK
     * BACKING_UP -> CREATING
     * RESTORING -> OK
     * DELETING -> DELETING
     * ERRED -> ERRED

   Additionally states CREATION_SCHEDULED, UPDATE_SCHEDULED and DELETION_SCHEDULED were added. It is possible to start
   several restoration projects simultaneously, so you need to check restoration state in instance.

2. Endpoints migrations:
     * POST to **/api/openstack-backups/<uuid>/delete/** -> DELETE to **/api/openstack-backups/<uuid>/**
     * POST to **/api/openstack-backups/<uuid>/restore/** -> POST to **/api/openstack-backup-restorations/**
     * Now backups have field "restorations" with restorations details.
