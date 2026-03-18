PROVIDER_DEFAULTS = {
    "ollama": {
        "temperature": 0.7,
        "top_p": 0.8,
    },
    "vllm": {
        "temperature": 0.7,
        "top_p": 0.8,
        "presence_penalty": 1.5,
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
            "top_k": 20,
        },
    },
    "openai": {
        "temperature": 0.7,
        "top_p": 0.8,
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
    }
)
