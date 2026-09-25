# SCTE-35 Segment Numbering — Cross-Standard Reference

**Purpose:** specify the differing rules for `segment_num`, `segments_expected`,
`sub_segment_num` and `sub_segments_expected` across three profiles, so a
numbering engine can compute correct values for any of them from a single
input model (a stream of Break / Placement Opportunity / Ad Block /
Advertisement-or-Promo events).

**Standards covered:**

| Mode key | Standard | Source |
|---|---|---|
| `SCTE35_2019A` | ANSI/SCTE 35 2019a | §10.3.3.5 (Segmenting Content), §10.3.3.11 (Placement Opportunities) |
| `SCTE35_2023R1` | ANSI/SCTE 35 2023r1 | §10.3.3.7 (Segmenting Content), §10.3.3.13 (Placement Opportunities), §10.3.3.14 (Advertisements/Promos/Ad Blocks) |
| `AF2M_SNPTV` | *Service de TV Segmentée* v2.0.7 (af2m/SNPTV, 2020) | §3.2 "Récapitulatif" table |

All three use the same four bitstream fields; **the meaning assigned to each
field is what differs.** This is the core problem the engine must model — not
missing fields, but reassigned semantics.

---

## 1. Field glossary (common to all three)

| Field | Size | Definition (base, SCTE 35 generic) |
|---|---|---|
| `segment_num` | 8 bits | Position of this segment within some collection. |
| `segments_expected` | 8 bits | Count of segments expected in that collection. |
| `sub_segment_num` | 8 bits | Position of this sub-segment within some sub-collection. |
| `sub_segments_expected` | 8 bits | Count of sub-segments expected in that sub-collection. |

The three standards disagree on **what "collection" and "sub-collection"
mean** for Placement Opportunities and for Advertisements/Promos. That
disagreement is the whole of this document.

---

## 2. Per-standard rules

### 2.1 SCTE 35 2019a

**Placement Opportunity Start/End** (§10.3.3.11) — outer counter numbers
**Breaks within a Program**:

- Program segmentation present, Break numbering *not* supported → `segment_num=0`, `segments_expected=0` on every PO Start in the Program.
- Program segmentation present, Break numbering supported → `segment_num` = index of this Break (1-based), `segments_expected` = total Breaks in the Program.
- Program segmentation absent, Break numbering not supported → `0`/`0`.
- Program segmentation absent, Break numbering supported (Provider-defined interval) → `segment_num`/`segments_expected` count Breaks in the Provider-defined interval; `segment_num` resets to 1 to mark a new interval.
- All PO Starts *within the same Break* carry identical `segment_num`/`segments_expected`.

`sub_segment_num`/`sub_segments_expected` on a PO Start: **optional**, numbers multiple *nested/sibling Placement Opportunities within one Break* (e.g. a PPO plus the DPOs it contains) — 1-based, reset per Break. **Restriction: "`sub_segment_num` and `sub_segments_expected` shall only be specified on Placement Opportunity Start segments"** (§10.3.3.5) — not valid on any other segmentation type.

**Advertisement (Provider/Distributor Advertisement Start/End)** (§10.3.3.5) — no dedicated numbering clause exists (there is no §10.3.3.14 equivalent in this version). The generic clause states:

> "`segment_num` ... may be optionally utilized for Advertisements in the same manner as Chapters." (i.e. 1-based position within a collection, reset at the start of the collection)
> "`segments_expected` ... shall be set to a non-zero value on Chapter Start, Provider Advertisement Start, or Distributor Advertisement Start, providing the number of starts..."

No standard scope is defined for what the "collection" is (Break vs Program) — implementers decide. `sub_segment_num` **must not** appear on Advertisement descriptors (restricted to PO Start only, above).

**Ad Block:** does not exist as a segmentation type in this version.

### 2.2 SCTE 35 2023r1

Introduces a strict, uniform two-tier model applied identically to **every** advertising segmentation type (PO, Ad Block, Advertisement, Promo):

- **Outer tier — `segment_num`/`segments_expected`: always Break-within-Program numbering.** Rules are structurally identical for POs (§10.3.3.13.2) and for Advertisement/Promo/Ad Block (§10.3.3.14.2): four cases based on (Program segmentation present/absent) × (Break numbering supported/not), same `0`/`0` default when unsupported, same 1-based Break index otherwise. All Starts *in the same Break*, of *any* advertising type, carry identical `segment_num`/`segments_expected`.

- **Inner tier — `sub_segment_num`/`sub_segments_expected`: always position-within-the-Break**, but tracked as **three independent counters**, one per level, each its own numbering scope:
  - **Placement Opportunities** (§10.3.3.13.3): sequential across all PO Starts (Provider or Distributor) in the Break, 1-based, `sub_segments_expected` = total PO Starts expected in the Break. (Worked example — Table 26 — PPO=1, DPO=2, PPO=3, all `sub_segments_expected=3`.)
  - **Ad Blocks** (§10.3.3.14.3): different semantics — `sub_segment_num` on an Ad Block Start = **the relative position of the first underlying advertisement or promo inside that Ad Block**, not a simple sequential Ad Block index.
  - **Advertisements and Promos** (§10.3.3.14.4): sequential across all Advertisement + Promo Starts (Provider or Distributor, type-blind) in the Break, 1-based, `sub_segments_expected` = total expected in the Break.

- `sub_segment_num` is **no longer restricted to PO Start** — the 2019a restriction is lifted; it's now valid (with distinct meaning) on PO, Ad Block, and Advertisement/Promo Starts alike.

- Hierarchy is explicit and normative (§10.3.3.7): **Break (highest) → Placement Opportunity → Ad Block → {Advertisement, Promo}** (lowest, and Advertisement/Promo "shall not overlap" each other). An Advertisement/Promo/Ad Block "shall not contain nested" messages of its own type (§10.3.3.14).

### 2.3 af2m/SNPTV (Service de TV Segmentée v2.0.7)

Built on SCTE 35 2019a (explicitly cited as the base standard) and only defines **Break Start/End, Provider Advertisement Start/End, Provider Placement Opportunity Start/End, and a private "Appel_Ad_Server" descriptor** — no Distributor types, no Ad Block, no Program/Chapter hierarchy, no cross-Program Break numbering. Fixed reference table (§3.2 "Récapitulatif"):

| Segment | `segment_num` | `segments_expected` |
|---|---|---|
| Break Start/End | **1** | **1** |
| Advertisement Start/End — opening jingle | 0 | n (spot count, excl. jingles) |
| Advertisement Start/End — spot *i* of *n* | **i** | **n** |
| Advertisement Start/End — closing jingle | 0 | 0 |
| PPO Start/End | **1** | **1** |
| Appel_Ad_Server | 0 | 0 |

`sub_segment_num`/`sub_segments_expected`: mentioned only as an optional field on PPO Start (per "the 2016 revision"), explicitly left unset/omitted for interoperability with older encoders. Never used on Advertisement descriptors.

**This exercises the 2019a option exactly as written**: `segment_num` numbers the Advertisement's position within its Break's ad collection (1-based, reset per Break, jingles excluded and set to 0), matching "may be optionally utilized for Advertisements in the same manner as Chapters." The one place af2m *deviates from the letter of 2019a* (and of 2023r1, which is unchanged here): both standards specify PPO `segment_num`/`segments_expected` = `0`/`0` when Break-numbering-within-Program is not supported; af2m instead fixes it at `1`/`1`, i.e. it applies the generic "position 1 of a 1-item collection" reading rather than the Break-numbering clause's unsupported-case default.

---

## 3. The core semantic collision (why "just read segment_num" breaks)

| | 2019a | 2023r1 | af2m |
|---|---|---|---|
| `segment_num` on **Advertisement** Start means | position of ad within its Break (optional) | which Break within the Program | position of ad within its Break (mandatory, jingle-aware) |
| `sub_segment_num` valid on Advertisement? | **No** (PO-only) | **Yes** — position of ad within Break | **No** (never used) |
| `sub_segment_num` valid on PO Start? | Yes — numbers sibling POs in a Break | Yes — numbers sibling POs in a Break (same rule) | Optional, unused in practice |
| PPO `segment_num`/`segments_expected` when Break numbering unsupported | `0`/`0` | `0`/`0` | `1`/`1` (fixed) |
| Ad Block as a numbering level | doesn't exist | exists, own `sub_segment_num` rule (§10.3.3.14.3) | doesn't exist |

A parser that assumes 2023r1 semantics reading a 2019a/af2m stream will misread every `segment_num` on an Advertisement descriptor as "which Break" when it's actually "which spot." A generator building for af2m must **not** emit `sub_segment_num` on Advertisement Starts at all — a 2023r1-literal generator would.

---

## 4. Implementation notes for a numbering engine

### 4.1 Required engine input model

The engine needs the segment hierarchy and Break-numbering context as input, independent of output mode:

```
Program
 └─ Break[]                         (ordered, 1..N per Program if numbered)
     ├─ PlacementOpportunity[]      (ordered; each Provider or Distributor; may nest)
     │   └─ AdBlock[]?              (2023r1 only; ordered)
     │       └─ AdOrPromo[]         (Provider/Distributor Advertisement or Promo, ordered)
     └─ AdOrPromo[]                 (direct children of Break, if not grouped under a PO/AdBlock)
```

Also required as engine-level config, independent of hierarchy:
- `mode`: `SCTE35_2019A | SCTE35_2023R1 | AF2M_SNPTV`
- `program_segmentation_present`: bool (2019a/2023r1 only — selects which of the 4 Break-numbering cases applies)
- `break_numbering_supported`: bool (2019a/2023r1 only)
- jingle flags on Advertisement entries (af2m only — controls the 0/n vs i/n vs 0/0 special-casing)

### 4.2 Decision logic, per mode

**`AF2M_SNPTV`**
```
Break.segment_num = 1;  Break.segments_expected = 1
PPO.segment_num = 1;    PPO.segments_expected = 1
for each Advertisement in Break, in order, excluding jingles:
    n = count of non-jingle Advertisements in this Break
    if is_opening_jingle:      segment_num = 0; segments_expected = n
    elif is_closing_jingle:    segment_num = 0; segments_expected = 0
    else:                      segment_num = i (1-based index among spots); segments_expected = n
never emit sub_segment_num / sub_segments_expected on Advertisement
sub_segment_num / sub_segments_expected on PPO: omit (recommended) or 0/0
```

**`SCTE35_2019A`**
```
# Break-level (applies to PO Starts; Advertisement Break-scoping is an implementer choice, not mandated)
if program_segmentation_present and not break_numbering_supported:  PO.segment_num/segments_expected = 0/0
if program_segmentation_present and break_numbering_supported:      PO.segment_num = break_index; PO.segments_expected = total_breaks_in_program
if not program_segmentation_present and not break_numbering_supported: PO.segment_num/segments_expected = 0/0
if not program_segmentation_present and break_numbering_supported:  PO.segment_num = index_in_interval; PO.segments_expected = interval_break_count

# sub_segment_num on PO Start only — numbers sibling POs within one Break
PO.sub_segment_num = 1-based index among POs in this Break (or omit if unnumbered)
PO.sub_segments_expected = count of POs in this Break (or omit)
# never emit sub_segment_num on any other type

# Advertisement — optional "Chapter-style" numbering; recommend Break-scoped to match af2m/real-world usage
if numbering_enabled:
    Advertisement.segment_num = 1-based index within its Break's ad collection
    Advertisement.segments_expected = count of ads in that Break
else:
    Advertisement.segment_num/segments_expected = 0/0 or omit
```

**`SCTE35_2023R1`**
```
# Outer tier — identical rule for PO, Ad Block, Advertisement/Promo Starts
if program_segmentation_present and not break_numbering_supported:  X.segment_num/segments_expected = 0/0
if program_segmentation_present and break_numbering_supported:      X.segment_num = break_index; X.segments_expected = total_breaks_in_program
if not program_segmentation_present and not break_numbering_supported: X.segment_num/segments_expected = 0/0
if not program_segmentation_present and break_numbering_supported:  X.segment_num = index_in_interval; X.segments_expected = interval_break_count
# All Starts in the same Break, of ANY advertising type, must carry identical segment_num/segments_expected.

# Inner tier — three independent counters, each reset per Break:
PO.sub_segment_num          = 1-based index among all PO Starts (Provider+Distributor) in this Break
PO.sub_segments_expected    = count of PO Starts expected in this Break

AdBlock.sub_segment_num     = position of the FIRST underlying ad/promo inside this Ad Block
                               (not a sequential Ad Block index — compute from the ad/promo counter below)
AdBlock.sub_segments_expected = count of Ad Blocks expected in this Break

AdOrPromo.sub_segment_num       = 1-based index among all Advertisement+Promo Starts (type-blind) in this Break
AdOrPromo.sub_segments_expected = count of Advertisement+Promo Starts expected in this Break
```

### 4.3 Validation the engine should assert per mode

- `AF2M_SNPTV`: reject any attempt to set `sub_segment_num` on an Advertisement descriptor.
- `SCTE35_2019A`: reject `sub_segment_num`/`sub_segments_expected` on anything other than PO Start.
- `SCTE35_2023R1`: reject a Distributor/Provider Advertisement or Promo nested inside another Advertisement/Promo/Ad Block of its own type (§10.3.3.14: "shall not contain nested ... segmentation messages" of the same kind); reject Advertisement/Promo overlap (§10.3.3.7: "shall not overlap").
- All modes: Start/End pairs must share `segmentation_event_id`; reject a mismatched pair.

---

## 5. Worked reference values (for regression tests)

**af2m, one Break with opening jingle + 2 spots + closing jingle:**

| Descriptor | segment_num | segments_expected |
|---|---|---|
| Break Start | 1 | 1 |
| Ad Start (jingle) | 0 | 2 |
| Ad Start (spot 1) | 1 | 2 |
| Ad Start (spot 2) | 2 | 2 |
| Ad Start (closing jingle) | 0 | 0 |
| PPO Start | 1 | 1 |
| PPO End | 1 | 1 |
| Break End | 1 | 1 |

**SCTE 35 2023r1, one Break (2nd of 3 in Program) containing a PPO with 2 nested DPOs and 2 ads under the PPO directly (Table 26 style, adapted):**

| Descriptor | segment_num | segments_expected | sub_segment_num | sub_segments_expected |
|---|---|---|---|---|
| Break Start | 2 | 3 | — | — |
| PPO Start | 2 | 3 | 1 | 3 |
| DPO Start #1 | 2 | 3 | 2 | 3 |
| DPO Start #2 | 2 | 3 | 3 | 3 |
| Ad Start #1 | 2 | 3 | 1 | 2 |
| Ad Start #2 | 2 | 3 | 2 | 2 |

(Note the two independent `sub_segment_num` tracks — POs count 1–3, ads count 1–2 — running concurrently inside the same Break, per §4.2 above.)

---

## 6. Source references

- ANSI/SCTE 35 2019a — §10.3.3.1 (field definitions), §10.3.3.5 (Segmenting Content), §10.3.3.11 (Placement Opportunities)
- ANSI/SCTE 35 2023r1 — §10.3.3.7 (Segmenting Content — hierarchy statement), §10.3.3.13 (Placement Opportunities), §10.3.3.14 (Advertisements, Promos and Ad Blocks)
- af2m/SNPTV, *Service de TV Segmentée*, v2.0.7 (31/07/2020) — §3.2 "Contenu détaillé des Descripteurs de segment", especially the "Récapitulatifs pour les champs segment num et segments expected" table
