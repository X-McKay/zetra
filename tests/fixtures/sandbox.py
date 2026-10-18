from zetra._runtime.openshell import OpenShellSession as BaseSession

# Fixture sandbox transport.


class OpenShellSession(BaseSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, runtime_module="zetra.testing.server", **kwargs)
