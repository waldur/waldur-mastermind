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

## Built-in actions on ResourceViewSet

The base `ResourceViewSet` provides several actions inherited by all resource ViewSets:

| Action | Method | Permission | Description |
|--------|--------|------------|-------------|
| `pull` | POST | Staff | Sync resource state from backend |
| `unlink` | POST | Staff | Delete resource from DB without backend operations |
| `set_erred` | POST | Staff | Force resource to ERRED state (useful for stuck transitional states) |
| `set_ok` | POST | Staff | Force resource to OK state and clear error fields |

The `set_erred` action accepts an optional request body with `error_message` and `error_traceback` fields.

## Complex actions and serializers

If your action uses serializer to parse complex data, you should declare
action-specific serializers on the resource viewset. For example:

``` python
class InstanceViewSet(structure_views.BaseResourceViewSet):

    assign_floating_ip_serializer_class = serializers.AssignFloatingIpSerializer
    resize_serializer_class = serializers.InstanceResizeSerializer
```
