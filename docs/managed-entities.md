# Managed entities

## Overview

Managed entities are entities for which Waldur's database is considered an authoritative source of information.
By means of REST API a user defines the desired state of the entities.
Waldur's jobs are then executed to make the backend (OpenStack, JIRA, etc) reflect
the desired state as close as possible.

Since making changes to a backend can take a long time, they are done in background tasks.

Here's a proper way to deal with managed entities:

* within the scope of REST API request:
  * introduce the change (create, delete or edit an entity) to the Waldur's database;
  * schedule a background job passing instance id as a parameter;
  * return a positive HTTP response to the caller.

* within the scope of background job:

  * fetch the entity being changed by its instance id;
  * make sure that it is in a proper state (e.g. not being updated by another background job);
  * transactionally update the its state to reflect that it is being updated;
  * perform necessary calls to backend to synchronize changes
    from Waldur's database to that backend;
  * transactionally update its state to reflect that it not being updated anymore.

Using the above flow makes it possible for user to get immediate feedback
from an initial REST API call and then query state changes of the entity.

## Managed entities operations flow

1. View receives request for entity change.

1. If request contains any data - view passes request to serializer for validation.

1. View extracts operations specific information from validated data and saves entity via serializer.

1. View starts executor with saved instance and operation specific information as input.

1. Executor handles entity states checks and transition.

1. Executor schedules celery tasks to perform asynchronous operations.

1. View returns response.

1. Tasks asynchronously call backend methods to perform required operation.

1. Callback tasks changes instance state after backend method execution.

## Simplified schema of operations flow

View ---> Serializer ---> View ---> Executor ---> Tasks ---> Backend
