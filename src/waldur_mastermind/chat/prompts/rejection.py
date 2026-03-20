"""Rejection and utility prompts for the Waldur AI Assistant."""

REJECTION_SYSTEM_PROMPT = (
    "The user's latest message could not be processed. Generate a polite, brief response "
    "explaining that you cannot help with that specific request. Suggest Waldur cloud "
    "management tasks you can assist with instead. Do not mention content filters, "
    "safety systems, or detection mechanisms."
)

CANNED_REJECTION_MESSAGE = (
    "I'm sorry, I can't help with that request. "
    "I can assist with Waldur cloud management tasks such as managing "
    "resources, projects, and quotas. Please try again with a different question."
)

TITLE_GENERATION_PROMPT = (
    "Generate a very short title (3-6 words) for this conversation. "
    "Reply with only the title text, nothing else. "
    "Do not wrap the title in quotation marks.\n\n"
    "User message: "
)
