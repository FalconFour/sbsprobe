# Field notes

What we learned getting an aftermarket battery accepted by a 2006 Dell, and
what that says about identifying smart-battery gauges. Everything here is from
two packs and one laptop. Each claim is tagged:

- **[observed]** we saw it on real hardware
- **[inferred]** it fits what we saw, but we didn't prove it
- **[documented]** from a TI document, forum thread or someone else's writeup
  (linked at the end)

## 1. The case: Dell Inspiron E1705 + an aftermarket pack

**Symptom [observed].** The pack fit, powered the laptop (81% charge), but the
BIOS said it couldn't identify the battery, and the battery LED flashed
orange/green while refusing to charge. The original pack (2006, dead cells)
made the laptop say "permanent failure" instead. That difference mattered: the
Dell accepted the *original's identity data*, so the problem with the
aftermarket pack was how it introduced itself, not its cells.

**Diff of what each pack reports over SBS [observed]:**

| Register | Original (accepted) | Aftermarket (rejected) |
|---|---|---|
| `0x21` DeviceName | `DELL F513361I` | `" DELL00"` (leading space) |
| `0x23` ManufacturerData | 5 bytes: `42016` | 32 bytes: `abcdefghijklmnopqrstuvwzxy012345` (placeholder fill) |
| `0x1A` SpecificationInfo | `0x0021` | `0x0031` |
| `0x03` BatteryMode | `0x4000` | `0x6000` |
| `0x18` DesignCapacity | 4800 | 4400 |
| `0x15`/`0x14` ChargingVoltage/Current | 0 (permanent failure) | 12600 / 2200 (sane) |

**The fix [observed].** We copied the original's `DeviceName` and
`ManufacturerData` into the aftermarket pack's data flash. After that the BIOS
showed "This battery is performing normally" and charging, and Windows XP
charged it. **We changed both strings together, so we don't know which one the
BIOS actually needed.** `SpecificationInfo`, `BatteryMode` and design capacity
were left different and it still worked, so those don't seem to matter to this
BIOS.

**What the OS shows [observed].**
- The manufacturer string is not validated: `SANYO` displayed as "Sanyo"
  (normalized), but `CHINESIUM` was displayed exactly as written and the pack
  still worked.
- Windows' battery name was cut to `DELL F51336` (11 characters) from the
  13-character `DELL F513361I`. Display truncation, not a flash problem.

## 2. Telling gauge families apart

Probes and what they showed:

| Probe | Pack A (aftermarket) | Pack B (original) |
|---|---|---|
| Standard SBS `0x01-0x23` | all answer | all answer |
| Cell voltages | `0x3C-0x3F` (3938 / 3937 / 3936 mV; `0x3C` = 0 on a 3S pack) | `0x30-0x35` block; `0x33-0x35` look like 3302 / 3282 / 2732 mV but didn't change when the supply voltage changed, so we don't trust them |
| `0x4F` | 100 (looks like state of health) | not present |
| `0x51-0x57` word reads | answer; these are block registers, so the word is `(first byte << 8) \| length` (`0x0004` = a 4-byte block starting `00`; see section 3) | answer (`0x010A`, `0x0308`, `0x0007`, `0x010A`); we can't tell if they're blocks too |
| Write sub-command 1/2/3 to `0x00`, read `0x00` | `0xFFA5`, `0xFFA5`, `0x0107` | `0xB901`, `0xB902`, `0xB903` (**echo with a constant `0xB9` top byte: a latch**) |
| `0x44` echoes a MAC command | **yes** | no (NACK) |
| `0x2F` | `0x0014` = a **20-byte block** (a 160-bit buffer; the SmartBatteryHack wiki lists `0x2F` as ManufacturerInput, used for SHA-1 authentication) | not present |

**Pack A [observed]:** behaves like a TI bq40z50/bq4050/bq8050-style gauge
(label on the board: "BQ4050"). **Pack B [observed]:** chip marking
**bq80201DBT** (a Dell D5318 pack, per the owner).

**About the bq80201 [inferred].** We couldn't find a standalone bq80201 data
sheet. Datasheet aggregators file `BQ80201DBT` under TI's **bq8015 "Cool-GG"
programmable battery management IC**, a part whose firmware lives in flash
(the bq8015's default state is flash erased to all 1s) [documented, bq8015
datasheet]. If that's right, a bq80201 pack's behavior is defined by the pack
maker's firmware, which would explain why it ignores TI-style ManufacturerAccess
info commands and why there's no generic unseal / permanent-failure recipe for
it. We haven't tested any of that beyond the register behavior above.

## 3. The 0x44 family (bq40z50 / bq4050 / bq8050-style)

**ManufacturerBlockAccess (`0x44`) [documented/observed].**
- MAC query: block-write `[0x44, 0x02, cmd_lo, cmd_hi]`, then block-read `0x44`.
  The reply is `[count, cmd_lo, cmd_hi, data...]`. Echoing our command bytes is
  how `detect` recognizes the family.
- Data flash read: block-write the 16-bit address (little-endian) to `0x44`,
  then block-read: you get `[34, addr_lo, addr_hi, 32 data bytes]`. Flash is
  `0x4000-0x5FFF`.
- Data flash write: block-write `[0x44, 2+n, addr_lo, addr_hi, data...]`. No
  checksum. Needs full access. Any permanent-failure flag blocks flash writes.
  TI warns about `0x4000` (calibration area).

**Register `0x23` is a trap [observed].** Before any `0x44` traffic it returned
the manufacturer-data string. After `0x44` reads it returned *the next 32 bytes
of flash each time we read it* (auto-increment), then zeros. TI's forum says
a MAC result can be read from either `ManufacturerData()` or
`ManufacturerBlockAccess()`. So don't trust `0x23` after `0x44` traffic; read
MAC `0x0070` instead. `sbsprobe dump` reads `0x23` first for this reason.

**Word-reading a block register [observed, Pack A].** Several "extended"
registers are *block* registers. A word read of one returns the block's
length byte in the low byte and its first data byte in the high byte. We
checked this against the MAC replies for the same data:

| Register | Word read | = `(byte0 << 8) \| length` | MAC reply |
|---|---|---|---|
| `0x54` OperationStatus | `0x0704` | byte0 `07`, 4 bytes | `07 01 00 00` |
| `0x55` ChargingStatus | `0x0803` | byte0 `08`, 3 bytes | `08 04 00` |
| `0x56` GaugingStatus | `0x4002` | byte0 `40`, 2 bytes | `40 00` |
| `0x57` ManufacturingStatus | `0x1002` | byte0 `10`, 2 bytes | `10 02` |
| `0x51` / `0x53` Safety / PFStatus | `0x0004` | byte0 `00`, 4 bytes | `00 00 00 00` |
| `0x2F` | `0x0014` | byte0 `00`, **20 bytes** | (not queried) |
| `0x70` ManufacturerInfo | `0x3405` now, `0x6120` before our edit | byte0 `34` ("4"), 5 bytes; before: byte0 `61` ("a"), 32 bytes | MAC `0x0070`: `34 32 30 31 36` |

Use the `0x44` MAC path (or a proper block read) to get the whole value. An
earlier version of these notes called the direct values "constants" and
"disagreeing with the MAC path"; that was a misreading, and `detect` no longer
raises a hint for it.

**Reading `0x00` [observed, inferred].** On Pack A, reading ManufacturerAccess
after writing sub-command 3 returned `0x0107` / `0x0187`, which equals the low 16
bits of OperationStatus at that moment, and the idle value (`0x8107`) looks like
it too. Sub-commands 1 and 2 returned `0xFFA5`. So the word read of `0x00` on
this chip doesn't return the MAC result you asked for. A later scan read `0x00`
as `0x8187`, the same word the `0x23` buffer showed at the time (`87 81 00 00`),
which fits.

**`0x23` buffer, again [observed].** Right after `detect` (which used `0x44`),
`dump` showed `0x23` = `87 81 00 00`, which is OperationStatus: the result
of the last MAC query, re-evaluated at read time (bit 15 had flipped since
`detect` read it). After a quiet period `0x23` returned `34 32 30 31 36`
(`42016`), and MAC `0x0070` (ManufacturerInfo) returns the same five bytes,
so the string we put in flash is what the gauge hands out at idle.

**RAM-backed buffer in the flash window [observed, inferred].** The
pre-edit dump has `07 01 00 00` + zeros at `0x5F80-0x5FBF` (the same status
word that also sat at `0x40C0`). After our edits, `df44` found `DELL F513361I`
and `LION` a second time at `0x5F85` / `0x5F9A`, an exact copy of the `0x4080`
block. That region looks like a buffer mapped into the window that mirrors
the blocks most recently served, not real flash. Our edits at `0x4085` changed
what SBS reports, so `0x4085` is the real field.

**Security mode [inferred].** TI's forum describes full access for the
bq40z50 as SEC1 clear / SEC0 set, in bits 9:8 of OperationStatus. MAC
`0x0054` = `0x107` has bit 8 set and bit 9 clear, which says "full access", and
flash was indeed readable and writable without sending any keys. The older
SmartBatteryHack wiki examples (`FFFF` sealed, `C001` unsealed, `8001` full
access) use a different layout; we didn't see those values. **A clone can fake
all of these bits.**

**Byte order [documented].** SMBus words go low byte first. `i2cset ... w` and
smbus2's `write_word_data` do that, so you pass a key word as printed in the
docs. The SmartBatteryHack wiki prints keys MSB-first, which is where "reverse
the bytes" advice comes from. Unsealing keys aren't implemented in `sbsprobe`
on purpose; see the wiki.

**Capacity numbers on the clone [observed].** We opened Pack A afterwards: six
cells labelled 2000 mAh (so 3S2P, about 4000 mAh nominal), date-coded 2/2026.
Compare that with the listing's 5200 mAh and the gauge's 4400 mAh (both
DesignCapacity and FullChargeCapacity, with MaxError pinned at 100% and a
cycle count of 1). So the gauge's capacity figures don't come from the cells
(about 10% above the cell labels), and the listing's figure is about 30% above
what the labels support. Cheap-cell labels can themselves be optimistic.
The charge *percentage* is a separate question. It read 81-82% when the pack
was new, and 66% later, after a long full charge and about an hour of use. That
is plausible, so we have no evidence either way about whether it tracks
reality. (An earlier version of these notes read the 82% to 66% drop as a sign
that the percentage was untrustworthy. The pack had been charged to full and
used in between, so that conclusion was unfounded.) Only a real discharge test
settles it.

## 4. Data flash layout of Pack A [observed unless noted]

Found by looking for the strings the gauge reports over SBS (`df44` does this):

| Address | Content |
|---|---|
| `0x4000-0x4015` | random-looking bytes; looks like calibration data [inferred] (the tool refuses writes here) |
| `0x4040` | ManufacturerData: length byte `0x20` (32) + 32 chars. The "space" before `abcdef...` is the length byte |
| `0x406C` | ManufactureDate word (`0x5D16` = 2026-08-22) |
| `0x406E` | SerialNumber word (`0x2178` = 8568) |
| `0x4070` | ManufacturerName: length `0x05` + `SANYO` |
| `0x4085` | DeviceName: length `0x07` + `" DELL00"` |
| `0x409A` | DeviceChemistry: length `0x04` + `LION` |
| `0x4443` | RemainingCapacityAlarm (440) |
| `0x4447` | RemainingTimeAlarm (10) |
| `0x444B` | SpecificationInfo (`0x0031`) |
| `0x444D` | DesignCapacity (4400) |
| `0x444F` | `0x1314` = 4884, matches 4400 mAh x 11.1 V as a design energy [inferred] |
| `0x4451` | DesignVoltage (11100) |

The identity fields tile at **21-byte spacing** (`0x4070 + 21 = 0x4085`,
`0x4085 + 21 = 0x409A`), so each looks like a length byte plus 20 characters.
The `0x4443-0x4451` entries were found by value-matching, not from a TRM table,
and `0x4445`/`0x4449` are unlabeled guesses. **This map is for that one pack;
your pack's layout may differ. Run `df44` and look.**

A byte past a string's length is invisible over SBS (the gauge returns only
`length` bytes). That would make the unused tail of a field a place to leave a
signature. **Tested:** we wrote `CLAUDE+FF` at `0x407B`, in the free tail of the
ManufacturerName field (the byte at `0x407A` left zero as a gap), and SBS still
reported plain `CHINESIUM`; `df44 --grep CLAUDE` finds it at `0x407B`.

## 5. Flash access by family

| Family | How flash is accessed | Status here |
|---|---|---|
| bq40z50 / bq4050 / bq8050-style | `0x44`, address `0x4000-0x5FFF` | **implemented, verified on one pack** |
| bq20z80 / bq20z90 | write a subclass ID to `0x77`, then 32-byte blocks via `0x78` and `0x79`; the bq20z80 data sheet puts Manufacturer Name / Device Name / Chemistry in subclass 48 at offsets 24 / 36 / 44 | **not implemented** (our attempt got write errors on a 0x44-family pack, and we had no genuine part) |
| bq2084 | data flash write / address / read commands `0x50` / `0x51` / `0x52`; status at `0x2F` | **not implemented** (same reason). **Don't read or write `0x50-0x52` on an unidentified chip** |
| Clone gauges (e.g. A2168) | vendor-specific; a CP2112-based open-source flasher exists | not implemented. Moderators on laptopu.ro report many clones ship in full access or with default keys, and that "unsealed" doesn't always mean you can write everything |
| bq80201 (Cool-GG) | firmware-defined | unknown |

## 6. Mistakes we made on the way (so you don't)

- We read `0x2F` as "PackStatus" and decoded it as sealed/PF flags. On the
  0x44 family it's a 20-byte block (ManufacturerInput), and `0x0014` was its
  length byte plus a zero.
- We treated word reads of block registers (`0x51-0x57`, `0x2F`, `0x44`, `0x60+`) as
  values, called them constants/junk, and even added a "direct read disagrees with
  MAC" hint to `detect`. They were `[length, first byte]` all along.
- We decoded `0x54` with three different layouts across the investigation.
  Watching a value *change* after an action is more trustworthy than decoding it.
- We assumed `0x2F`/`0x50-0x54` existed with TI-style meanings on every gauge.
  They're family-specific, and `0x50` is a flash *write* command on some parts,
  so `scan` skips it.
- We believed the "supply voltage" showed up in the original's `0x33-0x35`
  registers. It didn't (identical at 9.3 V and 7.5 V).
- `0x23` contents changing between reads looked like data corruption. It's the
  block buffer (section 3).

## 7. Open questions

- Which of the two edits (DeviceName, ManufacturerData) did the Dell BIOS need?
- What do genuine bq4050 packs return for the device-type bytes? Ours returned
  `34 9e` for MAC `0x0001` while the first two bytes of the firmware-version
  reply (MAC `0x0002`) were `9e 34`, the same bytes reversed. We don't know
  whether that's a clone tell or normal.
- What is the `0x30-0x35` block on the original, and how does the bq80201's
  permanent-failure latch work and clear?

## Sources

- SmartBatteryHack and its wiki: https://github.com/laszlodaniel/SmartBatteryHack
- TI bq4050 technical reference (SLUUAQ3): https://ti.com/document-viewer/lit/html/sluuaq3
- TI E2E, bq4050 writing data flash: https://e2e.ti.com/support/power-management-group/power-management/f/power-management-forum/743900/bq4050-writing-data-flash-memory
- TI E2E, bq40z50 unsealed to full access: https://e2e.ti.com/support/power-management-group/power-management/f/power-management-forum/336788/bq40z50-unsealed-to-full-access
- TI E2E, bq40z50 data flash recovery / PF blocks writes: https://e2e.ti.com/support/power-management-group/power-management/f/power-management-forum/999345/bq40z50-smbus-block-write-protocol-to-recover-data-flash-0x4000-default-value
- TI bq20z90 technical reference (SLUU264): https://www.ti.com/lit/pdf/sluu264
- NI forum, bq20z90 data flash via `0x77`/`0x78`/`0x79`: https://forums.ni.com/t5/LabVIEW/I2C-Data-Flash/m-p/1746316
- TI bq8015 Cool-GG datasheet (SLUS522A): https://www.mouser.com/ds/2/405/slus522a-128456.pdf
- A2168 clone gauge thread: https://www.laptopu.ro/community/laptop-battery-chip-reset-and-repair/lenovo-thinkpad-e550-copy-battery-with-a2168-chip-shut-down-randomly/
- A2168 flasher: https://github.com/lmdpua/A2168_Laptop_Battery_Flasher_on_CP2112
- Karosium, SMBusb: https://www.karosium.com/2016/08/smbusb-hacking-smart-batteries.html
