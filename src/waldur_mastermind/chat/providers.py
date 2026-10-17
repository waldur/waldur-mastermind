PROVIDER_DEFAULTS = {
    "ollama": {
        "temperature": 0.7,
        "top_p": 0.8,
        "parallel_tool_calls": True,
    },
    "vllm": {
        "temperature": 0.7,
        "top_p": 0.8,
        "presence_penalty": 1.5,
        "parallel_tool_calls": True,
        "extra_body": {
            "top_k": 20,
        },
    },
    "openai": {
        "temperature": 0.7,
        "top_p": 0.8,
        "parallel_tool_calls": True,
    },
}

FALLBACK_DEFAULTS = {
    "temperature": 0.7,
    "top_p": 0.8,
}

# Keys that admin config (AI_ASSISTANT_COMPLETION_KWARGS) is permitted to set.
# Anything not in this list is silently ignored, preventing injection of
# structural fields (model, messages, stream, tools, …) or transport-layer
# overrides (extra_headers, extra_query, timeout, …).
ALLOWED_COMPLETION_KEYS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "max_completion_tokens",
        "presence_penalty",
        "frequency_penalty",
        "repetition_penalty",
        "stop",
        "seed",
        "reasoning_effort",
        "extra_body",
        "parallel_tool_calls",
        # tool_choice: "auto" (default) | "required" | "none"
        # vLLM ≥0.8.3 supports "required" with schema enforcement;
        # useful when the model declines to call tools despite directives.
        "tool_choice",
    }
)


def completion_kwargs(backend_type, admin_kwargs, exclude=frozenset()):
    """The completion settings for ``backend_type``: the provider's defaults,
    then the keys admins may set from ``admin_kwargs``
    (AI_ASSISTANT_COMPLETION_KWARGS). ``extra_body`` is merged into the
    default's rather than replacing it, since it holds provider safety
    settings. Keys in ``exclude`` are left out of both layers.

    The caller passes the settings in, so that it reads them, once, from the
    config it is tested with.
    """
    kwargs = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in PROVIDER_DEFAULTS.get(backend_type, FALLBACK_DEFAULTS).items()
        if key not in exclude
    }
    for key, value in (admin_kwargs if isinstance(admin_kwargs, dict) else {}).items():
        if key not in ALLOWED_COMPLETION_KEYS or key in exclude:
            continue
        if (
            key == "extra_body"
            and isinstance(kwargs.get("extra_body"), dict)
            and isinstance(value, dict)
        ):
            kwargs["extra_body"] = {**kwargs["extra_body"], **value}
        else:
            kwargs[key] = value
    return kwargs
