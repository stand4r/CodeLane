"""Context size estimates and provider token accounting."""


CONTEXT_COMPACTION_BYTES = 220_000


def clip_utf8(text, limit):
    return text[:limit].encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")


def request_size(messages, tools=()):
    """Size of model-visible text and schemas, with an allowance for images."""
    pending = [messages, tools]
    size = 0
    while pending and size <= CONTEXT_COMPACTION_BYTES:
        value = pending.pop()
        if isinstance(value, str):
            remaining = CONTEXT_COMPACTION_BYTES - size + 1
            size += len(value[:remaining].encode("utf-8", errors="replace")) + 2
        elif isinstance(value, dict):
            if value.get("type") in ("image", "image_url"):
                size += 8_000
            else:
                size += 2 + len(value)
                pending.extend(value.keys())
                pending.extend(value.values())
        elif isinstance(value, (tuple, list)):
            size += 2 + len(value)
            pending.extend(value)
        else:
            size += len(str(value))
    return size


def input_tokens(usage):
    for key in ("measured_input_tokens", "prompt_tokens", "input_tokens"):
        incoming = usage.get(key)
        if type(incoming) is not int or incoming < 0:
            continue
        if key == "input_tokens" and not isinstance(usage.get("input_tokens_details"), dict):
            incoming += sum(
                usage[field] for field in ("cache_creation_input_tokens", "cache_read_input_tokens")
                if type(usage.get(field)) is int and usage[field] >= 0
            )
        return incoming
    return None


def total_tokens(usage):
    if type(usage.get("total_tokens")) is int:
        return usage["total_tokens"]
    incoming = input_tokens(usage)
    outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
    if type(outgoing) is not int:
        outgoing = None
    if incoming is None and outgoing is None:
        return None
    return (incoming or 0) + (outgoing or 0)
