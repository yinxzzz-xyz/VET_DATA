import io
import unittest

import canmatrix
import cantools
from canmatrix.formats import dbc

from vet_data_modular.baseline import _prepare_dbc_signal_names


def _dbc_text(database):
    output = io.BytesIO()
    dbc.dump(database, output, dbcExportEncoding="ascii")
    return output.getvalue().decode("ascii")


class ArxmlDbcSignalNameExportTests(unittest.TestCase):
    def _database(self):
        database = canmatrix.CanMatrix()
        classic = canmatrix.Frame(name="ClassicFrame", arbitration_id=0x123, size=8)
        # Different source names truncate to the same 32-character DBC name.
        prefix = "IRZCU_ZONE_5ms_Group01_Reserved"
        classic.add_signal(canmatrix.Signal(prefix + "1", start_bit=0, size=3, is_little_endian=False))
        classic.add_signal(canmatrix.Signal(prefix + "10", start_bit=16, size=8, is_little_endian=False))
        classic.add_signal(canmatrix.Signal("IntelValue", start_bit=32, size=8, is_little_endian=True))
        database.add_frame(classic)

        fd = canmatrix.Frame(name="FdFrame", arbitration_id=0x456, size=24, is_fd=True)
        fd.add_signal(canmatrix.Signal("FdMotorola11", start_bit=37, size=11, is_little_endian=False))
        fd.add_signal(canmatrix.Signal("FdIntel16", start_bit=80, size=16, is_little_endian=True))
        database.add_frame(fd)
        return database

    def test_truncated_name_collision_roundtrips_without_layout_changes(self):
        database = self._database()
        expected = {
            (frame.name, signal.name): (
                signal.size,
                signal.is_little_endian,
                signal.get_startbit(bit_numbering=1),
            )
            for frame in database.frames
            for signal in frame.signals
        }
        _prepare_dbc_signal_names({"test": database})

        text = _dbc_text(database)
        loaded = cantools.database.load_string(text, database_format="dbc")
        self.assertEqual(len(loaded.messages), 2)
        for message in loaded.messages:
            self.assertEqual(message.is_extended_frame, False)
            for signal in message.signals:
                size, little_endian, start = expected[(message.name, signal.name)]
                self.assertEqual(signal.length, size)
                self.assertEqual(signal.byte_order, "little_endian" if little_endian else "big_endian")
                self.assertEqual(signal.start, start)

        classic = loaded.get_message_by_name("ClassicFrame")
        self.assertEqual(classic.length, 8)
        self.assertEqual(classic.get_signal_by_name("IRZCU_ZONE_5ms_Group01_Reserved1").length, 3)
        self.assertEqual(classic.get_signal_by_name("IRZCU_ZONE_5ms_Group01_Reserved10").length, 8)
        self.assertEqual(classic.get_signal_by_name("IntelValue").byte_order, "little_endian")

        fd = loaded.get_message_by_name("FdFrame")
        self.assertEqual(fd.length, 24)
        self.assertEqual(fd.get_signal_by_name("FdMotorola11").length, 11)
        self.assertEqual(fd.get_signal_by_name("FdIntel16").length, 16)


if __name__ == "__main__":
    unittest.main()
