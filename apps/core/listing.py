"""
Server-side filtering and sorting for list endpoints, one syntax everywhere:

    ?sort=-created_at              newest first (a leading "-" = descending)
    ?filter_status=paid            exact filters, one per field the endpoint allows
    ?filter_after=2026-10-01       date / number / yes-no filters are converted and checked

Each endpoint declares what may be sorted and filtered (names → model lookups), so a request can
never filter or order by arbitrary columns. A bad value answers 400 with the reason.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import ValidationError

TRUE, FALSE = {'1', 'true', 'yes', 'on'}, {'0', 'false', 'no', 'off'}


def _cast(name, raw, kind):
    if kind == 'bool':
        v = str(raw).lower()
        if v in TRUE:
            return True
        if v in FALSE:
            return False
        raise ValidationError({f'filter_{name}': 'Use true or false.'})
    if kind == 'int':
        if not str(raw).isdigit():
            raise ValidationError({f'filter_{name}': 'Use a number.'})
        return int(raw)
    if kind == 'decimal':
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            raise ValidationError({f'filter_{name}': 'Use an amount.'})
    if kind == 'date':
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            raise ValidationError({f'filter_{name}': 'Use a date (YYYY-MM-DD).'})
    if kind == 'list':
        return [v for v in str(raw).split(',') if v]
    return str(raw)


def apply(qs, params, *, sorts=None, filters=None, default_sort=None):
    """
    `sorts`:   {"name": "model__lookup"}           → ?sort=name / ?sort=-name
    `filters`: {"name": ("model__lookup", kind)}   → ?filter_name=value   (kind: str/int/bool/date/decimal/list)
    Returns the filtered, ordered queryset. Raises ValidationError (→ 400) on unknown sort fields or bad values.
    """
    for name, (lookup, kind) in (filters or {}).items():
        raw = params.get(f'filter_{name}')
        if raw in (None, ''):
            continue
        qs = qs.filter(**{lookup: _cast(name, raw, kind)})
    sort = params.get('sort') or default_sort
    if sort:
        key = sort.lstrip('-')
        if key not in (sorts or {}):
            raise ValidationError({'sort': f'Sort by one of: {", ".join(sorted(sorts or {}))}.'})
        qs = qs.order_by(f"{'-' if sort.startswith('-') else ''}{sorts[key]}", '-pk' if sort.startswith('-') else 'pk')
    return qs

