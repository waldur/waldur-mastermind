Declaring resource actions
--------------------------

Any methods on the resource viewset decorated with @detail_route(methods=['post'])
will be recognized as resource actions. For example:

.. code-block:: python

    class InstanceViewSet(structure_views.BaseResourceViewSet):

        @detail_route(methods=['post'])
        @safe_operation(valid_state=models.Resource.States.OFFLINE)
        def start(self, request, resource, uuid=None):
            pass

        @detail_route(methods=['post'])
        @safe_operation()
        def unlink(self, request, resource, uuid=None):
            pass

Rendering simple actions
++++++++++++++++++++++++

Given the previous example, the following metadata is rendered for actions
as response to OPTIONS request against resource endpoint
http://example.com/api/openstack-instances/a9edaa7357c84bd9855f1c0bf3305b49/

.. code-block:: javascript

    {
        "actions": {
            "start": {
                "title": "Start",
                "url": "http://example.waldur.com/api/openstack-instances/a9edaa7357c84bd9855f1c0bf3305b49/start/",
                "enabled": false,
                "reason": "Performing start operation is not allowed for resource in its current state",
                "destructive": false,
                "method": "POST"
            },
            "unlink": {
                "title": "Unlink",
                "url": "http://example.com/api/openstack-instances/a9edaa7357c84bd9855f1c0bf3305b49/unlink/",
                "enabled": true,
                "reason": null,
                "destructive": true,
                "type": "button",
                "method": "POST"
            }
        }
    }

Simple actions, such as start and unlink, do not require any additional data.
In order to apply it, you should issue `POST` request against endpoint specified in `url` field.
Some actions, such as start and stop, may be undone, but unlink action can't be.
In order to indicate it, set `destructive` attribute on the viewset method.
Usually such action is rendered on the frontend with `warning` indicator.
If you do not want to use the default title generated for your action,
set `title` attribute on the viewset method.
If action is not enabled for resource it is rendered on the frontend with `disabled` class and `reason` is shown as tooltip.

Complex actions and serializers
+++++++++++++++++++++++++++++++

If your action uses serializer to parse complex data, `get_serializer_class`
method on the resource viewset should return action-specific serializer. For example:

.. code-block:: python

    class InstanceViewSet(structure_views.BaseResourceViewSet):

        serializers = {
            'assign_floating_ip': serializers.AssignFloatingIpSerializer,
            'resize': serializers.InstanceResizeSerializer,
        }

        def get_serializer_class(self):
            serializer = self.serializers.get(self.action)
            return serializer or super(InstanceViewSet, self).get_serializer_class()

In this case action has `form` type and list of input fields is rendered.
The following attributes are exposed for action's fields: label, help_text, min_length, max_length, min_value, max_value, many.
For example, given this serializer the following metadata is rendered:

.. code-block:: python

    class InstanceResizeSerializer(serializers.Serializer):
        disk_size = serializers.IntegerField(min_value=1, label='Disk size')

        def get_fields(self):
            fields = super(InstanceResizeSerializer, self).get_fields()
            if self.instance:
                fields['disk_size'].min_value = self.instance.data_volume_size
            return fields

.. code-block:: javascript

    {
        "actions": {
            "resize": {
                "title": "Resize virtual machine",
                "url": "http://example.com/api/openstack-instances/171c3ceaf02c49bc98111dd3cfd106af/resize/",
                "fields": {
                    "disk_size": {
                        "type": "integer",
                        "required": false,
                        "label": "Disk size",
                        "min_value": 1024
                    }
                },
                "enabled": true,
                "reason": null,
                "destructive": false,
                "type": "form",
                "method": "POST"
            }
        }
    }

Filtering valid choices for action's fields
+++++++++++++++++++++++++++++++++++++++++++

Frontend uses list of fields supported by action in order to render dialog.
For fields with `select` type, `url` attribute specifies endpoint for fetching valid choices.
Choices are not rendered for performance reasons, think of huge list of choices.
Each object rendered by this endpoint should have attributes corresponding to value of
`value_field` and `display_name_field`. They are used to render select choices.

In order to display only valid field choices to user in action's dialog,
ensure that serializer's field has the following attributes:
`view_name`, `query_params`, `value_field` and `display_name_field`.
For example:

.. code-block:: python

    class AssignFloatingIpSerializer(serializers.Serializer):
        floating_ip = serializers.HyperlinkedRelatedField(
            label='Floating IP',
            required=True,
            view_name='openstack-fip-detail',
            lookup_field='uuid',
            queryset=models.FloatingIP.objects.all()
        )

        def get_fields(self):
            fields = super(AssignFloatingIpSerializer, self).get_fields()
            if self.instance:
                query_params = {
                    'status': 'DOWN',
                    'project': self.instance.service_project_link.project.uuid.hex,
                    'service': self.instance.service_project_link.service.uuid
                }

                field = fields['floating_ip']
                field.query_params = query_params
                field.value_field = 'url'
                field.display_name_field = 'address'
            return fields

Given previous serializer the following metadata is rendered:

.. code-block:: javascript

    {
        "actions": {
            "assign_floating_ip": {
                "title": "Assign floating IP",
                "url": "http://example.com/api/openstack-instances/a9edaa7357c84bd9855f1c0bf3305b49/assign_floating_ip/",
                "fields": {
                    "floating_ip": {
                        "type": "select",
                        "required": true,
                        "label": "Floating IP",
                        "url": "http://example.com/api/openstack-floating-ips/?status=DOWN&project=01cfe887ba784a2faf054b2fcf464b6a&service=1547f5de7baa4dee80af5021629b76d9",
                        "value_field": "url",
                        "display_name_field": "address"
                    }
                },
                "enabled": true,
                "reason": null,
                "destructive": false,
                "type": "form",
                "method": "POST"
            }
        }
    }
