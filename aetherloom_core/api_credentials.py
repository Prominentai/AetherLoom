"""Resolve one provider's saved credentials without borrowing another's key."""


def get_credentials(store, provider, category=''):
    store = store if isinstance(store, dict) else {}
    name = str(provider or '')
    if name == 'custom':
        name = 'custom_' + str(category)
    record = store.get(name, {})
    if isinstance(record, str):
        return {'api_key': record.strip()}
    if not isinstance(record, dict):
        return {}
    result = {}
    for field, aliases in (
            ('api_key', ('api_key', 'apikey', 'apiKey', 'key')),
            ('appid', ('appid', 'app_id')),
            ('secret', ('secret', 'app_secret', 'secret_key'))):
        result[field] = next((str(record[key]).strip() for key in aliases if record.get(key)), '')
    return result
