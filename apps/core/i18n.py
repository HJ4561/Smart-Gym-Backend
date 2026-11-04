"""
Arabic copies of staff-entered text: product and class names, course and machine descriptions,
trainer and nutritionist bios.

A bilingual model keeps `<field>_ar` next to `<field>`. Empty means "show the English": the portal's
getLocalized(item, field) picks `<field>_ar` in Arabic when it has a value. `with_ar` adds the
Arabic copies to an API row; `read_ar` takes them from a request.
"""


def with_ar(obj, *fields):
    """{'name_ar': …, 'description_ar': …} for an API row."""
    return {f'{f}_ar': getattr(obj, f'{f}_ar', '') or '' for f in fields}


def read_ar(obj, data, limits, errors):
    """Copy `<field>_ar` from `data` onto `obj` (trimmed). `limits` = {field: max characters}."""
    for field, limit in limits.items():
        key = f'{field}_ar'
        if key not in data:
            continue
        value = str(data.get(key) or '').strip()
        if len(value) > limit:
            errors[key] = f'Keep it under {limit} characters. · أقل من {limit} حرفًا.'
        setattr(obj, key, value)
