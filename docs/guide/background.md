# Background processing

For executing heavier requests and performing background tasks Waldur is
using [Celery](https://docs.celeryproject.org/en/stable/). Celery is a task queue
that supports multiple backends for storing the tasks and results.
Currently Waldur is relying on [Redis](https://redis.io/) backend - Redis
server **must be** running for requests triggering background scheduling
to succeed.

If you are developing on OS X and have brew installed:

``` bash
brew install redis-server
redis-server
```

Please see Redis docs for installation on other platforms.

## Finite state machines

Some of the models in Waldur have a state field representing their
current condition. The state field is implemented as a finite state
machine. Both user requests and background tasks can trigger state
transition. A REST client can observe changes to the model instance
through polling the `state` field of the object.

Let's take VM instance in 'offline' state. A user can request the
instance to start by issuing a corresponding request over REST. This
will schedule a task in Celery and transition instance status to
'starting_scheduled'. Further user requests for starting an instance
will get state transition validation error. Once the background worker
starts processing the queued task, it updates the Instance status to the
'starting'. On task successful completion, the state is transitioned
to 'online' by the background task.

## Error state of background tasks

If a background task has failed to achieve it's goal, it should transit
into an error state. To propagate more information to the user each
model with an FSM field should include a field for error message
information - **error_message**. The field should be exposed via REST.
Background task should update this field before transiting into an erred
state.

Cleaning of the error state of the model instance should clean up also
`error_message` field.
