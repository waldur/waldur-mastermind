import gc
import inspect
import weakref
from collections import defaultdict

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db.models import signals as django_signals
from django.dispatch import Signal


class Command(BaseCommand):
    """
    A management command to print all registered signal handlers in Markdown format.
    It provides detailed information about each handler, including its name, sender,
    and description, grouped by the application.
    """

    help = "Prints all registered signal handlers in markdown format."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-file",
            type=str,
            help="Output file path (optional, defaults to stdout)",
        )
        parser.add_argument(
            "--handler-type",
            type=str,
            choices=["signals", "custom_signals", "all"],
            default="all",
            help="Type of handlers to collect (default: all)",
        )

    def handle(self, *args, **options):
        output_file = options.get("output_file")
        handler_type = options.get("handler_type")

        markdown_content = self._generate_markdown(handler_type)

        if output_file:
            with open(output_file, "w") as f:
                f.write(markdown_content)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully written registered handlers to {output_file}"
                )
            )
        else:
            self.stdout.write(markdown_content)

    def _generate_markdown(self, handler_type_filter="all"):
        """Generate markdown documentation for all registered handlers."""
        markdown = "# Registered Handlers\n\n"
        markdown += "This document lists all registered signal handlers found in the system.\n\n"
        markdown += self._get_table_styles()

        handlers = self._collect_handlers(handler_type_filter)
        handlers_by_app = self._group_handlers_by_app(handlers)

        for app_name, app_handlers in sorted(handlers_by_app.items()):
            markdown += f"## Application: `{app_name}`\n\n"
            markdown += "| Handler Name | Signal Type | Sender | Description |\n"
            markdown += "|--------------|-------------|--------|-------------|\n"

            for handler in sorted(app_handlers, key=lambda x: x["name"]):
                name = self._escape_markdown(handler["name"])
                signal_type = self._escape_markdown(handler["type"])
                sender = self._escape_markdown(handler["sender"])
                description = self._escape_markdown(handler["description"])
                markdown += (
                    f"| `{name}` | `{signal_type}` | `{sender}` | {description} |\n"
                )

            markdown += "\n"

        if not handlers:
            markdown += "No handlers found."

        markdown += self._get_summary(handlers_by_app)
        return markdown

    def _collect_handlers(self, handler_type_filter):
        """Collect all signal handlers using a garbage collection approach."""
        unique_handlers = {}
        signal_objects = [obj for obj in gc.get_objects() if isinstance(obj, Signal)]

        for signal_obj in signal_objects:
            signal_name = self._get_signal_name(signal_obj)
            is_django_signal = signal_name.startswith("Django Signal")
            is_custom_signal = not is_django_signal

            if (handler_type_filter == "signals" and not is_django_signal) or (
                handler_type_filter == "custom_signals" and is_custom_signal
            ):
                continue

            if not hasattr(signal_obj, "receivers"):
                continue

            for receiver_info in signal_obj.receivers:
                receiver_func = self._get_receiver_func(receiver_info)
                if not receiver_func:
                    continue

                app_name = self._get_app_name(receiver_func)
                if not app_name.startswith("waldur"):
                    continue

                handler_key = self._get_handler_key(
                    receiver_func, signal_obj, receiver_info
                )
                if handler_key in unique_handlers:
                    continue

                unique_handlers[handler_key] = {
                    "type": signal_name,
                    "name": receiver_func.__name__,
                    "sender": self._get_sender_info(receiver_info),
                    "description": self._get_function_description(receiver_func),
                    "receiver_func": receiver_func,
                }

        return list(unique_handlers.values())

    def _group_handlers_by_app(self, handlers):
        """Group handlers by the application they belong to."""
        handlers_by_app = defaultdict(list)
        for handler in handlers:
            app_name = self._get_app_name(handler["receiver_func"])
            handlers_by_app[app_name].append(handler)
        return handlers_by_app

    def _get_receiver_func(self, receiver_info):
        """Extract the receiver function from receiver info."""
        if len(receiver_info) < 2:
            return None
        receiver_ref = receiver_info[1]
        return (
            receiver_ref()
            if isinstance(receiver_ref, weakref.ReferenceType)
            else receiver_ref
        )

    def _get_handler_key(self, receiver_func, signal_obj, receiver_info):
        """Create a unique key for a handler."""
        sender_info = self._get_sender_info(receiver_info)
        return (receiver_func.__name__, self._get_signal_name(signal_obj), sender_info)

    def _get_app_name(self, func):
        """Determine the app name from the function's module."""
        try:
            module = inspect.getmodule(func)
            if module:
                # The app name is usually the second part of the module name
                # e.g., waldur_core.core.handlers -> waldur_core
                parts = module.__name__.split(".")
                if len(parts) > 1:
                    return parts[0]
        except Exception:
            return "Unknown"
        return "Unknown"

    def _get_signal_name(self, signal_obj):
        """Get a readable name for the signal."""
        standard_signals = {
            django_signals.post_save: "post_save",
            django_signals.pre_save: "pre_save",
            django_signals.post_delete: "post_delete",
            django_signals.pre_delete: "pre_delete",
            django_signals.post_init: "post_init",
            django_signals.pre_init: "pre_init",
            django_signals.m2m_changed: "m2m_changed",
        }
        if signal_obj in standard_signals:
            return f"Django Signal ({standard_signals[signal_obj]})"

        # For custom signals, try to find their name by searching through apps
        for app_config in apps.get_app_configs():
            try:
                signals_module = app_config.module.signals
                for attr_name in dir(signals_module):
                    if getattr(signals_module, attr_name) is signal_obj:
                        return f"Custom Signal ({attr_name})"
            except (AttributeError, ModuleNotFoundError):
                continue
        return "Unknown Signal"

    def _get_sender_info(self, receiver_info):
        """Extract sender information from signal receiver info."""
        try:
            lookup_key = receiver_info[0]
            if len(lookup_key) >= 2 and lookup_key[1] is not None:
                sender_id = lookup_key[1]
                for model in apps.get_models():
                    if id(model) == sender_id:
                        return f"{model._meta.app_label}.{model.__name__}"
                # Fallback for non-model senders
                for obj in gc.get_objects():
                    if id(obj) == sender_id and inspect.isclass(obj):
                        return obj.__name__
                return "—"
        except (IndexError, AttributeError):
            pass
        return "Any"

    def _get_function_description(self, func):
        """Get function description from docstring."""
        try:
            if hasattr(func, "__doc__") and func.__doc__:
                doc = func.__doc__.strip()
                first_line = doc.split("\n")[0]
                return first_line if first_line else "No description"
            return "No description"
        except Exception:
            return "Unable to get description"

    def _get_table_styles(self):
        """Return CSS styles for the markdown table."""
        return """<style>
table {
  table-layout: fixed;
  width: 100%;
}
td:nth-child(1), td:nth-child(2), td:nth-child(3) {
  width: 20%;
  word-wrap: break-word;
}
td:nth-child(4) {
  width: 40%;
  word-wrap: break-word;
}
</style>

"""

    def _get_summary(self, handlers_by_app):
        """Generate a summary of the collected handlers."""
        total_handlers = sum(len(h) for h in handlers_by_app.values())
        summary = "## Summary\n\n"
        summary += f"Total unique handlers found: {total_handlers}\n\n"
        for app_name, handlers in sorted(handlers_by_app.items()):
            summary += f"- **{app_name}**: {len(handlers)} handlers\n"
        return summary

    def _escape_markdown(self, text):
        """Escape characters that have special meaning in Markdown tables."""
        return text.replace("|", "\\|")
