# STOMP-Based Event Notification System

## System Overview

The [STOMP](https://stomp.github.io/)-based event notification system allows Waldur to communicate changes to resources, orders, and user roles to the `waldur-site-agent` that runs on a remote cluster. This eliminates the need for constant polling and enables immediate reactions to events.

The key components include:

1. **STOMP Publisher (Waldur side)**: Located in the [waldur_core/logging/utils.py](https://github.com/waldur/waldur-mastermind/blob/73f2a0a7df04405b1c9ed5d2512d6213d649d398/src/waldur_core/logging/utils.py#L88) file, this component publishes messages to STOMP queues when specific events occur.

2. **Event Subscription Service**: Manages subscriptions to events by creating unique topics for each type of notification. Related file: event subscription management via API: [waldur_core/logging/views.py](https://github.com/waldur/waldur-mastermind/blob/73f2a0a7df04405b1c9ed5d2512d6213d649d398/src/waldur_core/logging/views.py#L193)

3. **STOMP Consumer (Agent side)**: The `waldur-site-agent` running on the resource provider's infrastructure that subscribes to these topics and processes incoming messages. Related files:
   1. Event subscription registration: [waldur_site_agent/event_processing/utils.py](https://github.com/waldur/waldur-site-agent/blob/464b46f287aadffe5f98191221af7fea6e6c0ce1/waldur_site_agent/event_processing/utils.py#L50)
   2. STOMP message handlers: [waldur_site_agent/event_processing/handlers.py](https://github.com/waldur/waldur-site-agent/blob/464b46f287aadffe5f98191221af7fea6e6c0ce1/waldur_site_agent/event_processing/handlers.py#L99)
   3. STOMP listener: [waldur_site_agent/event_processing/listener.py](https://github.com/waldur/waldur-site-agent/blob/464b46f287aadffe5f98191221af7fea6e6c0ce1/waldur_site_agent/event_processing/listener.py#L30)

## Event Flow

1. An event occurs in Waldur (e.g., a new order is created, a user role changes, or a resource is updated)
2. Waldur publishes a message to the appropriate STOMP queue(s)
3. The site agent receives the message and processes it based on the event type
4. The agent communicates with the backend (e.g., SLURM) to execute the necessary actions

## Queue Naming Strategy

The system follows an **object-based naming convention** for STOMP queues rather than event-based naming. This design choice provides several benefits:

- **Simplified Client Configuration**: Clients subscribe to object types (e.g., `resource_periodic_limits`) rather than specific event types
- **Action Flexibility**: Specific actions (e.g., `apply_periodic_settings`, `update_limits`) are stored in the message payload
- **Easier Maintenance**: Adding new actions doesn't require queue reconfiguration
- **Future Migration Path**: Sets foundation for eventual migration to event-based naming without immediate client changes

**Current Approach:**

- Queue: `resource_periodic_limits`
- Payload: `{"action": "apply_periodic_settings", "settings": {...}}`

**Alternative Event-Based Approach** (for future consideration):

- Queue: `resource_periodic_limits_update`
- More specific but requires client reconfiguration for each new event type

## Message Types

The system handles several types of events:

1. **Order Messages** (`order`): Notifications about marketplace orders (create, update, terminate)
2. **User Role Messages** (`user_role`): Changes to user permissions in projects
3. **Resource Messages** (`resource`): Updates to resource configuration or status
4. **Resource Periodic Limits** (`resource_periodic_limits`): SLURM periodic usage policy updates with allocation and limit settings
5. **Offering User Messages** (`offering_user`): Creation, updates, and deletion of offering users
6. **Service Account Messages** (`service_account`): Service account lifecycle events
7. **Course Account Messages** (`course_account`): Course account management events
8. **Importable Resources Messages** (`importable_resources`): Backend resource discovery events

## Implementation Details

### Publishing Messages (Waldur Side)

When events like order creation occur, Waldur prepares and publishes STOMP messages: [code link](https://github.com/waldur/waldur-mastermind/blob/73f2a0a7df04405b1c9ed5d2512d6213d649d398/src/waldur_mastermind/marketplace_slurm_remote/utils.py#L12)

These messages are then sent via: [publish_stomp_messages](https://github.com/waldur/waldur-mastermind/blob/73f2a0a7df04405b1c9ed5d2512d6213d649d398/src/waldur_core/logging/tasks.py#L83)

### Offering User Event Messages

Offering user events are published when offering users are created, updated, or deleted. These handlers are located in [waldur_mastermind/marketplace/handlers.py](https://github.com/waldur/waldur-mastermind/blob/develop/src/waldur_mastermind/marketplace/handlers.py):

- `send_offering_user_created_message` - Triggers when an OfferingUser is created
- `send_offering_user_updated_message` - Triggers when an OfferingUser is updated
- `send_offering_user_deleted_message` - Triggers when an OfferingUser is deleted

**Message Payload Structure for OfferingUser Events:**

```json
{
  "offering_user_uuid": "uuid-hex-string",
  "user_uuid": "user-uuid-hex-string",
  "username": "generated-username",
  "state": "OK|Requested|Creating|Pending account linking|Pending additional validation|Requested deletion|Deleting|Deleted|Error creating|Error deleting",
  "action": "create|update|delete",
  "offering_uuid": "offering-uuid",
  "changed_fields": ["field1", "field2"]  // Only present for updates
}
```

**Event Triggers:**

- **Create**: When a new offering user account is created for a user in an offering
- **Update**: When any field of an existing offering user is modified (username, state, etc.)
- **Delete**: When an offering user account is removed from an offering

### Resource Periodic Limits Event Messages

Resource periodic limits events are published when SLURM periodic usage policies are applied to resources. These messages contain calculated SLURM settings including allocation limits, fairshare values, and QoS thresholds. The handler is located in [waldur_mastermind/policy/models.py](https://github.com/waldur/waldur-mastermind/blob/develop/src/waldur_mastermind/policy/models.py).

**Message Payload Structure for Resource Periodic Limits:**

```json
{
  "resource_uuid": "resource-uuid-hex-string",
  "backend_id": "slurm-account-name",
  "offering_uuid": "offering-uuid-hex-string",
  "action": "apply_periodic_settings",
  "timestamp": "2024-01-01T00:00:00.000000",
  "settings": {
    "fairshare": 333,
    "limit_type": "GrpTRESMins",
    "grp_tres_mins": {
      "billing": 119640
    },
    "qos_threshold": {
      "billing": 119640
    },
    "grace_limit": {
      "billing": 143568
    },
    "carryover_details": {
      "carryover_applied": true,
      "previous_period": "2023-Q4",
      "previous_usage": 750.0,
      "decay_factor": 0.015625,
      "effective_previous_usage": 11.7,
      "unused_allocation": 988.3,
      "base_allocation": 1000.0,
      "total_allocation": 1988.3
    }
  }
}
```

**Event Triggers:**

- **Policy Application**: When a SLURM periodic usage policy calculates new allocation limits and sends them to the site agent
- **Carryover Calculation**: When unused allocation from previous periods is calculated with decay factors
- **Limit Updates**: When fairshare values, TRES limits, or QoS thresholds need to be updated on the SLURM backend

### Subscription Management (Agent Side)

The [EventSubscriptionManager](https://github.com/waldur/waldur-site-agent/blob/464b46f287aadffe5f98191221af7fea6e6c0ce1/waldur_site_agent/event_processing/event_subscription_manager.py#L28) class handles creation of event subscriptions and setup of STOMP consumers:

- [get_or_create_event_subscription](https://github.com/waldur/waldur-site-agent/blob/464b46f287aadffe5f98191221af7fea6e6c0ce1/waldur_site_agent/event_processing/event_subscription_manager.py#L98) - create an event subscription in Waldur if doesn't exist yet
- [start_stomp_connection](https://github.com/waldur/waldur-site-agent/blob/464b46f287aadffe5f98191221af7fea6e6c0ce1/waldur_site_agent/event_processing/event_subscription_manager.py#L193) - setup STOMP client, connect agent to the broker and subscribe consumer to a queue

### Message Processing (Agent Side)

When a message arrives, it's routed to the appropriate handler based on the event type:

- [on_order_message_stomp](https://github.com/waldur/waldur-site-agent/blob/464b46f287aadffe5f98191221af7fea6e6c0ce1/waldur_site_agent/event_processing/handlers.py#L99) - create or update resources on backend
- [on_user_role_message_stomp](https://github.com/waldur/waldur-site-agent/blob/464b46f287aadffe5f98191221af7fea6e6c0ce1/waldur_site_agent/event_processing/handlers.py#L117) - create or update access permissions on backend
- [on_resource_message_stomp](https://github.com/waldur/waldur-site-agent/blob/464b46f287aadffe5f98191221af7fea6e6c0ce1/waldur_site_agent/event_processing/handlers.py#L152C5-L152C30) - create or update resource configuration on backend
- **on_resource_periodic_limits_message_stomp** - apply SLURM periodic usage policies, fairshare values, and allocation limits

## Technical Components

1. **WebSocket Transport**: The system uses STOMP over WebSockets for communication
2. **TLS Security**: Connections can be secured with TLS
3. **User Authentication**: Each subscription has its own credentials and permissions in RabbitMQ
4. **Queue Structure**: Queue names follow the pattern `/queue/subscription_{subscription_uuid}_offering_{offering_uuid}_{affected_object}`

## Error Handling and Resilience

The system includes:

- Graceful connection handling
- Signal handlers for proper shutdown
- Retry mechanisms for order processing
- Error logging and optional Sentry integration

## Benefits of the STOMP Approach

1. **Real-time Processing**: Actions are triggered immediately when events occur
2. **Reduced Network Traffic**: No constant polling needed
3. **Decoupling**: The agent doesn't need direct access to Waldur's database
4. **Scalability**: Multiple agents can subscribe to different events
5. **Reliability**: The STOMP protocol provides queue persistency to ensure message delivery and different acknowledgement options on the agent side

This event-driven architecture significantly improves the responsiveness and efficiency of the order processing system compared to traditional polling approaches.
