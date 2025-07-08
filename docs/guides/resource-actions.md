# Declaring resource actions

Any methods on the resource viewset decorated with
`@action(detail=True, methods=['post'])` will be recognized as resource
actions. For example:

``` python
class InstanceViewSet(structure_views.BaseResourceViewSet):

    @action(detail=True, methods=['post'])
    def start(self, request, uuid=None):
        pass

    @action(detail=True, methods=['post'])
    def unlink(self, request, uuid=None):
        pass
```

## Complex actions and serializers

If your action uses serializer to parse complex data, you should declare
action-specific serializers on the resource viewset. For example:

``` python
class InstanceViewSet(structure_views.BaseResourceViewSet):

    assign_floating_ip_serializer_class = serializers.AssignFloatingIpSerializer
    resize_serializer_class = serializers.InstanceResizeSerializer
```
