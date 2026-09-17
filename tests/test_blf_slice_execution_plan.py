import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from vet_data_modular.blf_slice_execution import build_slice_execution_plan
from vet_data_modular.blf_slice_models import (
    BlfIndexEntry,
    BlfTimeRangeStatus,
    ConditionSpec,
)
from vet_data_modular.blf_slice_service import (
    BlfSourceChain,
    ConditionCandidateMatch,
    SourceChainBreak,
    SourceChainBuildResult,
)
from vet_data_modular.blf_slice_time import ConditionTimeWindow


class SliceExecutionPlanTests(unittest.TestCase):
    def _condition(self, row, name, start, end):
        condition = ConditionSpec(row, name, name, datetime(2026, 1, 1))
        return condition, ConditionTimeWindow(start, end)

    def _entry(self, path, start, end):
        return BlfIndexEntry(
            path=path,
            time_range_status=BlfTimeRangeStatus.CONFIRMED,
            effective_start_timestamp=start,
            effective_stop_timestamp=end,
        )

    def test_single_source_is_scanned_once_for_multiple_conditions(self):
        path = Path("shared.blf")
        entry = self._entry(path, 0, 30)
        first, first_window = self._condition(2, "A", 0, 10)
        second, second_window = self._condition(3, "B", 5, 15)
        plan = build_slice_execution_plan(
            (
                ConditionCandidateMatch(first_window, (path,)),
                ConditionCandidateMatch(second_window, (path,)),
            ),
            SourceChainBuildResult((BlfSourceChain((entry,), ()),)),
            (first, second),
        )
        self.assertEqual(len(plan.source_scans), 1)
        self.assertEqual(len(plan.source_scans[0].target_ids), 2)
        self.assertEqual(len(plan.targets), 2)
        self.assertTrue(all(not target.coverage_gaps for target in plan.targets))

    def test_condition_spans_continuous_volumes_in_one_target(self):
        first_path, second_path = Path("a.blf"), Path("b.blf")
        first = self._entry(first_path, 0, 10)
        second = self._entry(second_path, 11, 20)
        condition, window = self._condition(2, "跨卷", 2, 19)
        plan = build_slice_execution_plan(
            (ConditionCandidateMatch(window, (first_path, second_path)),),
            SourceChainBuildResult((BlfSourceChain((first, second), (1,)),)),
            (condition,),
        )
        self.assertEqual(plan.targets[0].source_paths, (first_path, second_path))
        self.assertEqual(len(plan.source_scans), 2)
        self.assertEqual(plan.source_scans[0].target_ids, plan.source_scans[1].target_ids)

    def test_source_breaks_produce_separate_outputs_and_coverage_gaps(self):
        a, b = Path("a.blf"), Path("b.blf")
        first, second = self._entry(a, 0, 4), self._entry(b, 10, 20)
        condition, window = self._condition(2, "断点", 0, 20)
        chains = SourceChainBuildResult(
            (BlfSourceChain((first,), ()), BlfSourceChain((second,), ())),
            breaks=(SourceChainBreak(a, b, 6),),
        )
        plan = build_slice_execution_plan(
            (ConditionCandidateMatch(window, (a, b)),), chains, (condition,)
        )
        self.assertEqual(len(plan.targets), 2)
        self.assertTrue(all(target.coverage_gaps for target in plan.targets))

    def test_no_candidates_and_unresolved_are_retained(self):
        condition, window = self._condition(2, "无数据", 0, 10)
        plan = build_slice_execution_plan(
            (ConditionCandidateMatch(window, (), (Path("bad.blf"),)),),
            SourceChainBuildResult(),
            (condition,),
        )
        self.assertEqual(len(plan.targets), 1)
        self.assertEqual(plan.targets[0].source_paths, ())
        self.assertEqual(plan.targets[0].unresolved_paths, (Path("bad.blf"),))
        self.assertEqual(plan.source_scans, ())


if __name__ == "__main__":
    unittest.main()
