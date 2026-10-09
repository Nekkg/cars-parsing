"""Shared generation selection for current, saved and legacy search snapshots."""

def selected_generations(filters):
    vehicle = filters.get('vehicle', {})
    if 'generations' in vehicle:
        return vehicle['generations']
    generation = vehicle.get('generation')
    return [generation] if generation else []


def effective_year_bounds(filters):
    """Envelope of selected production periods intersected with user years.

    An inverted pair denotes no intersection, including a range inside a gap.
    """
    from .filter_values import bounds
    lo, hi = bounds(filters.get('year'))
    generations = selected_generations(filters) or filters.get('generation_years', [])
    if not generations:
        return lo, hi
    periods = []
    for row in generations:
        start, end = row.get('year_from'), row.get('year_to')
        if lo is not None:
            start = max(lo, start) if start is not None else lo
        if hi is not None:
            end = min(hi, end) if end is not None else hi
        if start is None or end is None or start <= end:
            periods.append((start, end))
    if not periods:
        return 1, 0
    starts, ends = zip(*periods)
    return (min(starts) if all(value is not None for value in starts) else None,
            max(ends) if all(value is not None for value in ends) else None)


def in_selected_years(year, filters):
    """Keep unknown years for card enrichment, exclude gaps between selected periods."""
    generations = selected_generations(filters) or filters.get('generation_years', [])
    if not generations or not str(year).isdigit():return True
    year = int(year)
    return any((row.get('year_from') is None or year >= row['year_from']) and
               (row.get('year_to') is None or year <= row['year_to']) for row in generations)
