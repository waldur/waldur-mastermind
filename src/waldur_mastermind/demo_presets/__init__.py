def __getattr__(name: str):
    if name == "DemoPresetManager":
        from waldur_mastermind.demo_presets.manifest import DemoPresetManager

        return DemoPresetManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["DemoPresetManager"]
