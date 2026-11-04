"""Core implementation for aligning log records to a reference clock.

WHY THIS MODULE EXISTS
----------------------
Logs from different machines carry timestamps from unsynchronised clocks.
A common task is to compare them: which event happened first?  The answer
is not obvious when each source has its own offset and drift.  This module
provides a small, dependency-free way to estimate a per-source shift that
maps that source's timestamps onto a chosen reference source's clock, and
to apply that shift to whole records.

DESIGN CHOICE
-------------
The shift is *monotonic*: once a source has been aligned, later alignments
only ever move its timestamps forward or backward relative to the reference,
never both in an inconsistent way.  Concretely, the offset is estimated from
the median of pairwise differences between reference and source records that
share a correlation key (for example a request id).  A median is used rather
than a mean because log timestamps routinely contain outliers (a record
stamped after a retry, a clock jump); the median is robust to them.

Only a single scalar offset per source is supported.  Clock *drift* would
require a linear model, which is deliberately out of scope: the brief asks
for monotonic shifting, and a scalar shift is the simplest thing that can
satisfy it.  If a source drifts, its records will still be monotonic among
themselves because the same offset is applied to all of them.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Record:
    """A single log record.

    Attributes:
        source: Identifier of the emitting source (host, service, file).
        timestamp: Seconds since the epoch, as a float.  Using a float rather
            than a datetime keeps the arithmetic obvious and makes it easy to
            feed in monotonic counters for tests.
        key: Optional correlation key shared by records that describe the same
            logical event across sources.  Alignment is only possible for a
            source that has at least one key in common with the reference.
        payload: Arbitrary user data, carried through untouched.
    """

    source: str
    timestamp: float
    key: Optional[str] = None
    payload: object = None


@dataclass(frozen=True)
class AlignedRecord:
    """A record after alignment, with the applied offset recorded.

    Keeping the original timestamp and the offset separate (rather than just
    overwriting the timestamp) makes it possible to audit how a record moved
    and to re-align later without losing information.
    """

    record: Record
    aligned_timestamp: float
    offset: float


class Aligner:
    """Align records from several sources onto one reference clock.

    The reference source is chosen explicitly.  Its records are never shifted.
    Every other source is shifted by a single offset estimated from the
    median difference between its keyed records and the reference's keyed
    records.

    Sources with no key in common with the reference are left unshifted
    (offset 0.0) and reported in :meth:`unaligned_sources`; this is a
    deliberate, visible failure mode rather than a silent guess.
    """

    def __init__(self, reference_source: str) -> None:
        if not isinstance(reference_source, str) or not reference_source:
            raise ValueError("reference_source must be a non-empty string")
        self.reference_source = reference_source
        self._records: List[Record] = []
        self._offsets: Dict[str, float] = {reference_source: 0.0}
        self._aligned: bool = False

    # -- ingestion --------------------------------------------------------

    def add(self, record: Record) -> None:
        """Add one record.  Adding after :meth:`align` invalidates offsets."""
        if not isinstance(record, Record):
            raise TypeError("record must be a Record instance")
        self._records.append(record)
        self._aligned = False

    def extend(self, records: Iterable[Record]) -> None:
        """Add many records.  Equivalent to calling :meth:`add` repeatedly."""
        for record in records:
            self.add(record)

    # -- alignment --------------------------------------------------------

    def align(self) -> None:
        """Estimate offsets for every source from the records seen so far.

        Idempotent: calling it twice without adding records recomputes the
        same offsets.  Sources with no shared key are given offset 0.0.
        """
        reference_times: Dict[str, float] = {}
        for record in self._records:
            if record.source == self.reference_source and record.key is not None:
                # First keyed record wins.  Logs may repeat a key; taking the
                # first occurrence keeps the result deterministic regardless
                # of the order in which duplicates were ingested.
                reference_times.setdefault(record.key, record.timestamp)

        candidates: Dict[str, List[float]] = {}
        for record in self._records:
            if record.source == self.reference_source or record.key is None:
                continue
            ref_time = reference_times.get(record.key)
            if ref_time is None:
                continue
            candidates.setdefault(record.source, []).append(
                ref_time - record.timestamp
            )

        offsets: Dict[str, float] = {self.reference_source: 0.0}
        for source, deltas in candidates.items():
            offsets[source] = _median(deltas)

        # Any source that appeared but produced no candidates is explicitly
        # left at zero so that unaligned_sources() can report it.
        seen_sources = {record.source for record in self._records}
        for source in seen_sources:
            offsets.setdefault(source, 0.0)

        self._offsets = offsets
        self._aligned = True

    def _require_aligned(self) -> None:
        if not self._aligned:
            raise RuntimeError("align() must be called before querying offsets")

    # -- queries ----------------------------------------------------------

    def offset_for(self, source: str) -> float:
        """Return the offset applied to ``source``.

        Raises ``KeyError`` for a source that has never been seen.
        """
        self._require_aligned()
        if source not in self._offsets:
            raise KeyError(source)
        return self._offsets[source]

    def offsets(self) -> Dict[str, float]:
        """Return a copy of the per-source offsets."""
        self._require_aligned()
        return dict(self._offsets)

    def unaligned_sources(self) -> List[str]:
        """Sources that were seen but could not be aligned to the reference.

        A source is unaligned when it shares no correlation key with the
        reference.  Its offset is reported as 0.0 by :meth:`offset_for`.
        """
        self._require_aligned()
        return sorted(
            source
            for source, offset in self._offsets.items()
            if source != self.reference_source and offset == 0.0
        )

    def aligned(self) -> List[AlignedRecord]:
        """Return every ingested record with its aligned timestamp.

        Records are returned in the order they were added, not sorted by
        time: reordering is a separate concern and doing it implicitly would
        surprise callers who rely on ingestion order.
        """
        self._require_aligned()
        result: List[AlignedRecord] = []
        for record in self._records:
            offset = self._offsets.get(record.source, 0.0)
            result.append(
                AlignedRecord(
                    record=record,
                    aligned_timestamp=record.timestamp + offset,
                    offset=offset,
                )
            )
        return result

    def aligned_for(self, source: str) -> List[AlignedRecord]:
        """Return aligned records for one source, in ingestion order."""
        self._require_aligned()
        if source not in self._offsets:
            raise KeyError(source)
        offset = self._offsets[source]
        return [
            AlignedRecord(
                record=record,
                aligned_timestamp=record.timestamp + offset,
                offset=offset,
            )
            for record in self._records
            if record.source == source
        ]


def _median(values: Sequence[float]) -> float:
    """Return the median of a non-empty sequence of floats.

    For an even-length sequence the mean of the two middle values is used.
    The input is copied before sorting so the caller's list is untouched.
    """
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0
