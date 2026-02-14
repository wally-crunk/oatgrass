# Profile Search Architecture Decision

Stage 8 decision: **use a second controller for cached profile-list search**.

Chosen approach:

- `oatgrass/profile/search_service.py` provides `run_profile_list_search(...)`.
- It reuses existing lower-level search helpers (`search_with_tiers`, edition parser/matcher/comparison, upload candidate extraction).
- It does not overload `run_search_mode(...)` URL/ID entry points.

Rationale:

1. Cached profile rows are torrent-scoped inputs, not collage/group URL inputs. A dedicated controller keeps this distinction explicit.
2. The dedicated controller can enforce profile-specific constraints (single-source-torrent scope, no sibling expansion) without adding branching complexity to `run_search_mode`.
3. `cli.py` remains a thin director: it selects tracker/list, ensures cache, and delegates to the appropriate controller.
4. Existing lower-level matcher logic is still reused, avoiding duplicate matching algorithms.
