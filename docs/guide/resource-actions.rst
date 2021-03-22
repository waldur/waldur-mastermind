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

Complex actions and serializers
+++++++++++++++++++++++++++++++

If your action uses serializer to parse complex data, you should declare
action-specific serializers on the resource viewset. For example:

.. code-block:: python

    class InstanceViewSet(structure_views.BaseResourceViewSet):

        assign_floating_ip_serializer_class = serializers.AssignFloatingIpSerializer
        resize_serializer_class = serializers.InstanceResizeSerializer
