# Event logging

Event log entries is something an end user will see. In order to improve user experience the messages should be written in a consistent way.

Here are the guidelines for writing good log events.

* Use present perfect passive for the message.

  **Right:** `Environment %s has been created.`

  **Wrong:** `Environment %s was created.`

* Build a proper sentence: start with a capital letter, end with a period.

  **Right:** `Environment %s has been created.`

  **Wrong:** `environment %s has been created`

* Include entity names into the message string.

  **Right:** `User %s has gained role of %s in project %s.`

  **Wrong:** `User has gained role in project.`

* Don't include too many details into the message string.

  **Right:** `Environment %s has been updated.`

  **Wrong:** `Environment has been updated with name: %s, description: %s.`

* Use the name of an entity instead of its `__str__`.

  **Right:** `event_logger.info('Environment %s has been updated.', env.name)`

  **Wrong:** `event_logger.info('Environment %s has been updated.', env)`

* Don't put quotes around names or entity types.

  **Right:** `Environment %s has been created.`

  **Wrong:** `Environment "%s" has been created.`

* Don't capitalize entity types.

  **Right:** `User %s has gained role of %s in project %s.`

  **Wrong:** `User %s has gained Role of %s in Project %s.`

* For actions that require background processing log both start of the process and its outcome.

  **Success flow:**

   1. log `Environment %s creation has been started.` within HTTP request handler;

   2. log `Environment %s has been created.` at the end of background task.

  **Failure flow:**

   1. log `Environment %s creation has been started.` within HTTP request handler;

   2. log `Environment %s creation has failed.` at the end of background task.

* For actions that can be processed within HTTP request handler log only success.

  **Success flow:**

   log `User %s has been created.` at the end of HTTP request handler.

  **Failure flow:**

   don't log anything, since most of the errors that could happen here
   are validation errors that would be corrected by user and then resubmitted.
