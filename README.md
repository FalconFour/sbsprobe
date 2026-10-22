# sbsprobe

**Talk to a laptop battery. Find out what's inside. Fix its identity crisis.**

`sbsprobe` is a single-file Python tool that talks to a laptop "smart battery"
over SMBus from a Raspberry Pi. It can dump the standard registers, work out
which *family* of gauge chip is inside (without opening the pack), read the
gauge's data flash on TI bq40z/bq4050/bq8050-style parts, and carefully edit
the strings the laptop checks.

It exists because of one afternoon with a Dell Inspiron E1705: an aftermarket
battery fit, discharged, and reported 81%, but the laptop said *"unable to
identify the battery"* and refused to charge it. Reading the pack over SMBus,
comparing it with the dead original, and fixing two strings in the gauge's
flash made the BIOS say *"This battery is performing normally"*. The full story
and everything we learned is in [`docs/FIELD_NOTES.md`](docs/FIELD_NOTES.md).

**If you landed here because...**
- a Dell laptop says it can't identify the battery (or won't charge a
  replacement / aftermarket battery) and the battery LED flashes orange and
  green. We saw exactly that on an Inspiron E1705; other models word it
  differently.
- you want to see what an aftermarket pack's gauge claims, and whether it's a
  clone.
- you want to read or edit smart-battery data flash from a Raspberry Pi
  (bq4050 / bq40z50 / bq8050-style gauges).

> **Status: 0.1, verified on exactly two packs** (see [Tested on](#tested-on)).
> Detection is a guide, not a guarantee. Writing to a gauge can corrupt it.
> Read [Safety](#safety) before you use `dfwrite`.

## What it can do

| Command | What it does | Writes to the pack? |
|---|---|---|
| `detect` | Classifies the gauge, lists evidence, flags "clone smells", suggests next steps | Only fixed info-only queries (none with `--passive`) |
| `dump LABEL` | Standard SBS registers (voltage, capacity, strings, ...) to `LABEL.json` | No |
| `scan LABEL` | Which command codes `0x00-0xFF` answer a read (a register-map fingerprint) | No |
| `ma [LABEL]` | ManufacturerAccess info probe, "word" style (sub-commands 1-3 to `0x00`) | Info-only |
| `mac44 [LABEL]` | Info-only MAC queries through `0x44`: device/FW/HW versions, status words, ManufacturerInfo | Info-only |
| `df44 LABEL` | Reads data flash `0x4000-0x5FFF` to `LABEL.bin` and **locates the identity strings in it** | Sets the read-address pointer only |
| `dfwrite ...` | Writes data flash bytes. **Dry run unless `--yes`.** Backs up, verifies, prints the undo command | **Yes** |
| `diff A.json B.json` | Compares two dumps and flags the interesting differences | No |

Run `./sbsprobe.py <command> --help` for options. Global options:
`-b/--bus N` (I2C bus, default 1) and `--dev-addr` (default `0x0B`, which every
smart battery uses).

## Install

Needs Python 3.8+ and [`smbus2`](https://pypi.org/project/smbus2/). On
Raspberry Pi OS (Bullseye or newer):

```bash
sudo apt install python3-smbus2 i2c-tools     # both ship with the full desktop image
sudo raspi-config                              # Interface Options -> I2C -> enable
./sbsprobe.py --version       # or: python3 sbsprobe.py --version  (chmod +x first if needed)
```

No other dependencies. It's one file; copy it anywhere.

## Wiring

You need three wires: **SDA, SCL and ground.** The gauge powers itself from
its cells, so don't power it from the Pi. SMBus here is 3.3 V logic.

| Pi pin | Signal |
|---|---|
| 3 (GPIO2) | SDA |
| 5 (GPIO3) | SCL |
| any GND | battery negative |

- The Pi's I2C1 pins have **1.8 kΩ pull-ups on the board**, which is enough. Add
  your own (4.7-10 kΩ to 3.3 V) only if you use a bit-banged `i2c-gpio` bus on
  other pins, and don't stack both.
- Many packs want a "system present" line pulled low. On the Dell Inspiron
  6400/E1505/E1705 family, the laptop-side connector (per the service schematic)
  is: pins 1-2 BATT+, 3 SMB_CLK, 4 SMB_DAT, 5 BATT_PRES#, 6 SYSPRES#,
  7 BATT_VOLT, 8-9 BATT-. We grounded SYSPRES#. **The pack-side connector is a
  mirror image and other models differ, so confirm every contact with a meter
  before connecting anything.** BATT+ is right next to the data pins.
- Pack-side view, for the same family: connector facing you, label side up.
  The outer pairs (ground on the left, positive on the right) are what we
  found on our pack; the middle order follows from the schematic's pin order
  and mirror symmetry, so check it with a meter before trusting it:

  ```
   left                                                          right
  +-----+-----+------+----------+--------+-----+-----+-----+-----+
  | GND | GND | VOLT | SYSPRES# | PRES#  | SDA | SCL |  +  |  +  |
  +-----+-----+------+----------+--------+-----+-----+-----+-----+
    9     8     7      6          5        4     3     2     1     <- schematic pin
  ```

  Wire SDA, SCL, GND, and tie SYSPRES# to ground. SmartBatteryHack suggests
  doing that through 100-1000 ohms. Leave `+` alone unless you mean to power a dead
  pack (see below). BATT_PRES# and BATT_VOLT weren't needed.
- Check you can see it: `i2cdetect -y 1` should show `0b`.
- **Dead pack?** The gauge needs power to talk. We woke a deeply discharged
  pack with a current-limited bench supply (7.5-9.3 V, ~0.2 W) on a BATT+ pin
  for a few seconds at a time. Never push current into cells that are far below
  ~2 V per cell; that's a fire risk. If you can, disconnect the cells first.

## Quick start

```bash
./sbsprobe.py detect                 # what is this thing?
./sbsprobe.py dump mypack            # save the standard registers
./sbsprobe.py mac44 mypack_mac       # 0x44-family: status words, security mode
./sbsprobe.py df44 mypack_df         # 0x44-family: read flash, find the name fields

# change the device name (dry run first!)
./sbsprobe.py dfwrite --addr 0x4085 --pstr "DELL F513361I" --field-len 21
./sbsprobe.py dfwrite --addr 0x4085 --pstr "DELL F513361I" --field-len 21 --yes
```

`df44` prints where the strings the gauge reports over SBS live in flash, for
example (from our test pack):

```
  ManufacturerName  'SANYO'   -> 0x4071, length byte 0x05 at 0x4070
  DeviceName        ' DELL00' -> 0x4086, length byte 0x07 at 0x4085
  DeviceChemistry   'LION'    -> 0x409B, length byte 0x04 at 0x409A
```

Those fields are length-prefixed and sit 21 bytes apart, so `--field-len 21` fits
them. **Work out the field size for your own pack from the dump; don't assume
21.**

### What `detect` prints

Sample output (replayed from our captures by the simulator; these are the
real outputs from the two packs we tested):

```
family: 0x44-family

evidence:
  - SBS core responds (Voltage = 11811 mV)
  - cell-voltage block answers at 0x3C-0x3F (bq20z/bq40z-style map)
  - 0x44 answers a read
  - ManufacturerAccess (0x00) is not a plain latch (words after sub-commands 1/2/3: 0xFFA5, 0xFFA5, 0x0107)
  - 0x44 ManufacturerBlockAccess echoes our MAC command (bq40z/bq4050/bq8050-style)
  - OperationStatus via MAC = 0x00000107 -> security FULL ACCESS(01) (TI bq40z layout; clones may fake)

clone-smell heuristics (hints, not proof):
  ! DeviceName has leading/trailing whitespace (' DELL00')
  ! ManufacturerData looks like placeholder fill (alphabet / counting pattern)
```

```
family: ma-latch

evidence:
  - SBS core responds (Voltage = 1038 mV)
  - extra block answers at 0x30-0x35 but no 0x3C-0x3F (not the TI bq20z/bq40z map)
  - ManufacturerAccess (0x00) just latches the last word written: no TI-style info replies
```

## How `detect` decides

| Cue | Meaning |
|---|---|
| `0x44` echoes the command bytes we send (MAC `0x0002`) | **0x44-family**: bq40z50 / bq4050 / bq8050-style, or a clone of one. Flash is at `0x4000-0x5FFF` via `0x44` |
| ManufacturerAccess (`0x00`) returns what we wrote, with a constant top byte | **ma-latch**: no TI-style info replies. Seen on a pack with a TI bq80201 |
| Cell voltages at `0x3C-0x3F` but no `0x44` echo | **bq20z-candidate** (untested here): likely older bq20z80/90-style |
| Only the standard registers answer | **plain-sbs** |
| `0x20`-`0x23` strings with leading spaces, alphabet fill, counting patterns | clone smell (aftermarket firmware that nobody finished) |

These are our observations from two packs plus what TI documents for the
families, not a complete fingerprint database. If `detect` gets your pack
wrong, please open an issue with the output of `scan` and `detect`.

## Safety

- **Li-ion packs can burn.** Don't charge cells that are deeply discharged,
  don't short anything, and don't leave a bench supply feeding a pack
  unattended.
- **`dfwrite` can break a gauge.** On real TI parts the data flash also holds
  protection thresholds (over-voltage, over-current, temperature), calibration
  data, and the permanent-failure configuration. This tool is aimed at the
  identity strings. It refuses `0x4000-0x401F` (looks like calibration data on
  the pack we studied) unless you pass `--force`. **Don't edit protection
  settings.**
- `dfwrite` is a dry run unless you pass `--yes`. When it writes, it saves the
  old bytes to `dfwrite_backup_*.json`, reads back to verify, and prints the
  exact undo command. Keep a full `df44` dump as well.
- Writes only happen if the gauge allows them. On bq40z-style parts that means
  full-access mode and no permanent-failure flag. Our test pack was already in
  full access (some aftermarket packs ship that way).
- The only things `sbsprobe` ever sends besides reads are the info-only queries
  in `SAFE_MA` and `SAFE_MAC44` (see the source) and your explicit `dfwrite`
  bytes. The test suite checks this with a simulator that raises on any other
  write.
- This is for repairing and studying batteries you own. Making a laptop accept
  a pack does not make the pack safe, and a cloned gauge's capacity and health
  numbers can be fiction.

## Troubleshooting

- `[Errno 5] Input/output error` is a NACK: wiring, no power to the gauge, or
  the gauge doesn't support that command. A fast failure usually means
  "unsupported"; a slow one means a stuck bus.
- Reads fine but flash reads fail: the gauge may be **sealed**. `detect` and
  `mac44` print the security mode where they can. Unsealing keys are not
  implemented in this tool on purpose; see `docs/FIELD_NOTES.md` for what's
  publicly documented.
- `scan` shows odd little words like `0x0004`: block registers read as words return `(first byte << 8) | block length`, so that's "a 4-byte block starting 00".
- Register `0x23` returns different data every time you read it: on 0x44-family
  gauges it's a result buffer after any `0x44` traffic. Use `mac44` (MAC
  `0x0070`, ManufacturerInfo) to see the real string.
- `df44` finds a string at two addresses: one may be a RAM-backed buffer mirror, not a real field. The real one is whichever changes what SBS reports when you edit it.
- Flaky reads on a Pi: try the bit-banged bus
  (`dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24` in
  `/boot/firmware/config.txt`, add your own pull-ups, use `-b 3`).

## Tested on

| | |
|---|---|
| Host | Raspberry Pi 3 B+, Raspberry Pi OS (full desktop image: `python3-smbus2` and `i2c-tools` preinstalled) |
| Pack A | Aftermarket "Dell Inspiron E1505/1501/6400" 3S pack, listed 5200 mAh, gauge reports 4400 mAh, cells are 6 x 2000 mAh-labelled (3S2P, about 4000 mAh), date code 2/2026. Board labelled "BQ4050". 0x44-family; already in full access. `detect`, `dump`, `mac44`, `df44` and `dfwrite` (backup, verify and undo output) all run on the real pack; Dell E1705 BIOS accepted it after the string edits |
| Pack B | Original 2006 Dell/Sanyo pack (a Dell D5318, per the owner), 3S, design 4800 mAh. Chip marking **bq80201DBT**. `ma-latch` behaviour; read-only so far |
| Laptop | Dell Inspiron E1705 (BIOS shows MP061; Windows XP) |

The simulator tests replay real captures from those two packs (`tests/fixtures`):

```bash
python3 -m unittest discover -s tests -v      # no hardware needed
```

## Not implemented (ideas and pointers)

- Flash access for **bq20z80/bq20z90**-style gauges (subclass select via `0x77`,
  32-byte blocks via `0x78`/`0x79`) and **bq2084** (`0x50`/`0x51`/`0x52`). We
  wrote exploratory versions, they did NACK on our 0x44-family pack, and we
  never had a genuine part to verify them, so they're out. See the field notes.
- Unsealing / permanent-failure clearing. Publicly documented default keys exist
  for TI parts; we deliberately don't ship an auto-unsealer.
- Clone-gauge families such as the A2168 (see
  [lmdpua/A2168_Laptop_Battery_Flasher_on_CP2112](https://github.com/lmdpua/A2168_Laptop_Battery_Flasher_on_CP2112)).
- Firmware-defined TI "Cool-GG" parts like the bq80201.

PRs with captures from other packs (`scan` + `detect` + `dump` output) are the
most useful contribution.

## Credits and related work

- [laszlodaniel/SmartBatteryHack](https://github.com/laszlodaniel/SmartBatteryHack)
  (Arduino-based SMBus battery hacking tool, with a wiki that was our map of the
  `0x44`-family security keys and flash access).
- TI's bq4050 technical reference manual (SLUUAQ3), the bq20z90 TRM (SLUU264),
  and several TI E2E threads on `ManufacturerBlockAccess()` / data flash access.
- [Charlie Miller, *Battery Firmware Hacking* (Black Hat 2011)](https://media.blackhat.com/bh-us-11/Miller/BH_US_11_Miller_Battery_Firmware_Public_WP.pdf),
  worth reading for how seriously battery firmware can go wrong.
- [NickLD/PackProbe](https://github.com/NickLD/PackProbe): Arduino code that reads
  smart-battery data over SMBus/I2C (read-only; a similar name, a different tool).
- [emersonknapp/smart_battery_driver](https://github.com/emersonknapp/smart_battery_driver):
  a Linux driver that publishes a smart battery's state to ROS, via a CP2112 adapter.
- [wesleygas/dell-battery-emulator](https://github.com/wesleygas/dell-battery-emulator):
  the inverse problem (booting a Dell with no battery at all).

## License

TBD (MIT suggested).
