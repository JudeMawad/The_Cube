"""Validate backend-authored wording without changing or generating its text."""


def approved_response_options(response, alternatives=()):
    """Keep the canonical response first, then valid distinct alternatives."""
    if not isinstance(response, str) or not response.strip() or len(response) > 500:
        # ai_turn already supplies its safe canonical fallback for this case.
        return []
    options = [response]
    if isinstance(alternatives, (list, tuple)):
        for text in alternatives:
            if isinstance(text, str) and text.strip() and len(text) <= 500 and text not in options:
                options.append(text)
    return options
