# log-record-time-aligner

Aligns records from different sources by monotonically shifting their timestamps onto a shared reference clock.

## Usage

```python
from log_record_time_aligner import Aligner, Record

aligner = Aligner(reference_source="api")
aligner.extend([
    Record(source="api", timestamp=100.0, key="req-1"),
    Record(source="worker", timestamp=90.0, key="req-1"),
    Record(source="worker", timestamp=95.0, key="req-2"),
])
aligner.align()

aligner.offset_for("worker")          # 10.0
for item in aligner.aligned_for("worker"):
    print(item.record.timestamp, "->", item.aligned_timestamp)
```

Exported names: `Aligner`, `Record`, `AlignedRecord`.

## Why this exists

Logs from different machines carry timestamps from unsynchronised clocks, so a naive merge can put a response before its request. The usual fix is to pick one source as the reference and shift every other source by a single offset. This library estimates that offset from the median of the differences between records that share a correlation key (a request id, say) and applies it to the whole source.

The trade-off is that only a scalar offset per source is supported. Clock drift would need a linear model, and a linear model would make the shift non-monotonic across sources in ways that are hard to reason about. A scalar offset keeps every source's internal ordering and spacing intact, which is what most log inspection actually needs.

## The awkward edge

A source that shares no correlation key with the reference cannot be aligned. It is left at offset `0.0` and listed by `unaligned_sources()` rather than being silently guessed at. If your logs do not carry a shared key, this library will report every non-reference source as unaligned; you will need to add a key to your records first.

Duplicate keys are resolved by taking the first reference record seen for that key, so re-ingesting a log file in a different order does not change the offsets.

## Performance

The window keeps a bounded buffer, so `push` is constant time and memory does not
grow with the length of the stream. `peak` and `trough` are linear in the window
size, which is the trade that keeps `push` cheap.

