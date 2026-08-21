"""Caching primitives for xrpldashboard.

Introduced in Phase 2 (2026-08-21) as the memory-safe replacement for the
naive homepage SWR cache that OOM'd on 2026-08-15. Design pack lives at
docs/PHASE2_MEMORY_AWARE_CACHE_DESIGN.md.

Public surface:
    from caching.memory_aware_cache import MemoryAwareTTLCache
"""

from caching.memory_aware_cache import MemoryAwareTTLCache

__all__ = ["MemoryAwareTTLCache"]
