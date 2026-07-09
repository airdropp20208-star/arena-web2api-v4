"""v4.1.0: ensure last_activity field for LRU eviction."""
VERSION = "4.1.0"


def migrate(data: list[dict]) -> list[dict]:
    """Add last_activity field if missing — fallback to updated_at or created_at."""
    for conv in data:
        if "last_activity" not in conv:
            conv["last_activity"] = conv.get("updated_at", conv.get("created_at", 0))
    return data
