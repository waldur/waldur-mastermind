from django.apps import AppConfig


def _resolve_preset_path(name: str):
    from waldur_mastermind.demo_presets.manifest import DemoPresetManager

    return DemoPresetManager.get_preset_path(name)


class DemoPresetsConfig(AppConfig):
    name = "waldur_mastermind.demo_presets"
    verbose_name = "Demo Presets & Structure Tooling"

    def ready(self):
        from waldur_mastermind.chat.validation import presets as chat_presets
        from waldur_mastermind.demo_presets import (
            tasks,  # noqa: F401 registers the task
        )

        chat_presets.register_preset_path_resolver(_resolve_preset_path)
