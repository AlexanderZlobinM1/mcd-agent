from __future__ import annotations

from collections import deque


def reconcile_ring(old_ring: deque[int] | None, ordered_ids: list[int], *, new_to_front: bool = False) -> deque[int]:
    if old_ring is None or not old_ring:
        return deque(ordered_ids)
    old = list(old_ring)
    new_set = set(ordered_ids)
    keep = [x for x in old if x in new_set]
    keep_set = set(keep)
    new_items = [x for x in ordered_ids if x not in keep_set]
    if new_to_front:
        return deque(new_items + keep)
    return deque(keep + new_items)
