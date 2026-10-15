"""Normalize optional input budgets while preserving omitted partial-update fields."""


def normalize_input_budget_settings(
    settings: dict, *, default_mode: str = "inherit"
) -> dict:
    """Translate numeric legacy requests and clear inactive budgets explicitly."""
    values = dict(settings)
    mode_key, budget_key = "context_input_budget_mode", "context_input_budget"
    if mode_key not in values:
        if budget_key in values:
            values[mode_key] = (
                "custom" if values[budget_key] is not None else default_mode
            )
        return values
    mode = default_mode if values[mode_key] is None else values[mode_key]
    values[mode_key] = mode
    if mode in ("inherit", "unlimited"):
        if values.get(budget_key) is not None:
            raise ValueError("Only a custom input budget can specify a token limit.")
        values[budget_key] = None
    elif mode == "custom" and budget_key in values and values[budget_key] is None:
        raise ValueError("A custom input budget requires a positive token limit.")
    return values
