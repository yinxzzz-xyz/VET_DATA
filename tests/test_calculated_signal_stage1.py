import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from asammdf import Signal

from vet_data_modular.calculated_signal import (
    CALCULATED_SIGNAL_SCHEMA_VERSION,
    CalculationStatus,
    CalculatedSignalDefinition,
    CalculatedSignalResult,
)
from vet_data_modular.signal_resolver import (
    SampleKind,
    SignalKeyNotFoundError,
    SignalResolver,
    SignalSource,
)


def _info(name, *, display_name=None, group=-1, channel=-1, unit="", comment="test"):
    return {
        "name": name,
        "display_name": display_name or name,
        "group": group,
        "channel": channel,
        "unit": unit,
        "comment": comment,
    }


class _FakeMdf:
    def __init__(self, signals):
        self.signals = signals
        self.calls = []

    def get(self, name, group=None, index=None):
        self.calls.append((name, group, index))
        return self.signals[(name, group, index)]


class CalculatedSignalModelTests(unittest.TestCase):
    def test_definition_defaults_and_json_compatible_serialization(self):
        definition = CalculatedSignalDefinition(
            stable_id="calc-001", display_name="Power", user_formula="[Torque] * [Speed]"
        )
        serialized = definition.to_dict()
        self.assertEqual(serialized["schema_version"], CALCULATED_SIGNAL_SCHEMA_VERSION)
        self.assertEqual(serialized["normalized_formula"], "")
        self.assertEqual(serialized["dependencies"], [])
        self.assertEqual(serialized["alignment_policy"], "intersection_union")
        self.assertEqual(serialized["numeric_policy"]["invalid"], "nan")
        json.dumps(serialized)

    def test_definition_preserves_tokens_dependencies_policies_and_metadata(self):
        definition = CalculatedSignalDefinition(
            stable_id="calc-002",
            display_name="Wheel Power",
            user_formula="S001*S002",
            normalized_formula="S001 * S002",
            signal_tokens={"S001": "Torque_G1_C2", "S002": "Speed_G3_C4"},
            dependencies=("Torque_G1_C2", "Speed_G3_C4"),
            result_unit="kW",
            alignment_policy="future-policy",
            interpolation_policy={"default": "future"},
            numeric_policy={"invalid": "nan"},
            comment="formula definition",
        )
        serialized = definition.to_dict()
        self.assertEqual(serialized["signal_tokens"]["S001"], "Torque_G1_C2")
        self.assertEqual(serialized["dependencies"], ["Torque_G1_C2", "Speed_G3_C4"])
        self.assertEqual(serialized["result_unit"], "kW")
        self.assertEqual(serialized["comment"], "formula definition")

    def test_success_result_holds_arrays_counts_and_diagnostics(self):
        result = CalculatedSignalResult(
            timestamps=np.array([0.0, 1.0]),
            samples=np.array([2.0, np.nan]),
            unit="kW",
            status=CalculationStatus.SUCCESS,
            nan_count=1,
            diagnostics={"valid_count": 1},
        )
        np.testing.assert_array_equal(result.timestamps, [0.0, 1.0])
        self.assertEqual(result.status, CalculationStatus.SUCCESS)
        self.assertEqual(result.nan_count, 1)
        self.assertEqual(result.diagnostics["valid_count"], 1)

    def test_error_result_has_explicit_status_error_and_diagnostics(self):
        result = CalculatedSignalResult(
            status=CalculationStatus.ERROR,
            error="missing signal",
            inf_count=2,
            diagnostics={"missing": ["S001"]},
        )
        self.assertEqual(result.status, CalculationStatus.ERROR)
        self.assertEqual(result.error, "missing signal")
        self.assertEqual(result.inf_count, 2)
        self.assertEqual(result.samples.size, 0)


class SignalResolverTests(unittest.TestCase):
    def test_resolver_module_has_no_gui_dependency(self):
        source = Path(__file__).parents[1] / "vet_data" / "vet_data_modular" / "signal_resolver.py"
        self.assertNotIn("PyQt", source.read_text(encoding="utf-8"))

    def test_csv_signal_samples_timestamps_unit_and_metadata(self):
        frame = pd.DataFrame({"speed": [1, 2]}, index=[0.1, 0.3])
        catalog = {"csv-speed": _info("speed", display_name="Vehicle Speed", unit="km/h", comment="CSV speed")}
        result = SignalResolver(catalog, data_source=frame, data_path="input.csv").resolve("csv-speed")
        self.assertEqual(result.source, SignalSource.CSV)
        self.assertEqual(result.key, "csv-speed")
        self.assertEqual(result.name, "speed")
        self.assertEqual(result.display_name, "Vehicle Speed")
        self.assertEqual(result.unit, "km/h")
        self.assertEqual(result.comment, "CSV speed")
        np.testing.assert_array_equal(result.samples, [1.0, 2.0])
        np.testing.assert_array_equal(result.timestamps, [0.1, 0.3])

    def test_vbo_signal_uses_same_dataframe_contract_with_distinct_source(self):
        frame = pd.DataFrame({"velocity": [3, 4]}, index=[0.0, 0.2])
        result = SignalResolver(
            {"velocity": _info("velocity")}, data_source=frame, data_path="run.vbo"
        ).resolve("velocity")
        self.assertEqual(result.source, SignalSource.VBO)
        np.testing.assert_array_equal(result.samples, [3.0, 4.0])

    def test_can_signal_is_resolved_without_file_data_source(self):
        catalog = {"CAN_1_Rpm": _info("CAN_1_Rpm", display_name="RPM", comment="decoded")}
        can_data = {
            "CAN_1_Rpm": {
                "samples": np.array([1000, 1100]),
                "timestamps": np.array([1.0, 1.1]),
                "unit": "rpm",
            }
        }
        result = SignalResolver(catalog, can_data=can_data).resolve("CAN_1_Rpm")
        self.assertEqual(result.source, SignalSource.CAN)
        self.assertEqual(result.unit, "rpm")
        np.testing.assert_array_equal(result.timestamps, [1.0, 1.1])

    def test_legacy_math_signal_is_resolved_with_existing_math_unit(self):
        catalog = {"MATH_sum": _info("MATH_sum", display_name="Sum")}
        math_data = {
            "MATH_sum": {
                "samples": np.array([4.0, 6.0]),
                "timestamps": np.array([0.0, 1.0]),
            }
        }
        result = SignalResolver(catalog, legacy_math_data=math_data).resolve("MATH_sum")
        self.assertEqual(result.source, SignalSource.LEGACY_MATH)
        self.assertEqual(result.unit, "Math")
        np.testing.assert_array_equal(result.samples, [4.0, 6.0])

    def test_mdf_signal_uses_unique_group_channel_and_preserves_metadata(self):
        first = Signal(samples=np.array([1]), timestamps=np.array([0.0]), name="duplicate", unit="A")
        second = Signal(samples=np.array([2]), timestamps=np.array([0.0]), name="duplicate", unit="B")
        mdf = _FakeMdf({("duplicate", 1, 2): first, ("duplicate", 3, 4): second})
        catalog = {
            "duplicate_G1_C2": _info("duplicate", group=1, channel=2, comment="first"),
            "duplicate_G3_C4": _info("duplicate", group=3, channel=4, comment="second"),
        }
        resolver = SignalResolver(catalog, data_source=mdf, data_path="input.mf4")
        result_a = resolver.resolve("duplicate_G1_C2")
        result_b = resolver.resolve("duplicate_G3_C4")
        self.assertEqual(mdf.calls, [("duplicate", 1, 2), ("duplicate", 3, 4)])
        self.assertEqual((result_a.group, result_a.channel, result_a.unit), (1, 2, "A"))
        self.assertEqual((result_b.group, result_b.channel, result_b.unit), (3, 4, "B"))
        np.testing.assert_array_equal(result_a.samples, [1.0])
        np.testing.assert_array_equal(result_b.samples, [2.0])

    def test_missing_unique_key_has_explicit_failure(self):
        resolver = SignalResolver({"present": _info("present")})
        with self.assertRaisesRegex(SignalKeyNotFoundError, "missing") as caught:
            resolver.resolve("missing")
        self.assertEqual(caught.exception.signal_key, "missing")

    def test_text_samples_retain_legacy_mapping_and_expose_sample_kind(self):
        frame = pd.DataFrame({"state": ["off", "on", "off"]}, index=[0, 1, 2])
        result = SignalResolver(
            {"state": _info("state")}, data_source=frame, data_path="states.csv"
        ).resolve("state")
        self.assertEqual(result.sample_kind, SampleKind.TEXT)
        np.testing.assert_array_equal(result.samples, [0.0, 1.0, 0.0])
        self.assertEqual(result.signal.text_mapping, ["off", "on"])
        self.assertTrue(result.signal.is_text_signal)


if __name__ == "__main__":
    unittest.main()
