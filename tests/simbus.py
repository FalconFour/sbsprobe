"""
A small simulated smart battery, replayed from real captures in tests/fixtures.
It is *strict*: any write the tool isn't supposed to make raises AssertionError,
which is how the tests check the "only whitelisted writes" claim.

Two devices are provided:
  new_pack()  - the aftermarket 0x44-family pack (flash readable, full access)
  original()  - the 2006 original pack (ManufacturerAccess just latches)
"""
import json
import os
import re

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load(name):
    with open(os.path.join(HERE, name)) as f:
        return json.load(f)


def _scan_words(name):
    return {int(k.split()[1], 16): int(v, 16) for k, v in _load(name).items() if k.startswith("SCAN")}


def _hex_after_marker(text):
    """'...  [53 41 4e]' or '(32 bytes) 61 62 ..' -> bytes"""
    m = re.search(r"\[([0-9a-f ]+)\]", text) or re.search(r"bytes\)\s*([0-9a-f ]+)$", text)
    return bytes.fromhex(m.group(1))


def _strings(name):
    d = _load(name)
    out = {}
    for k, reg in (("0x20", 0x20), ("0x21", 0x21), ("0x22", 0x22), ("0x23", 0x23)):
        key = next(x for x in d if x.startswith(k))
        out[reg] = _hex_after_marker(d[key])
    return out


class Msg:
    def __init__(self, kind, addr, data):
        self.kind, self.addr, self.data = kind, addr, data

    @staticmethod
    def write(addr, data):
        return Msg("w", addr, list(data))

    @staticmethod
    def read(addr, n):
        return Msg("r", addr, [0] * n)

    def __iter__(self):
        return iter(self.data)


class FakeBus:
    def __init__(self, scan, strings, ma_replies=None, latch_hi=None, mac_replies=None,
                 df=None, allow_df_write=False, has_0x44=True):
        self.scan, self.strings = scan, strings
        self.ma_replies, self.latch_hi = ma_replies or {}, latch_hi
        self.mac_replies = mac_replies or {}
        self.df = bytearray(df) if df is not None else None
        self.allow_df_write, self.has_0x44 = allow_df_write, has_0x44
        self.ma_state = None            # last sub-command written to 0x00
        self.pend = None                # last address/command written via 0x44
        self.writes = []                # every write, for assertions
        self.block_reads = []           # order of block reads (register numbers)

    # -- word protocol -------------------------------------------------
    def read_word_data(self, addr, reg):
        if reg == 0x00:
            if self.latch_hi is not None:
                return (self.latch_hi << 8) | (self.ma_state or 0x06)
            return self.ma_replies[self.ma_state]
        if reg in self.scan:
            return self.scan[reg]
        raise OSError(5, "Input/output error")

    def write_word_data(self, addr, reg, val):
        self.writes.append(("word", reg, val))
        assert reg == 0x00 and val in (1, 2, 3), f"unexpected word write reg=0x{reg:02X} val=0x{val:04X}"
        self.ma_state = val

    # -- i2c_rdwr: raw write, or write+read ------------------------------
    def i2c_rdwr(self, *msgs):
        w = msgs[0]
        if len(msgs) == 1:
            self.writes.append(("raw", list(w.data)))
            if not self.has_0x44:
                raise OSError(5, "Input/output error")
            assert w.data[0] == 0x44, f"unexpected raw write to 0x{w.data[0]:02X}"
            cnt = w.data[1]
            lo_hi = w.data[2] | (w.data[3] << 8)
            if cnt == 2:
                self.pend = lo_hi
                return
            assert self.allow_df_write and self.df is not None, "unexpected data-flash write"
            payload = w.data[4:4 + cnt - 2]
            off = lo_hi - 0x4000
            assert 0 <= off and off + len(payload) <= len(self.df)
            self.df[off:off + len(payload)] = bytes(payload)
            return

        r = msgs[1]
        reg = w.data[0]
        self.block_reads.append(reg)
        if reg in (0x20, 0x21, 0x22, 0x23):
            data = self.strings[reg]
            out = [len(data)] + list(data)
        elif reg == 0x44 and self.has_0x44:
            if self.pend is not None and self.pend >= 0x4000 and self.df is not None:
                o = self.pend - 0x4000
                out = [34, self.pend & 0xFF, self.pend >> 8] + list(self.df[o:o + 32])
            elif self.pend in self.mac_replies:
                body = [self.pend & 0xFF, self.pend >> 8] + list(self.mac_replies[self.pend])
                out = [len(body)] + body
            else:
                out = [0]                      # unknown MAC: no echo
        else:
            raise OSError(5, "Input/output error")
        r.data[:] = (out + [0] * len(r.data))[:len(r.data)]

    def close(self):
        pass


def new_pack(allow_df_write=False):
    mac = {int(k.split()[1], 16): bytes.fromhex(v)
           for k, v in _load("new_mac44.json").items()}
    m = _load("new_pack_mac.json")
    ma = {None: int(m["MA idle"], 16), 1: int(m["MA 0x0001"], 16),
          2: int(m["MA 0x0002"], 16), 3: int(m["MA 0x0003"], 16)}
    with open(os.path.join(HERE, "new_df.bin"), "rb") as f:
        df = f.read()
    return FakeBus(_scan_words("new_pack_scan.json"), _strings("new_pack_1.json"),
                   ma_replies=ma, mac_replies=mac, df=df, allow_df_write=allow_df_write)


def original():
    return FakeBus(_scan_words("original_scan.json"), _strings("original.json"),
                   latch_hi=0xB9, has_0x44=False)
