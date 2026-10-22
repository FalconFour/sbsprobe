#!/usr/bin/env python3
# AI-assisted: written by Claude Sonnet 5.5 (Anthropic) under human direction,
# and tested on real hardware by FalconFour. See "AI assistance" in README.md.
"""
sbsprobe - explore, identify and (carefully) edit laptop "smart batteries"
over SMBus from a Raspberry Pi (or anything with Linux i2c-dev + smbus2).

    sbsprobe.py detect              what kind of gauge is this? (read-mostly)
    sbsprobe.py dump LABEL          standard SBS registers -> LABEL.json
    sbsprobe.py scan LABEL          which command codes 0x00-0xFF answer a read
    sbsprobe.py ma [LABEL]          ManufacturerAccess info probe (word style)
    sbsprobe.py mac44 [LABEL]       info-only MAC commands via 0x44 (bq40z-style)
    sbsprobe.py df44 LABEL          read data flash 0x4000-0x5FFF (bq40z-style)
    sbsprobe.py dfwrite ...         write data flash (dry run unless --yes)
    sbsprobe.py diff A.json B.json  compare two dumps

READ THE README FIRST. Writing to a battery gauge can corrupt it, and gauge
flash can hold protection thresholds. This tool only ever writes (a) fixed,
info-only query commands and (b) data flash bytes you explicitly ask for.
"""
import argparse
import contextlib
import datetime
import json
import re
import sys
import time

__version__ = "0.1.0"

SBS_ADDR = 0x0B                  # 7-bit address of every smart battery (0x16 in 8-bit form)
DF_LO, DF_HI = 0x4000, 0x5FFF    # bq40z-family data flash window via 0x44
DF_CAL_END = 0x401F              # looks like calibration data on the pack we studied

# (register, name, kind)  u=unsigned word, s=signed word, k=temp in 0.1K,
# h=hex word, d=SBS date, b=block string, x=block hex
STD = [
    (0x01, "RemainingCapacityAlarm", "u"),
    (0x02, "RemainingTimeAlarm", "u"),
    (0x03, "BatteryMode", "h"),
    (0x08, "Temperature", "k"),
    (0x09, "Voltage_mV", "u"),
    (0x0A, "Current_mA", "s"),
    (0x0B, "AverageCurrent_mA", "s"),
    (0x0C, "MaxError_pct", "u"),
    (0x0D, "RelativeStateOfCharge", "u"),
    (0x0E, "AbsoluteStateOfCharge", "u"),
    (0x0F, "RemainingCapacity", "u"),
    (0x10, "FullChargeCapacity", "u"),
    (0x11, "RunTimeToEmpty", "u"),
    (0x12, "AverageTimeToEmpty", "u"),
    (0x13, "AverageTimeToFull", "u"),
    (0x14, "ChargingCurrent", "u"),
    (0x15, "ChargingVoltage", "u"),
    (0x16, "BatteryStatus", "h"),
    (0x17, "CycleCount", "u"),
    (0x18, "DesignCapacity", "u"),
    (0x19, "DesignVoltage", "u"),
    (0x1A, "SpecificationInfo", "h"),
    (0x1B, "ManufactureDate", "d"),
    (0x1C, "SerialNumber", "u"),
    (0x20, "ManufacturerName", "b"),
    (0x21, "DeviceName", "b"),
    (0x22, "DeviceChemistry", "b"),
    (0x23, "ManufacturerData", "x"),
]

# Write-allowed "ManufacturerAccess" queries. Informational on the TI-style
# parts we know of; nothing else is ever sent to register 0x00.
SAFE_MA = (0x0001, 0x0002, 0x0003)
# Info-only MAC queries sent through 0x44 (device type, FW, HW, status words,
# ManufacturerInfo). Nothing here changes state on TI bq40z-family gauges.
SAFE_MAC44 = (0x0001, 0x0002, 0x0003, 0x0051, 0x0053, 0x0054,
              0x0055, 0x0056, 0x0057, 0x0070)
SEC_NAMES = {0: "reserved(00)", 1: "FULL ACCESS(01)", 2: "UNSEALED(10)", 3: "SEALED(11)"}


# --------------------------------------------------------------------------
# bus layer
# --------------------------------------------------------------------------
def retry(fn, tries=3, delay=0.05):
    last = None
    for _ in range(tries):
        try:
            return fn()
        except OSError as e:
            last = e
            time.sleep(delay)
    raise last


class Dev:
    """Thin wrapper so the rest of the code doesn't care whether the bus is
    a real smbus2 bus or a simulator (the tests use a simulator)."""

    def __init__(self, bus, addr, msg):
        self.bus, self.addr, self.msg = bus, addr, msg

    def word(self, reg, tries=3):
        return retry(lambda: self.bus.read_word_data(self.addr, reg), tries)

    def put_word(self, reg, val):
        self.bus.write_word_data(self.addr, reg, val)

    def block(self, reg, n=33):
        """Write [reg] then read n bytes with a repeated start. Returns
        [count, data...] exactly as the gauge sent it."""
        def go():
            w, r = self.msg.write(self.addr, [reg]), self.msg.read(self.addr, n)
            self.bus.i2c_rdwr(w, r)
            return list(r)
        return retry(go)

    def write_raw(self, payload):
        self.bus.i2c_rdwr(self.msg.write(self.addr, list(payload)))

    def close(self):
        close = getattr(self.bus, "close", None)
        if close:
            close()


def open_dev(args):
    try:
        from smbus2 import SMBus, i2c_msg
    except ImportError:
        sys.exit("smbus2 not found. Try: sudo apt install python3-smbus2   (or: pip install smbus2)")
    return Dev(SMBus(args.bus), args.dev_addr, i2c_msg)


@contextlib.contextmanager
def opened(args):
    dev = open_dev(args)
    try:
        yield dev
    finally:
        dev.close()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def sbs_block(dev, reg):
    """SBS block read -> bytes (length byte stripped, max 32)."""
    blk = dev.block(reg, 33)
    return bytes(blk[1 : 1 + min(blk[0], 32)])


def decode(dev, reg, kind):
    if kind in ("u", "s", "k", "h", "d"):
        w = dev.word(reg)
        if kind == "u":
            return w
        if kind == "s":
            return w - 0x10000 if w & 0x8000 else w
        if kind == "k":
            return f"{w / 10 - 273.15:.1f} C (raw {w})"
        if kind == "h":
            return f"0x{w:04X}"
        y, m, d = ((w >> 9) & 0x7F) + 1980, (w >> 5) & 0x0F, w & 0x1F
        return f"{y:04d}-{m:02d}-{d:02d} (raw 0x{w:04X})"
    data = sbs_block(dev, reg)
    if kind == "b":
        return f"{data.decode('ascii', errors='replace')!r}  [{data.hex(' ')}]"
    return f"({len(data)} bytes) {data.hex(' ')}"


def save_json(label, data):
    fn = label if label.endswith(".json") else f"{label}.json"
    out = {"_tool": f"sbsprobe {__version__}"}
    out.update(data)
    with open(fn, "w") as f:
        json.dump(out, f, indent=2)
    return fn


def ack_map(dev):
    """Word-read every command code 0x00-0xFF; return {reg: word} for the ones
    that ACK. Skips 0x50 (a data-flash WRITE command on some old gauges)."""
    res = {}
    for reg in range(0x100):
        if reg == 0x50:
            continue
        try:
            res[reg] = dev.word(reg, tries=1)
        except OSError:
            pass
        time.sleep(0.01)
    return res


def ma_probe(dev):
    """Send ManufacturerAccess info sub-commands 1..3 and read register 0x00
    back. TI-style gauges answer with device type / FW / HW words. Some gauges
    just latch whatever was written ('latch': True)."""
    out = {"idle": dev.word(0x00)}
    for sub in SAFE_MA:
        dev.put_word(0x00, sub)
        time.sleep(0.15)
        out[sub] = dev.word(0x00)
        time.sleep(0.3)
    vals = [out[s] for s in SAFE_MA]
    out["latch"] = (all((v & 0xFF) == s for v, s in zip(vals, SAFE_MA))
                    and len({v >> 8 for v in vals}) == 1)
    return out


def mac44(dev, sub):
    """Info-only MAC query through ManufacturerBlockAccess (0x44).
    Returns the data bytes, or None if the reply doesn't echo the command."""
    lo, hi = sub & 0xFF, sub >> 8
    dev.write_raw([0x44, 2, lo, hi])
    time.sleep(0.06)
    blk = dev.block(0x44, 40)
    body = bytes(blk[1 : 1 + min(blk[0], 38)])
    return body[2:] if body[:2] == bytes([lo, hi]) else None


def sec_from_opstatus(data):
    """Decode the security mode from OperationStatus using TI's bq40z layout
    (SEC1:SEC0 = bits 9:8 of the 32-bit word). Clones may fake this."""
    v = int.from_bytes(data[:4], "little")
    return v, SEC_NAMES[(v >> 8) & 3]


def df_read(dev, addr, n):
    """Read n bytes of 0x44-family data flash. The address is re-sent for each
    32-byte block, starting from a 32-byte-aligned base."""
    base = addr & ~0x1F
    need = (addr + n) - base
    buf = bytearray()
    a = base
    while len(buf) < need:
        dev.write_raw([0x44, 2, a & 0xFF, a >> 8])
        time.sleep(0.04)
        blk = retry(lambda: dev.block(0x44, 36), 2)
        got = blk[1] | (blk[2] << 8)
        if blk[0] < 34 or got != a:
            raise OSError(f"bad DF read at 0x{a:04X} (count={blk[0]}, addr=0x{got:04X})")
        buf += bytes(blk[3:35])
        a += 32
    return bytes(buf[addr - base : addr - base + n])


# --------------------------------------------------------------------------
# detect
# --------------------------------------------------------------------------
def looks_placeholder(data):
    s = data.decode("ascii", errors="ignore")
    if len(s) >= 8 and (re.search(r"abcdefgh", s, re.I) or "01234567" in s):
        return True
    return len(data) >= 4 and len(set(data)) == 1


def detect(dev, passive=False):
    """Classify the gauge. Returns a dict (the CLI prints it, tests inspect it)."""
    r = {"family": None, "evidence": [], "smells": [], "hints": [], "next": [],
         "security": None, "ids": {}}
    ev = r["evidence"]

    try:
        mv = dev.word(0x09)
    except OSError:
        r["family"] = "none"
        ev.append("no answer at 0x%02X for Voltage() (0x09): not an SBS battery here, or wiring/power problem" % dev.addr)
        return r
    ev.append(f"SBS core responds (Voltage = {mv} mV)")

    # Identity first: on 0x44-family gauges register 0x23 returns the 0x44 result
    # buffer after any 0x44 traffic, so read it before we do any.
    mfr_data = sbs_block(dev, 0x23)
    for reg, key in ((0x20, "ManufacturerName"), (0x21, "DeviceName"), (0x22, "DeviceChemistry")):
        r["ids"][key] = sbs_block(dev, reg).decode("ascii", errors="replace")
    r["ids"]["ManufacturerData"] = mfr_data.hex(" ")

    acks = ack_map(dev)
    cells_3c = {0x3C, 0x3D, 0x3E, 0x3F} <= set(acks)
    cells_33 = {0x33, 0x34, 0x35} <= set(acks) and not cells_3c
    if cells_3c:
        ev.append("cell-voltage block answers at 0x3C-0x3F (bq20z/bq40z-style map)")
    if cells_33:
        ev.append("extra block answers at 0x30-0x35 but no 0x3C-0x3F (not the TI bq20z/bq40z map)")
    if 0x44 in acks:
        ev.append("0x44 answers a read")

    name = r["ids"]["DeviceName"]
    if name != name.strip():
        r["smells"].append(f"DeviceName has leading/trailing whitespace ({name!r})")
    if looks_placeholder(mfr_data):
        r["smells"].append("ManufacturerData looks like placeholder fill (alphabet / counting pattern)")

    if passive:
        r["family"] = "unknown (passive mode: no probes sent)"
        r["next"].append("Run without --passive for a definitive probe (sends a few info-only queries).")
        return r

    ma = ma_probe(dev)
    r["ma"] = {("idle" if k == "idle" else k if isinstance(k, str) else f"0x{k:04X}"): v
               for k, v in ma.items()}
    if ma["latch"]:
        ev.append("ManufacturerAccess (0x00) just latches the last word written: no TI-style info replies")
    else:
        ev.append("ManufacturerAccess (0x00) is not a plain latch (words after sub-commands 1/2/3: "
                  + ", ".join(f"0x{ma[s]:04X}" for s in SAFE_MA) + ")")

    mac_ok = False
    try:
        fw = mac44(dev, 0x0002)
        mac_ok = fw is not None
    except OSError:
        fw = None
    if mac_ok:
        ev.append("0x44 ManufacturerBlockAccess echoes our MAC command (bq40z/bq4050/bq8050-style)")
        try:
            op = mac44(dev, 0x0054)
            if op:
                v, sec = sec_from_opstatus(op)
                r["security"] = sec
                ev.append(f"OperationStatus via MAC = 0x{v:08X} -> security {sec} (TI bq40z layout; clones may fake)")
        except OSError:
            pass

    if mac_ok:
        r["family"] = "0x44-family"
        r["next"] += [
            "sbsprobe.py mac44 LABEL   (status words, ManufacturerInfo)",
            "sbsprobe.py df44 LABEL    (read data flash 0x4000-0x5FFF; also locates the identity strings)",
            "sbsprobe.py dfwrite ...   (dry run first; see README safety notes)",
        ]
        if r["security"] and "SEALED" in r["security"]:
            r["next"].append("Gauge reports SEALED: flash reads will likely fail until unsealed (see docs/FIELD_NOTES.md).")
    elif ma["latch"]:
        r["family"] = "ma-latch"
        r["next"] += [
            "No TI-style info commands answer, so this can't be identified over the wire.",
            "Seen on a 2006 Dell/Sanyo pack whose BMS chip is a TI bq80201 (a firmware-defined 'Cool-GG' part):",
            "behaviour is whatever the pack maker's firmware does, so there is no generic unseal / PF-clear recipe.",
            "Standard SBS reads work (dump). To know the chip, read its marking inside the pack.",
        ]
    elif cells_3c:
        r["family"] = "bq20z-candidate"
        r["next"] += [
            "Looks like a bq20z-style map but no 0x44 echo. UNTESTED territory: flash access on bq20z80/90 uses",
            "0x77/0x78/0x79, on bq2084 uses 0x50/0x51/0x52 (not implemented here; see docs/FIELD_NOTES.md).",
        ]
    else:
        r["family"] = "plain-sbs"
        r["next"].append("Only the standard SBS registers answer. dump/diff work; nothing else is known.")
    return r


def print_report(r):
    print(f"family: {r['family']}")
    print("\nevidence:")
    for e in r["evidence"]:
        print(f"  - {e}")
    print("\nidentity as reported:")
    for k, v in r["ids"].items():
        print(f"  {k:17} {v!r}")
    if r["smells"]:
        print("\nclone-smell heuristics (hints, not proof):")
        for s in r["smells"]:
            print(f"  ! {s}")
    for h in r["hints"]:
        print(f"  ? {h}")
    if r["next"]:
        print("\nnext steps:")
        for n in r["next"]:
            print(f"  > {n}")
    print("\nverified on exactly two packs so far; see README 'Tested on'. Classification is a guide, not a guarantee.")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_detect(args):
    with opened(args) as dev:
        r = detect(dev, passive=args.passive)
    print_report(r)
    if args.save:
        print("saved ->", save_json(args.save, {"detect": json.dumps(r, default=str)}))
    return 0 if r["family"] != "none" else 1


def cmd_dump(args):
    out = {}
    with opened(args) as dev:
        order = [x for x in STD if x[0] == 0x23] + [x for x in STD if x[0] != 0x23]
        for reg, name, kind in order:
            try:
                out[f"0x{reg:02X} {name}"] = decode(dev, reg, kind)
            except OSError as e:
                out[f"0x{reg:02X} {name}"] = f"<read failed: {e}>"
            time.sleep(0.03)
    for k in sorted(out):
        print(f"{k:34} {out[k]}")
    print("\nnote: (0x44-family gauges only) 0x23 returns the 0x44 result buffer after any 0x44 traffic,"
          "\n      so there it only shows the real ManufacturerData string after a quiet period.")
    print("saved ->", save_json(args.label, dict(sorted(out.items()))))
    return 0


def cmd_scan(args):
    with opened(args) as dev:
        res = ack_map(dev)
    for reg, w in res.items():
        print(f"0x{reg:02X}: 0x{w:04X}  ({w})")
    print(f"\n{len(res)} of 255 command codes answered (0x50 skipped on purpose)")
    print("note: for block registers (e.g. 0x2F, 0x44, 0x51-0x57, 0x60+) a word read returns "
          "(first_byte << 8) | block_length,\n      so 0x0004 means 'a 4-byte block starting 00', not the value 4.")
    print("saved ->", save_json(args.label, {f"SCAN 0x{r:02X}": f"0x{w:04X}" for r, w in res.items()}))
    return 0


def cmd_ma(args):
    with opened(args) as dev:
        ma = ma_probe(dev)
    print(f"MA idle read: 0x{ma['idle']:04X}")
    for s in SAFE_MA:
        print(f"MA 0x{s:04X} -> 0x{ma[s]:04X}")
    print("verdict:", "plain latch (echoes what was written; no TI-style info replies)" if ma["latch"]
          else "not a plain latch (on some chips the word read of 0x00 is not the sub-command result; "
               "see docs/FIELD_NOTES.md)")
    if args.label:
        data = {"MA idle": f"0x{ma['idle']:04X}"}
        data.update({f"MA 0x{s:04X}": f"0x{ma[s]:04X}" for s in SAFE_MA})
        print("saved ->", save_json(args.label, data))
    return 0


def cmd_mac44(args):
    out = {}
    with opened(args) as dev:
        for sub in SAFE_MAC44:
            try:
                d = retry(lambda sub=sub: mac44(dev, sub), 2)
            except OSError as e:
                print(f"MAC 0x{sub:04X}: failed ({e})")
                out[f"MAC44 0x{sub:04X}"] = f"<failed: {e}>"
                continue
            if d is None:
                print(f"MAC 0x{sub:04X}: no echo (not a 0x44-style gauge, or unsupported command)")
                out[f"MAC44 0x{sub:04X}"] = "<no echo>"
                continue
            line = f"MAC 0x{sub:04X}: {d.hex(' ')}"
            if sub == 0x0054 and len(d) >= 2:
                v, sec = sec_from_opstatus(d)
                line += f"   (OperationStatus 0x{v:08X}; TI bq40z layout says {sec})"
            if sub == 0x0070:
                line += "   ascii: " + repr(d.decode("ascii", errors="replace"))
            print(line)
            out[f"MAC44 0x{sub:04X}"] = d.hex(" ")
            time.sleep(0.1)
    if args.label:
        print("saved ->", save_json(args.label, out))
    return 0


def find_all(hay, pat):
    hits, i = [], hay.find(pat)
    while i != -1:
        hits.append(i)
        i = hay.find(pat, i + 1)
    return hits


def cmd_df44(args):
    start, end = int(args.start, 0), int(args.end, 0)
    mem, have, streak = bytearray(0x10000), set(), 0
    with opened(args) as dev:
        # Strings the gauge reports over SBS; we look for them in flash to find the fields.
        idstr = {}
        for reg, key in ((0x20, "ManufacturerName"), (0x21, "DeviceName"), (0x22, "DeviceChemistry")):
            with contextlib.suppress(OSError):
                idstr[key] = sbs_block(dev, reg)
        a = start
        while a <= end:
            try:
                dev.write_raw([0x44, 2, a & 0xFF, a >> 8])
                time.sleep(0.04)
                blk = retry(lambda: dev.block(0x44, 36), 2)
                got = blk[1] | (blk[2] << 8)
                if blk[0] < 34 or got != a:
                    raise OSError(f"bad reply (count={blk[0]}, addr=0x{got:04X})")
                mem[a : a + 32] = bytes(blk[3:35])
                have.update(range(a, a + 32))
                streak = 0
                if (a - start) % 0x400 == 0:
                    print(f"  ...0x{a:04X} ok", flush=True)
            except OSError as e:
                streak += 1
                print(f"  0x{a:04X}: {e}", flush=True)
                if streak >= 3:
                    print("3 failures in a row, giving up (sealed? not a 0x44-family gauge?)")
                    break
            a += 32
            time.sleep(0.02)
    if not have:
        print("no data flash read at all.")
        return 1
    lo, hi = min(have), max(have) + 1
    binfn = args.label if args.label.endswith(".bin") else f"{args.label}.bin"
    with open(binfn, "wb") as f:
        f.write(bytes(mem[lo:hi]))
    print(f"\nsaved -> {binfn}  (0x{lo:04X}-0x{hi - 1:04X}, {len(have)} bytes; first byte = address 0x{lo:04X})")

    raw = bytes(mem)
    print("\nwhere the gauge's reported identity strings live in flash:")
    for key, val in idstr.items():
        if not val:
            continue
        hits = find_all(raw, val)
        desc = []
        for h in hits:
            lenbyte = f", length byte 0x{raw[h - 1]:02X} at 0x{h - 1:04X}" if raw[h - 1] == len(val) else ""
            desc.append(f"0x{h:04X}{lenbyte}")
        print(f"  {key:17} {val.decode('ascii', 'replace')!r:18} -> {', '.join(desc) if desc else 'not found'}")
    if any(len(find_all(raw, v)) > 1 for v in idstr.values() if v):
        print("  note: a string found at several addresses may include buffer mirrors, not real fields "
              "(on our test pack a RAM-backed\n        buffer near 0x5F80 echoed blocks that had just been read). "
              "The copy the gauge really uses is the one that\n        changes what SBS reports after you edit it.")
    for extra in (args.grep or []):
        hits = find_all(raw, extra.encode())
        print(f"  grep {extra!r}: {', '.join(f'0x{h:04X}' for h in hits) or 'not found'}")
    runs = [(m.start(), m.group()) for m in re.finditer(rb"[ -~]{5,}", raw[lo:hi])]
    if runs:
        print("\nother printable runs (first 20):")
        for off, s in runs[:20]:
            print(f"  0x{lo + off:04X}: {s.decode()!r}")
    return 0


def cmd_dfwrite(args):
    addr = int(args.addr, 0)
    if args.pstr is not None:
        text = args.pstr.encode("ascii")
        flen = args.field_len or (len(text) + 1)
        if len(text) > flen - 1:
            sys.exit(f"string too long for a {flen}-byte field")
        data = bytes([len(text)]) + text + bytes(flen - 1 - len(text))
    elif args.hex:
        data = bytes.fromhex(args.hex.replace("0x", "").replace(",", " "))
    else:
        sys.exit("give --hex or --pstr")
    end = addr + len(data) - 1
    if not (DF_LO <= addr and end <= DF_HI):
        sys.exit(f"address range outside 0x{DF_LO:04X}-0x{DF_HI:04X}")
    if addr <= DF_CAL_END and not args.force:
        sys.exit("0x4000-0x401F looks like calibration data; refusing (use --force if you really mean it)")

    with opened(args) as dev:
        before = df_read(dev, addr, len(data))
        print(f"address 0x{addr:04X}..0x{end:04X} ({len(data)} bytes)")
        print(f"  before: {before.hex(' ')}")
        print(f"  after : {data.hex(' ')}")
        if before == data:
            print("nothing to change.")
            return 0
        if not args.yes:
            print("\ndry run only. Re-run with --yes to write.")
            return 0

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"dfwrite_backup_{stamp}_0x{addr:04X}.json"
        with open(bak, "w") as f:
            json.dump({"addr": f"0x{addr:04X}", "before_hex": before.hex(" "),
                       "restore": f"sbsprobe.py dfwrite --addr 0x{addr:04X} --hex \"{before.hex(' ')}\" --yes"},
                      f, indent=2)
        print(f"\nbackup of the old bytes -> {bak}")
        print(f"undo with: sbsprobe.py dfwrite --addr 0x{addr:04X} --hex \"{before.hex(' ')}\" --yes")

        for off in range(0, len(data), 16):
            chunk = data[off : off + 16]
            a = addr + off
            dev.write_raw([0x44, 2 + len(chunk), a & 0xFF, a >> 8] + list(chunk))
            time.sleep(0.2)
        time.sleep(0.2)
        got = df_read(dev, addr, len(data))
        ok = got == data
        print(f"\nverify read-back: {got.hex(' ')}  -> {'OK' if ok else 'MISMATCH!'}")
        for reg, name in ((0x20, "ManufacturerName"), (0x21, "DeviceName"), (0x22, "DeviceChemistry")):
            with contextlib.suppress(OSError):
                print(f"  SBS 0x{reg:02X} {name:17} now reads: {decode(dev, reg, 'b')}")
        print("\n(SBS 0x23 is not read on purpose: after DF reads it returns the 0x44 buffer. Use `mac44`, MAC 0x0070.)")
        print("If 0x20/0x21 still show old values, the gauge may only reload flash into RAM after a reset.")
        return 0 if ok else 2


def cmd_diff(args):
    with open(args.a) as fa, open(args.b) as fb:
        a, b = json.load(fa), json.load(fb)
    noisy = ("Voltage", "Current", "Temperature", "StateOfCharge", "RemainingCapacity",
             "RunTime", "AverageTime", "SerialNumber", "MaxError")
    keys = sorted(k for k in set(a) | set(b) if not k.startswith("_"))
    n = 0
    for k in keys:
        va, vb = a.get(k, "<missing>"), b.get(k, "<missing>")
        if va == vb:
            continue
        n += 1
        tag = "  (expected to differ)" if any(x in k for x in noisy) else "  <-- LOOK"
        print(f"{k:34} {str(va):>28} | {vb}{tag}")
    print(f"\n{n} differing entries")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="sbsprobe", description="Explore, identify and edit smart batteries over SMBus.")
    p.add_argument("--version", action="version", version=f"sbsprobe {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-b", "--bus", type=int, default=1, help="i2c bus number (default 1)")
    common.add_argument("--dev-addr", dest="dev_addr", type=lambda s: int(s, 0), default=SBS_ADDR,
                        help="7-bit SMBus address of the battery (default 0x0B)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("detect", parents=[common], help="classify the gauge")
    s.add_argument("--passive", action="store_true", help="reads only; send no probe commands")
    s.add_argument("--save", metavar="LABEL")
    s.set_defaults(fn=cmd_detect)

    s = sub.add_parser("dump", parents=[common], help="standard SBS registers to LABEL.json")
    s.add_argument("label")
    s.set_defaults(fn=cmd_dump)

    s = sub.add_parser("scan", parents=[common], help="which command codes answer a word read")
    s.add_argument("label")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("ma", parents=[common], help="ManufacturerAccess info probe (writes sub-commands 1-3 to 0x00)")
    s.add_argument("label", nargs="?")
    s.set_defaults(fn=cmd_ma)

    s = sub.add_parser("mac44", parents=[common], help="info-only MAC queries via 0x44")
    s.add_argument("label", nargs="?")
    s.set_defaults(fn=cmd_mac44)

    s = sub.add_parser("df44", parents=[common], help="read data flash via 0x44 (bq40z-style), locate identity strings")
    s.add_argument("label")
    s.add_argument("--start", default=hex(DF_LO))
    s.add_argument("--end", default=hex(DF_HI))
    s.add_argument("--grep", action="append", metavar="TEXT", help="also search the dump for TEXT (repeatable)")
    s.set_defaults(fn=cmd_df44)

    s = sub.add_parser("dfwrite", parents=[common], help="write data flash (DRY RUN unless --yes)")
    s.add_argument("--addr", required=True, help="data flash address to write at, e.g. 0x4085 (NOT the SMBus address)")
    s.add_argument("--hex", help="bytes to write, e.g. \"05 53 41 4e 59 4f\"")
    s.add_argument("--pstr", help="length-prefixed ASCII string; zero-padded to --field-len")
    s.add_argument("--field-len", type=int, help="total field size in bytes including the length byte")
    s.add_argument("--yes", action="store_true", help="actually write (default is a dry run)")
    s.add_argument("--force", action="store_true", help="allow the apparent calibration bytes at 0x4000-0x401F")
    s.set_defaults(fn=cmd_dfwrite)

    s = sub.add_parser("diff", help="compare two JSON dumps")
    s.add_argument("a")
    s.add_argument("b")
    s.set_defaults(fn=cmd_diff)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
