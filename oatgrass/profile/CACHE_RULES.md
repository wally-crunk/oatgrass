# Profile Cache Rules

This module caches profile list results in-memory for the current process run.

Rules:

1. A fetched list (`snatched`, `uploaded`, or `downloaded`) is reusable until the program exits.
2. The cache is scoped to tracker + list type. A list fetched from RED is never reused for OPS.
3. The cache is refreshed only when the user explicitly requests refetch (future menu option flow).
4. Cache consumers should treat entries as deterministic for the run, unless a manual refresh replaces them.

Operational note:

- The retriever itself always supports direct refetch by calling `fetch()` again.
- Higher-level menu/controller logic is responsible for deciding when to reuse cached data versus invoking a refresh.
