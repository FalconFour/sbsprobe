"""
Simulator-based tests: no hardware needed.  Run from the repo root with

    python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import sbsprobe          # noqa: E402
import simbus            # noqa: E402


def run(argv, bus):
    """Run the CLI against a simulated bus in a scratch directory."""
    dev = sbsprobe.Dev(bus, 0x0B, simbus.Msg)
    out = io.StringIO()
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with mock.patch.object(sbsprobe, "open_dev", lambda args: dev), \
                    mock.patch.object(sbsprobe.time, "sleep", lambda s: None), \
                    contextlib.redirect_stdout(out):
                try:
                    code = sbsprobe.main(argv)
                except SystemExit as e:
                    code = e.code
            files = {}
            for f in os.listdir(tmp):
                with open(f, "rb") as fh:
                    files[f] = fh.read()
        finally:
            os.chdir(cwd)
    return code, out.getvalue(), files


class NoSleep(unittest.TestCase):
    """Real hardware needs the delays; the simulator doesn't."""

    def setUp(self):
        p = mock.patch.object(sbsprobe.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)


class DetectTests(NoSleep):
    def test_new_pack_is_0x44_family(self):
        bus = simbus.new_pack()
        r = sbsprobe.detect(sbsprobe.Dev(bus, 0x0B, simbus.Msg))
        self.assertEqual(r["family"], "0x44-family")
        self.assertEqual(r["security"], "FULL ACCESS(01)")
        self.assertTrue(any("placeholder" in s for s in r["smells"]))
        self.assertTrue(any("whitespace" in s for s in r["smells"]))
        self.assertEqual(r["hints"], [])      # word reads of block registers are not a "disagreement"

    def test_original_is_ma_latch(self):
        r = sbsprobe.detect(sbsprobe.Dev(simbus.original(), 0x0B, simbus.Msg))
        self.assertEqual(r["family"], "ma-latch")
        self.assertIsNone(r["security"])
        self.assertEqual(r["smells"], [])          # its strings look legitimate
        self.assertEqual(r["ids"]["DeviceName"], "DELL F513361I")

    def test_passive_sends_no_writes(self):
        bus = simbus.new_pack()
        r = sbsprobe.detect(sbsprobe.Dev(bus, 0x0B, simbus.Msg), passive=True)
        self.assertTrue(r["family"].startswith("unknown"))
        self.assertEqual(bus.writes, [])

    def test_no_device(self):
        bus = simbus.FakeBus({}, {0x20: b"", 0x21: b"", 0x22: b"", 0x23: b""})
        r = sbsprobe.detect(sbsprobe.Dev(bus, 0x0B, simbus.Msg))
        self.assertEqual(r["family"], "none")


class SafetyTests(unittest.TestCase):
    """The simulator raises AssertionError on any non-whitelisted write."""

    def test_read_and_probe_commands_only_make_whitelisted_writes(self):
        for make in (simbus.new_pack, simbus.original):
            for argv in (["detect"], ["dump", "x"], ["scan", "x"], ["ma"], ["mac44"]):
                bus = make()
                code, out, _ = run(argv, bus)
                self.assertIn(code, (0, 1), f"{make.__name__} {argv}: {out}")

    def test_df44_only_sets_address_pointer(self):
        bus = simbus.new_pack()
        code, out, files = run(["df44", "flash"], bus)
        self.assertEqual(code, 0)
        self.assertTrue(all(w[0] == "raw" and w[1][:2] == [0x44, 2] for w in bus.writes))
        self.assertEqual(len(files["flash.bin"]), 0x2000)

    def test_dump_reads_0x23_before_anything_else(self):
        bus = simbus.new_pack()
        run(["dump", "x"], bus)
        self.assertEqual(bus.block_reads[0], 0x23)


class FlashTests(unittest.TestCase):
    def test_df44_locates_identity_strings(self):
        code, out, _ = run(["df44", "flash"], simbus.new_pack())
        self.assertIn("'SANYO'", out)
        self.assertIn("0x4071, length byte 0x05 at 0x4070", out)
        self.assertIn("0x4086, length byte 0x07 at 0x4085", out)

    def test_dfwrite_dry_run_changes_nothing(self):
        bus = simbus.new_pack(allow_df_write=True)
        before = bytes(bus.df)
        code, out, files = run(["dfwrite", "--addr", "0x4085", "--pstr", "DELL F513361I", "--field-len", "21"], bus)
        self.assertEqual(code, 0)
        self.assertIn("dry run only", out)
        self.assertEqual(bytes(bus.df), before)
        self.assertEqual(files, {})

    def test_dfwrite_writes_verifies_backs_up_and_undoes(self):
        bus = simbus.new_pack(allow_df_write=True)
        original_df = bytes(bus.df)
        code, out, files = run(["dfwrite", "--addr", "0x4085", "--pstr", "DELL F513361I",
                                "--field-len", "21", "--yes"], bus)
        self.assertEqual(code, 0, out)
        self.assertIn("-> OK", out)
        self.assertEqual(bytes(bus.df[0x85:0x85 + 14]), b"\x0dDELL F513361I")
        bak = [f for f in files if f.startswith("dfwrite_backup_")]
        self.assertEqual(len(bak), 1)
        info = json.loads(files[bak[0]])
        self.assertEqual(info["addr"], "0x4085")
        # undo using the printed hex
        code, out, _ = run(["dfwrite", "--addr", "0x4085", "--hex", info["before_hex"], "--yes"], bus)
        self.assertEqual(code, 0, out)
        self.assertEqual(bytes(bus.df), original_df)

    def test_calibration_guard(self):
        bus = simbus.new_pack(allow_df_write=True)
        code, out, _ = run(["dfwrite", "--addr", "0x4000", "--hex", "00 00", "--yes"], bus)
        self.assertNotEqual(code, 0)
        code, out, _ = run(["dfwrite", "--addr", "0x6000", "--hex", "00", "--yes"], bus)
        self.assertNotEqual(code, 0)

    def test_string_too_long_rejected(self):
        bus = simbus.new_pack(allow_df_write=True)
        code, _, _ = run(["dfwrite", "--addr", "0x4085", "--pstr", "X" * 30, "--field-len", "21"], bus)
        self.assertNotEqual(code, 0)


class DiffTests(unittest.TestCase):
    def test_diff_flags_identity_fields(self):
        fx = os.path.join(simbus.HERE)
        code, out, _ = run(["diff", os.path.join(fx, "new_pack_1.json"), os.path.join(fx, "original.json")], simbus.new_pack())
        self.assertIn("0x21 DeviceName", out)
        self.assertIn("<-- LOOK", out)


if __name__ == "__main__":
    unittest.main()
