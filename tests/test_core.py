import unittest

from log_record_time_aligner import Aligner, AlignedRecord, Record


class TestRecord(unittest.TestCase):
    def test_record_defaults(self):
        r = Record(source="a", timestamp=1.0)
        self.assertIsNone(r.key)
        self.assertIsNone(r.payload)

    def test_record_is_frozen(self):
        r = Record(source="a", timestamp=1.0)
        with self.assertRaises(Exception):
            r.timestamp = 2.0  # type: ignore[misc]


class TestAlignerConstruction(unittest.TestCase):
    def test_reference_source_must_be_non_empty_string(self):
        with self.assertRaises(ValueError):
            Aligner("")
        with self.assertRaises(ValueError):
            Aligner(None)  # type: ignore[arg-type]

    def test_queries_before_align_raise(self):
        a = Aligner("ref")
        with self.assertRaises(RuntimeError):
            a.offsets()
        with self.assertRaises(RuntimeError):
            a.aligned()
        with self.assertRaises(RuntimeError):
            a.unaligned_sources()
        with self.assertRaises(RuntimeError):
            a.offset_for("ref")


class TestAlignment(unittest.TestCase):
    def test_reference_records_are_never_shifted(self):
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.align()
        self.assertEqual(a.offset_for("ref"), 0.0)
        out = a.aligned()
        self.assertEqual(out[0].aligned_timestamp, 100.0)
        self.assertEqual(out[0].offset, 0.0)

    def test_single_offset_from_shared_key(self):
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="other", timestamp=90.0, key="k1"))
        a.align()
        self.assertAlmostEqual(a.offset_for("other"), 10.0)

    def test_median_is_robust_to_outlier(self):
        # Three shared keys; one source timestamp is wildly off.  The median
        # difference is 10.0, not the mean (which would be ~1000).
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="ref", timestamp=200.0, key="k2"))
        a.add(Record(source="ref", timestamp=300.0, key="k3"))
        a.add(Record(source="other", timestamp=90.0, key="k1"))
        a.add(Record(source="other", timestamp=190.0, key="k2"))
        a.add(Record(source="other", timestamp=-2700.0, key="k3"))
        a.align()
        self.assertAlmostEqual(a.offset_for("other"), 10.0)

    def test_even_number_of_differences_averages_middle_two(self):
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="ref", timestamp=200.0, key="k2"))
        a.add(Record(source="other", timestamp=90.0, key="k1"))
        a.add(Record(source="other", timestamp=180.0, key="k2"))
        a.align()
        # Differences are 10 and 20; median is 15.
        self.assertAlmostEqual(a.offset_for("other"), 15.0)

    def test_first_reference_occurrence_wins_for_duplicate_keys(self):
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="ref", timestamp=999.0, key="k1"))
        a.add(Record(source="other", timestamp=90.0, key="k1"))
        a.align()
        self.assertAlmostEqual(a.offset_for("other"), 10.0)

    def test_source_without_shared_key_is_reported_unaligned(self):
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="lonely", timestamp=50.0, key="other-key"))
        a.add(Record(source="keyless", timestamp=5.0))
        a.align()
        self.assertEqual(a.offset_for("lonely"), 0.0)
        self.assertEqual(a.offset_for("keyless"), 0.0)
        self.assertEqual(a.unaligned_sources(), ["keyless", "lonely"])

    def test_unknown_source_raises_key_error(self):
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=1.0, key="k"))
        a.align()
        with self.assertRaises(KeyError):
            a.offset_for("ghost")
        with self.assertRaises(KeyError):
            a.aligned_for("ghost")

    def test_offsets_returns_a_copy(self):
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="other", timestamp=90.0, key="k1"))
        a.align()
        snapshot = a.offsets()
        snapshot["other"] = 12345.0
        self.assertAlmostEqual(a.offset_for("other"), 10.0)

    def test_adding_after_align_invalidates_offsets(self):
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="other", timestamp=90.0, key="k1"))
        a.align()
        a.add(Record(source="other", timestamp=0.0, key="k2"))
        with self.assertRaises(RuntimeError):
            a.offsets()
        a.align()
        # k2 exists only on "other", so it does not contribute a difference.
        self.assertAlmostEqual(a.offset_for("other"), 10.0)

    def test_align_is_idempotent(self):
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="other", timestamp=90.0, key="k1"))
        a.align()
        first = a.offsets()
        a.align()
        self.assertEqual(first, a.offsets())

    def test_extend_matches_repeated_add(self):
        records = [
            Record(source="ref", timestamp=100.0, key="k1"),
            Record(source="other", timestamp=90.0, key="k1"),
        ]
        a = Aligner("ref")
        a.extend(records)
        a.align()
        self.assertAlmostEqual(a.offset_for("other"), 10.0)

    def test_add_rejects_non_record(self):
        a = Aligner("ref")
        with self.assertRaises(TypeError):
            a.add("not a record")  # type: ignore[arg-type]


class TestAlignedOutput(unittest.TestCase):
    def test_aligned_preserves_ingestion_order(self):
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=300.0, key="k3"))
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="other", timestamp=90.0, key="k1"))
        a.align()
        out = a.aligned()
        self.assertEqual([r.record.timestamp for r in out], [300.0, 100.0, 90.0])

    def test_aligned_timestamps_use_offset(self):
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="other", timestamp=90.0, key="k1"))
        a.add(Record(source="other", timestamp=95.0, key="k2"))
        a.align()
        out = a.aligned_for("other")
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0].aligned_timestamp, 100.0)
        self.assertAlmostEqual(out[1].aligned_timestamp, 105.0)
        for item in out:
            self.assertIsInstance(item, AlignedRecord)
            self.assertAlmostEqual(item.offset, 10.0)

    def test_aligned_carries_payload_untouched(self):
        payload = {"message": "hello"}
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="other", timestamp=90.0, key="k1", payload=payload))
        a.align()
        out = a.aligned_for("other")
        self.assertIs(out[0].record.payload, payload)

    def test_monotonic_shift_applies_uniformly(self):
        # Two records from the same source must keep their relative order and
        # spacing after alignment, because a single scalar offset is applied.
        a = Aligner("ref")
        a.add(Record(source="ref", timestamp=100.0, key="k1"))
        a.add(Record(source="other", timestamp=50.0, key="k1"))
        a.add(Record(source="other", timestamp=70.0, key="k2"))
        a.align()
        out = a.aligned_for("other")
        gap_before = 70.0 - 50.0
        gap_after = out[1].aligned_timestamp - out[0].aligned_timestamp
        self.assertAlmostEqual(gap_before, gap_after)


if __name__ == "__main__":
    unittest.main()
