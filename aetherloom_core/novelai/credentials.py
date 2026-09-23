"""Ordered NovelAI tokens, with compatibility for legacy single-key records."""


def tokens_from_record(record):
    """Return stripped, nonempty, unique tokens without changing the record.

    An explicit list is authoritative, including an empty list. A malformed
    list item is ignored rather than converted into a credential string.
    """
    if isinstance(record, dict):
        values = record.get('api_keys')
        if not isinstance(values, list):
            values = [record.get('api_key', '')]
    else:
        values = [record] if isinstance(record, str) else []
    return list(dict.fromkeys(value.strip() for value in values
                              if isinstance(value, str) and value.strip()))


def tokens_for(owner):
    store = getattr(owner, '_apikeys', None)
    return tokens_from_record(store.get('novelai')) if isinstance(store, dict) else []


def token_for(owner):
    tokens = tokens_for(owner)
    return tokens[0] if tokens else ''
