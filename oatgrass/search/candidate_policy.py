"""Policy rules for candidate eligibility, suppression, and score adjustments."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import re

from oatgrass.search.upload_candidates import UploadCandidate


class CandidatePolicy(str, Enum):
    STANDARD = "standard"
    PERFECT = "perfect"
    PERFECTER = "perfecter"


@dataclass(frozen=True)
class SuppressedCandidate:
    candidate: UploadCandidate
    reason_code: str
    reason_text: str


@dataclass
class PolicySummary:
    promoted: int = 0
    demoted: int = 0
    excluded_by_policy: int = 0
    duplicate_24bit: int = 0

    @property
    def suppressed_total(self) -> int:
        return self.excluded_by_policy + self.duplicate_24bit

    def merge(self, other: "PolicySummary") -> None:
        self.promoted += other.promoted
        self.demoted += other.demoted
        self.excluded_by_policy += other.excluded_by_policy
        self.duplicate_24bit += other.duplicate_24bit


@dataclass(frozen=True)
class PolicyOutcome:
    candidates: list[UploadCandidate]
    suppressed: list[SuppressedCandidate]
    summary: PolicySummary


@dataclass(frozen=True)
class _CandidateDecision:
    suppressed: bool
    reason_code: str | None = None
    reason_text: str | None = None
    adjustment: int = 0


_MISSING_LINEAGE_RE = re.compile(r"(missing lineage|lineage missing|no[\s_-]*lineage)", re.IGNORECASE)
_BAD_NO_CHECKSUM_RE = re.compile(r"(bad[\s_-]*no[\s_-]*checksum|no[\s_-]*checksum)", re.IGNORECASE)
_PERFECT_LINEAGE_MEDIA = {"vinyl", "sacd", "dat", "cassette"}
_PERFECTER_ALLOWED_MEDIA = {"cd", "vinyl"}


def parse_candidate_policy(*, perfect: bool, perfecter: bool) -> CandidatePolicy:
    """Resolve policy from CLI-style flags. perfecter wins when both are set."""
    if perfecter:
        return CandidatePolicy.PERFECTER
    if perfect:
        return CandidatePolicy.PERFECT
    return CandidatePolicy.STANDARD


def apply_candidate_policy(
    candidates: list[UploadCandidate],
    *,
    policy: CandidatePolicy,
) -> PolicyOutcome:
    if policy == CandidatePolicy.STANDARD:
        sorted_candidates = sorted(candidates, key=lambda c: c.priority, reverse=True)
        return PolicyOutcome(candidates=sorted_candidates, suppressed=[], summary=PolicySummary())

    summary = PolicySummary()
    scored_candidates: list[tuple[UploadCandidate, int]] = []
    suppressed: list[SuppressedCandidate] = []

    for candidate in candidates:
        decision = _evaluate_candidate(candidate, policy=policy)
        if decision.suppressed:
            suppressed.append(
                SuppressedCandidate(
                    candidate=candidate,
                    reason_code=decision.reason_code or "suppressed",
                    reason_text=decision.reason_text or "Suppressed by policy.",
                )
            )
            summary.excluded_by_policy += 1
            continue

        scored_candidates.append(
            (
                replace(candidate, priority=candidate.priority + decision.adjustment),
                decision.adjustment,
            )
        )

    if policy == CandidatePolicy.PERFECTER:
        filtered, perfecter_suppressed, duplicate_count = _apply_perfecter_vinyl_rules(
            [candidate for candidate, _ in scored_candidates]
        )
        suppressed.extend(perfecter_suppressed)
        summary.excluded_by_policy += sum(
            1 for item in perfecter_suppressed if item.reason_code == "excluded_vinyl_not_24bit"
        )
        summary.duplicate_24bit += duplicate_count
        kept_ids = {candidate.source_torrent.torrent_id for candidate in filtered}
        scored_candidates = [
            (candidate, adjustment)
            for candidate, adjustment in scored_candidates
            if candidate.source_torrent.torrent_id in kept_ids
        ]

    accepted = [candidate for candidate, _ in scored_candidates]
    for _candidate, adjustment in scored_candidates:
        if adjustment > 0:
            summary.promoted += 1
        elif adjustment < 0:
            summary.demoted += 1

    accepted.sort(key=lambda c: c.priority, reverse=True)
    return PolicyOutcome(candidates=accepted, suppressed=suppressed, summary=summary)


def _evaluate_candidate(candidate: UploadCandidate, *, policy: CandidatePolicy) -> _CandidateDecision:
    media = (candidate.media or "").strip().lower()
    source = candidate.source_torrent

    if not _is_flac(candidate):
        return _CandidateDecision(
            suppressed=True,
            reason_code="excluded_non_flac",
            reason_text="Suppressed: non-FLAC encoding is excluded by policy.",
        )

    if policy == CandidatePolicy.PERFECTER:
        if media not in _PERFECTER_ALLOWED_MEDIA:
            return _CandidateDecision(
                suppressed=True,
                reason_code="excluded_media",
                reason_text=f"Suppressed: media '{candidate.media}' is excluded in perfecter mode.",
            )

    if media == "cd":
        is_perfect = _is_perfect_cd(candidate)
    elif media in _PERFECT_LINEAGE_MEDIA:
        if policy == CandidatePolicy.PERFECTER and media == "vinyl" and not _is_24bit(candidate):
            # Perfecter prefers 24-bit Vinyl, but 16-bit Vinyl can remain when no 24-bit exists.
            is_perfect = False
        else:
            is_perfect = not _has_missing_lineage(source)
    else:
        # WEB FLAC is allowed in perfect mode but does not count as perfect quality.
        is_perfect = False

    return _CandidateDecision(suppressed=False, adjustment=20 if is_perfect else -20)


def _apply_perfecter_vinyl_rules(
    candidates: list[UploadCandidate],
) -> tuple[list[UploadCandidate], list[SuppressedCandidate], int]:
    vinyl_by_edition: dict[tuple[int | None, int, str], list[UploadCandidate]] = {}
    for candidate in candidates:
        media = (candidate.media or "").strip().lower()
        if media != "vinyl":
            continue
        vinyl_by_edition.setdefault(_edition_key(candidate), []).append(candidate)

    suppressed: list[SuppressedCandidate] = []
    drop_ids: set[int] = set()
    duplicate_count = 0
    for group in vinyl_by_edition.values():
        vinyl_24bit = [candidate for candidate in group if _is_24bit(candidate)]
        if not vinyl_24bit:
            continue

        for candidate in group:
            if _is_24bit(candidate):
                continue
            drop_ids.add(candidate.source_torrent.torrent_id)
            suppressed.append(
                SuppressedCandidate(
                    candidate=candidate,
                    reason_code="excluded_vinyl_not_24bit",
                    reason_text="Suppressed: vinyl is not 24-bit and 24-bit Vinyl exists in the same edition.",
                )
            )

        if len(vinyl_24bit) <= 1:
            continue
        winner = max(
            vinyl_24bit,
            key=lambda c: (
                c.priority,
                c.size,
                -c.source_torrent.torrent_id,
            ),
        )
        for candidate in vinyl_24bit:
            if candidate.source_torrent.torrent_id == winner.source_torrent.torrent_id:
                continue
            drop_ids.add(candidate.source_torrent.torrent_id)
            duplicate_count += 1
            suppressed.append(
                SuppressedCandidate(
                    candidate=candidate,
                    reason_code="duplicate_vinyl_24bit",
                    reason_text=(
                        "Suppressed: duplicate 24-bit Vinyl in the same edition; "
                        f"kept torrent #{winner.source_torrent.torrent_id}."
                    ),
                )
            )

    filtered = [candidate for candidate in candidates if candidate.source_torrent.torrent_id not in drop_ids]
    return filtered, suppressed, duplicate_count


def _edition_key(candidate: UploadCandidate) -> tuple[int | None, int, str]:
    return (
        candidate.edition_id,
        candidate.edition_year,
        candidate.edition_title,
    )


def _is_flac(candidate: UploadCandidate) -> bool:
    return "flac" in (candidate.source_torrent.format or "").lower()


def _is_24bit(candidate: UploadCandidate) -> bool:
    return "24bit" in (candidate.encoding or "").replace("-", "").lower()


def _is_perfect_cd(candidate: UploadCandidate) -> bool:
    source = candidate.source_torrent
    if _is_24bit(candidate):
        return False
    return (
        bool(source.has_log)
        and bool(source.has_cue)
        and int(source.log_score or 0) == 100
        and _has_cd_checksum(source)
    )


def _has_missing_lineage(source_torrent) -> bool:
    reasons = [str(item) for item in (source_torrent.trumpable_reasons or [])]
    for reason in reasons:
        if _MISSING_LINEAGE_RE.search(reason):
            return True

    description = source_torrent.description or ""
    if description and _MISSING_LINEAGE_RE.search(description):
        return True
    return False


def _has_cd_checksum(source_torrent) -> bool:
    if source_torrent.log_checksum is not None:
        return bool(source_torrent.log_checksum)

    reasons = [str(item) for item in (source_torrent.trumpable_reasons or [])]
    if any(_BAD_NO_CHECKSUM_RE.search(reason) for reason in reasons):
        return False

    # RED may omit logChecksum while still exposing overall trumpable status.
    return source_torrent.trumpable is False
