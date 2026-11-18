# DESIGN.md — Desk Hedging monitor (ui, P3.M1)

Design system for the monitoring UI. Product context: `PRODUCT.md`. Implementation of every value here: `src/styles/tokens.css` (the token names below are the CSS custom property names; never hard-code a hex or a pixel value in a component).

## Principles

1. **Dark-first desktop terminal.** Dark is the default theme; light uses the same token names. Desktop only, designed for 1280 px and up (a design floor, not a CSS `min-width`: see Spacing), no mobile layouts.
2. **Dense tables.** Monospace tabular numerals, row height 24 px (22 px compact), spacing on a 4 px grid.
3. **Colour is reserved for limit and health state.** Four hues (OK, WARN, HARD, halted), nowhere else. Surfaces, text, bars, sparklines, order status, and sign of a number are neutral.
4. **No state is colour alone.** Every state has a glyph, a text label, and a border/fill shape that survive colour-blind viewing and greyscale (see State matrix).
5. **Flat.** No shadow, no gradient, no glow, no icon library, no decorative shape, no animation on value change. Separation is a one-step tone change or a 1 px line.
6. **System fonts.** No webfont and no dependency; the desk may be offline.
7. **Footnotes are legends.** A visible footnote says only what a glyph, bar or column means. NATS subject patterns, ADR citations and wire field names live in `title` tooltips (panel title, footnote, cell), never as running text.

## Surfaces, text, borders

One ramp, five surface steps. A panel sits on `--bg`, one step lighter than the canvas; headers and hovers step up.

| Token             | Dark      | Light     | Use                                         |
| ----------------- | --------- | --------- | ------------------------------------------- |
| `--bg`            | `#0c0e11` | `#eceff2` | Canvas (body)                               |
| `--surface-1`     | `#12151a` | `#f8f9fb` | Panels, cards                               |
| `--surface-2`     | `#181c22` | `#eef0f3` | Table header, disconnected banner           |
| `--surface-3`     | `#1f242b` | `#e2e6ea` | Row hover, bar track                        |
| `--surface-4`     | `#272d35` | `#d5dae0` | Selected row                                |
| `--text`          | `#e4e8ec` | `#14181d` | Values, labels                              |
| `--text-2`        | `#a6aeb8` | `#434c57` | Column headers, units, sparklines, bar fill |
| `--text-3`        | `#8a94a0` | `#55606c` | Stale, waiting, disconnected, placeholders  |
| `--border-subtle` | `#232830` | `#dde1e6` | Row dividers                                |
| `--border`        | `#2e353e` | `#c9cfd6` | Panel edges                                 |
| `--border-strong` | `#5b6672` | `#7a8490` | Controls, dashed/dotted state outlines      |

Measured contrast (WCAG ratio, `--text-3` only on `--bg`, `--surface-1`, `--surface-2`; never on 3 or 4):

| Pair                               | Dark | Light |
| ---------------------------------- | ---- | ----- |
| `--text` on `--surface-1`          | 14.9 | 16.9  |
| `--text-2` on `--surface-1`        | 8.2  | 8.3   |
| `--text-3` on `--surface-1`        | 5.9  | 6.1   |
| `--border-strong` on `--surface-1` | 3.1  | 3.3   |

## State palette

Only these carry hue. Each has `-fg` (text, glyph, bar fill), `-bg` (tint behind a chip), `-border`. HARD also has `--state-hard-bg-solid` + `--state-hard-on-fill` for a solid chip.

| State  | Dark fg   | Light fg  | Dark tint | Light tint | fg on `--surface-1` (dark / light) | fg on its tint (dark / light) |
| ------ | --------- | --------- | --------- | ---------- | ---------------------------------- | ----------------------------- |
| OK     | `#5fb88a` | `#1a7348` | `#14261d` | `#dff1e7`  | 7.6 / 5.6                          | 6.6 / 5.0                     |
| WARN   | `#e3a93b` | `#855800` | `#2a2214` | `#fbf0d9`  | 8.7 / 5.9                          | 7.5 / 5.5                     |
| HARD   | `#f4645f` | `#b8231f` | `#321a1a` | `#fbe3e2`  | 6.0 / 6.0                          | 5.3 / 5.2                     |
| halted | `#6fa8ee` | `#1a58a8` | `#15233a` | `#e0ebf9`  | 7.4 / 6.6                          | 6.4 / 5.8                     |

Solid HARD chip: `--state-hard-on-fill` on `--state-hard-bg-solid` is 6.3 (dark) and 6.4 (light).

Stale, waiting and disconnected have **no hue**. They are the absence of live data, drawn in `--text-3` with a different outline. Hue never appears on them, so a dimmed number can never be mistaken for a healthy or a breached one.

## State matrix

Seven states. Each is recognisable with colour removed by glyph + label + outline style (+ fill for HARD). Glyphs are text characters (`content: var(--glyph-*)`), marked `aria-hidden`; the label is real text, so assistive tech reads "WARN", not a shape.

| State             | Glyph | Label        | Outline (token)                     | Fill                                                           | Colour                    | Value text                          |
| ----------------- | ----- | ------------ | ----------------------------------- | -------------------------------------------------------------- | ------------------------- | ----------------------------------- |
| **OK**            | `●`   | OK           | none                                | none                                                           | `--state-ok-fg`           | `--text`                            |
| **WARN**          | `▲`   | WARN         | solid 1 px                          | `--state-warn-bg`                                              | `--state-warn-fg`         | `--state-warn-fg`                   |
| **HARD** (breach) | `✕`   | HARD         | solid 1 px                          | **solid** `--state-hard-bg-solid`, text `--state-hard-on-fill` | `--state-hard-fg`         | `--state-hard-fg`, weight 600       |
| **halted**        | `■`   | HALTED       | **double** 3 px                     | `--state-halted-bg`                                            | `--state-halted-fg`       | `--text-3` (last value kept)        |
| **stale**         | `◔`   | STALE 47s    | **dashed** 1 px                     | none                                                           | `--state-stale-fg`        | `--text-3`, italic, last value kept |
| **waiting**       | `○`   | WAITING      | **dotted** 1 px                     | none                                                           | `--state-waiting-fg`      | `–`, no number at all               |
| **disconnected**  | `⊘`   | DISCONNECTED | solid 1 px, banner on `--surface-2` | none                                                           | `--state-disconnected-fg` | board-wide, see below               |

Greyscale reading order, from the most to the least alarming shape: solid block (HARD), filled outline (WARN), double outline (halted), dashed (stale), dotted (waiting), bare glyph (OK). The label word is always present as the final fallback.

Health states reuse the same cues. A health fault (`ok=false`, `last_error`, `fatal`) uses the HARD cue with the label **FAULT**; a limit breach uses the same cue with the label **HARD**.

### Rules per state

- **OK.** Quietest thing on the screen. Glyph and label only, no outline, no tint. Most cells on a healthy board are OK; they must not shout.
- **WARN.** Applies to a limit at or above warn and below hard, and also to a WARN forced by invalid or stale data (ADR-014 s3). The chip tooltip and the alert ticker show the alert `text` verbatim (rendered as text, never HTML) so the reason is readable.
- **HARD.** At or above the hard limit. Solid chip, bold value, bar fills in `--state-hard-fg` with the `»` overflow mark if beyond the limit. Never pulses, flashes or plays a sound in P3.M1.
- **halted.** An EXO book's publication is halted (`exo.alerts.recon` with `publishing_halted`, or `exo.health` `book.<id>.state` = `halted:recon` or `halted:no-snapshot`, ADR-014 s12). Delta One has no halted field (`d1.health` carries none, ADR-014 Amendment 1 s2), so HALTED applies only to EXO books, never to a Delta One component. The chip shows the reason after the label. Values already shown stay (they were true when published) in `--text-3`. P3.M2's kill-switch halt reuses this exact cue.
- **stale.** At least one message was received for this key in this session, and the last one is older than the threshold. Last value stays visible, italic, in `--text-3`, with its age in the chip (`◔ STALE 47s`). The age ticks up locally.
- **waiting.** The UI is connected and subscribed, and **no message has ever arrived** for this key in this session. The value is replaced by `–` (en dash). Never `0`, never blank, never a bar. The chip shows how long it has waited (`○ WAITING 1m05s`). Waiting never escalates into stale: no value existed to go stale. **Cadence subjects only** (positions, health, risk.firm, valuations, tracker): an **event** subject (`d1.alerts.limits`, `exo.alerts.recon`, `d1.exec`, `d1.crosses`, plus `exo.targets`; `NEVER` in the registry) has no cadence to miss, so an empty one never shows a WAITING chip or a running clock. Its panel reads `none since connect 11:52:07Z` as plain text, using the first connect time.
- **disconnected.** NATS link down. The whole board gets `--disconnected-filter` (greyscale) and a banner under the shell bar: `⊘ DISCONNECTED since 12:04:09Z (last data 22s old)`. On the first dial, before any link has ever been up, nothing was lost: the banner reads `○ CONNECTING to NATS, no data yet`, with no time and never `since –`. Values stay readable in `--text-3`, upright (not italic, to keep it distinct from stale); chips keep glyph and label so the last-known level is legible, but grey. All command controls (P3.M2) are disabled: no ack is possible. On reconnect the UI resubscribes and requests fresh snapshots; every value reads **stale** until its first fresh message arrives.

### Waiting is not stale

|                                         | waiting                          | stale                                      |
| --------------------------------------- | -------------------------------- | ------------------------------------------ |
| Has the key ever reported this session? | No                               | Yes                                        |
| Shows a value                           | No: `–`                          | Yes: last value, italic                    |
| Outline                                 | dotted                           | dashed                                     |
| Glyph                                   | `○` hollow                       | `◔` part-filled                            |
| Clock in the chip                       | time spent waiting               | age of the last message                    |
| Ends when                               | first message                    | next message                               |
| After a reconnect                       | unchanged (still never reported) | every previously reported key becomes this |

### Precedence when several apply to one cell

`disconnected` (board) > `waiting` > `halted` > `stale` > limit level `HARD` > `WARN` > `OK`.

Book rows (Books table) apply the ladder with one reading: a book that `exo.health` or a recon alert names as halted has reported, so HALTED shows even before its first valuation, rather than WAITING hiding the halt. A book's halt state comes from `exo.health` `book.<id>.state` (`halted:recon`, `halted:no-snapshot`) and falls back to the latest `exo.alerts.recon` only when exo.health carries no state for it. A book named by an EXO source (valuations, `exo.health` book keys, recon alerts) or by `d1.health` with any valuation token other than `none`, but with no valuation yet, reads WAITING. A book named **only** by `d1.health` with `limits.book.<id>.valuation = none` is never accepted or rejected, which is normal for a book EXO does not value (ADR-014 s3): it is not a row and never WAITING; the Books panel carries one neutral line, `no EXO valuation for books 6, 7 (normal)`.

A cell shows one chip. When `waiting`, `halted` or `stale` outranks a limit level, the last known level is kept as a small trailing glyph (`▲` or `✕`) in `--text-3`, so a breach is never hidden by staleness. The lower-ranked states are also listed in the chip tooltip.

### Thresholds (proposal, undecided until Slice 1)

Stale threshold = 3 x the producer's cadence, mirroring Delta One's own rule (`valuation_max_age_s` = three of EXO's 5 s republish intervals, `delta-one/d1.toml`). Cadences: `d1.health` / `exo.health` / `d1.positions.<book>` 1 s, `exo.valuations.<book>` 5 s, `d1.risk.firm` 1 s. The tracker is the exception: its threshold is 2.5 x `sampling_interval_s` from the message (60 s demo), as the registry (`src/bus/subjects.ts`) sets it, not 3 x. Thresholds are named constants in one module, not literals in components.

## Type

System stacks (`--font-sans`, `--font-mono`). Sentence case. The only uppercase text is a state label (OK, WARN, HARD, HALTED, STALE, WAITING, DISCONNECTED, FAULT) and a blotter kind or status code (ORDER, ALLOC, CROSS; NEW, FILLED, ...), so it reads as a code.

| Token      | Size  | Use                                          |
| ---------- | ----- | -------------------------------------------- |
| `--fs-xs`  | 11 px | Column headers (`--text-2`), units, captions |
| `--fs-sm`  | 12 px | Table cells, chips, ticker rows. All numbers |
| `--fs-md`  | 13 px | UI text, panel titles (weight 500), controls |
| `--fs-lg`  | 14 px | Book code on a card, empty-state headline    |
| `--fs-xl`  | 16 px | Reserved: disconnected banner headline       |
| `--fs-kpi` | 20 px | Tracker card headline figure (TE)            |

Line height `--lh-tight` 16 px in single-line cells, `--lh-body` 20 px in prose. Weights 400 / 500 / 600; 600 only for HARD values and the KPI figure. Five sizes in use at most on any screen.

## Numbers

- Every number: `.num` (monospace, `font-variant-numeric: tabular-nums slashed-zero`, tokens `--num-variant`, `--num-features`).
- Right-aligned in tables; column header right-aligned over it. Fixed decimals per column. Thousands separator `,`. Unit in the column header, never repeated per cell.
- Negative: leading `−` (U+2212). **No colour for sign.** Colour is for limits only.
- Zero prints `0.00`. Missing prints `–` (U+2013). Never blank, never `0` for missing.
- `price_e9` / `qty_e2` are `bigint`; formatting happens only in `src/format.ts` (`ui/CLAUDE.md` rule 2). Text from the bus is rendered as text, truncated visually, full string in a tooltip.
- A trailing `*` after an underlying means `greeks_flagged` (MC std error above config max). Neutral ink, explained in the panel footnote.

## Spacing, density, layout

- 4 px grid: `--space-1` 4, `-2` 8, `-3` 12, `-4` 16, `-5` 20, `-6` 24, `-8` 32. `--space-half` (2 px) is for borders and glyph nudges only.
- Row height `--row-h` 24 px; `--row-h-compact` 22 px is the only off-grid height and is a user density choice. Cell padding `--row-pad-x` 8 px. Dividers `--border-subtle`, 1 px. No zebra. Hover `--row-hover`; selected `--row-selected` plus a `--select-edge` inset left edge in `--text` (a non-colour selection cue).
- Header row 24 px, `--surface-2`, sticky, `--text-2` 11 px.
- Shell bar `--bar-h` 32 px, sticky: product name (the page's `h1`), four screen tabs **Risk, Tracker, Blotter, Health** (active tab in brackets in the wireframes; in the UI, `--text` plus a 2 px underline in `--text`), the **data roll-up chip** (`Data` + the worst state across the screens, tooltip lists every non-OK finding), connection chip, UTC clock, theme button (shows the current theme; click switches). One screen is shown at a time, the active tab is in the URL hash (`#risk`, `#tracker`, `#blotter`, `#health`, default risk). Gutter `--gutter` 8 px, panel padding `--panel-pad` 8 px, 12 columns.
- **No page-level `min-width`.** A 1280 px floor plus a classic 15 px scrollbar overflows the viewport and scrolls the whole page sideways. The page is fluid; the design target is 1280 px and up. Tables fit at 1280 px with the scrollbar (1265 px of viewport) by column sizing, not by clipping: panels stack full width on the risk screen, blotter columns are sized to their longest value, and the one free-text column (Detail, the reject reason) takes the leftover width and ellipsizes with the full text in a tooltip. A table that still overflows (a narrower window, a long value) is a horizontal scroller (`role="region"`, `aria-label` naming the table) that becomes focusable (`tabindex="0"`) only while it overflows, so a keyboard user can reach the hidden columns.
- Heading order: `h1` the product name; `h2` the active screen (visually hidden, the tab is its label); `h3` panel and card titles; `h4` sub-sections inside a panel.
- Panel = `--surface-1`, `--border` 1 px, radius `--radius` 2 px, 24 px title row. No shadow.
- Focus: `--focus-ring` (2 px `--text`, offset `--focus-offset`) on every interactive element and every focusable scroller, from one base rule in `tokens.css` (links, buttons, inputs, selects, anything with a `tabindex`). Scrollers draw it inside their edge so an overflow clip cannot hide it. Tables are keyboard-navigable (arrow keys move the selected row): not built yet.
- Selection and scrollbars are themed from the tokens (`::selection` is `--bg` on `--text-2`; `scrollbar-color` is `--border-strong` on `--surface-2`), so both themes follow the toggle.

## Component rules (specified here so Slice 1 builds them once)

- **State chip.** Inline, 18 px tall, `--fs-sm`, padding 0 4 px, glyph + label per the State matrix. One component, one prop (state), used for limits, books and health.
- **Limit cell** (Greek columns): value, chip, headroom bar. Only vega, gamma and rho have limits (ADR-008, ADR-014 s3 "No delta limit exists"). Delta, theta and div sens show no chip and no bar.
- **Headroom bar.** 56 x 6 px. Track `--bar-track`. Fill = `|value| / hard limit`, `--bar-fill` neutral while OK; `--state-warn-fg` at or above warn; `--state-hard-fg` at or above hard. A 1 px tick in `--text-3` marks warn and is always drawn (non-colour reading of where WARN begins). Beyond hard the fill is full and a `»` is drawn after it. Limits compare the unsigned exposure (ADR-014 s3), so the bar shows magnitude and the number keeps its sign. Waiting: dotted track, no fill. The caps come from `d1.risk.firm`: each `NetGreeks` carries the absolute warn and hard caps and the limit level (`*_level`, `*_warn_*`, `*_hard_*`, ADR-014 Amendment 1 s1), so the bar is always drawn. `limit n/a` replaces it only if a hard cap arrives as 0 (not populated).
- **Rehedge band strip.** One line per book: band edges `[ ]` centred on target `T`, position `P`, in-flight `»`. Neutral ink. Text beside it states "inside band" or "outside band" so the strip is not the only carrier.
- **Sparkline.** 1 px line in `--spark`, last N values of a ring buffer, no axes, no fill, no state hue. Stale: kept, `--text-3`. Waiting: dotted placeholder line.
- **Tracker card.** `--surface-1` panel: book code (`--fs-lg`), benchmark, kind (ex-post), chip in the title row; TE as the `--fs-kpi` figure (weight 600), TD, cash drag and cash weight in `--fs-sm`; sparkline; window and freshness line. A stale card also carries a dashed-outline strip in `--text-2` at the top ("STALE 3m12s: last known values, last message 12:01:19Z") so it never reads as an inactive book; grey italics alone are not the cue. No tracker limits exist yet, so no limit colouring (Open questions).
- **Alert ticker.** Newest first, one line per alert: UTC time, chip, `text` verbatim. Level changes only, so it can look empty on a healthy board: the panel status reads "none since connect 11:52:07Z" (event subject, no WAITING chip).
- **Blotter row.** Row kinds: `CROSS` (first-class: buy book to sell book, qty, reference price, `px_policy_id`), `ORDER` (book 0 netting parent), `ALLOC` (book N). An `ALLOC` row is an `ExecutionReport` with `allocation = true` and shows its own `book_cum_qty_e2`, never the parent's cumulative (ADR-014 Amendment 1 s3). A CROSS links to its residual ORDER by `netting_cycle_id` together with the instrument id, never by the cycle id alone, because one cycle nets many instruments (ADR-014 Amendment 1 s4); `DIRECT` is a directed transfer with no parent. Allocation rows are indented `--space-4` under their parent, which has a `▾` / `▸` toggle. Order status is neutral text with a distinct shape glyph in front of every value, never a hue: `+ NEW`, `◐ PARTIALLY_FILLED`, `✓ FILLED`, `∅ CANCELED`, `✕ REJECTED`, `◇ PENDING_CANCEL`, `◈ PENDING_REPLACE`. The kind column reads `ORDER`, `ALLOC`, `CROSS` (one casing). List is virtualized with fixed row height; never more than a viewport of rows in the DOM.
- **Blotter filters.** A row above the table: Kind, Book, Instrument and Status selects built from the values in the ring (the current choice is always kept as an option), and a case-insensitive ClOrdID / CrossId text field. Filters AND together and apply row by row: an `ALLOC` row inherits its parent order's status for the Status filter, a `CROSS` matches a Book filter on either side and has no status, so any Status filter drops it. A collapsed parent hides its allocations only while the parent row is itself shown. Changing a filter returns to the top and clears the pause-scroll hold; rows the filter hides never count toward "N new, resume". Filter state is per mount: switching screens resets it. Clear and "N rows match" appear while a filter is active.
- **Banner.** Disconnected banner and the git_sha mismatch strip: `--surface-2`, `--border-strong`, glyph + sentence. The mismatch strip uses the WARN cue.

## Accessibility

- Contrast: all text at or above 4.5:1 in both themes (tables above). Non-text outlines at or above 3:1 (`--border-strong`).
- State is never colour-only (matrix above). Glyphs are `aria-hidden`; labels are text.
- Screens are a WAI-ARIA tablist (`role="tablist"`, `aria-selected`, `aria-controls`, one tab stop with arrow keys, Home and End moving between tabs) over one `role="tabpanel"`.
- Status changes of the connection are announced through a polite live region; individual value changes are not.
- `prefers-color-scheme` does not override the dark default; theme is `data-theme="dark" | "light"` on the root element. The toggle is the shell bar's theme button; the choice is remembered per browser in `localStorage` (a convenience: blocked storage falls back to dark).
- Respect `prefers-reduced-motion`: there is no animation to remove.

## Wireframes

Illustrative values only. Where a wireframe differs from the shipped layout, the rules above win: tabs read Risk, Tracker, Blotter, Health; the shell bar also carries the data roll-up chip; the Books and Target panels are stacked full width (side by side they cannot show their columns at 1280 px); filters are a row inside the blotter panel and the event counts sit beside them (limits use the demo defaults in `delta-one/d1.toml`: vega warn 50k / hard 100k ccy per vol point, gamma warn 20k / hard 50k dDelta per 1%, rho warn 10k / hard 25k ccy per bp). Glyph key: `●` OK, `▲` WARN, `✕` HARD / rejected, `■` halted, `◔` stale, `○` waiting, `⊘` disconnected, `┈` dotted placeholder, `–` missing value. The shell bar is shared by all four screens; `[ ]` marks the active tab.

### 1. Risk board

Regions, top to bottom: firm-net Greeks by underlying (the headline table), per-book table beside the target / position / in-flight panel, limit-alert ticker. Rows shown cover every state: AAPL WARN, MSFT HARD, NESN waiting, SPX WARN, EURUSD stale (its book EXO-FX is halted).

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Desk Hedging   [Risk board]   Tracker    Blotter    Health                                         ● CONNECTED  12:04:31Z   dark │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
┌─ Firm net Greeks by underlying   (d1.risk.firm: levels and caps in it)    ───────────────────────────────────────────────────────┐
│ Underlying   Delta qty   Notional Gamma  dDelta/1%         Vega  /vol pt            Rho  /bp                 Theta/day Div sens  │
│ AAPL*        12,300.00  2,411,820  12,400 ●OK    ██·|····   62,000 ▲WARN  ████|···    3,100 ●OK    █··|····       −840      210  │
│ MSFT         −4,150.00 −1,730,550   8,100 ●OK    █··|····  104,500 ✕HARD  ████|███»  −1,200 ●OK    ···|····     −1,120       95  │
│ NESN                 –          –       – ○WAIT  ┈┈┈┈┈┈┈┈        – ○WAIT  ┈┈┈┈┈┈┈┈        – ○WAIT  ┈┈┈┈┈┈┈┈          –        –  │
│ SPX          55,000.00 28,602,750  30,200 ▲WARN  ███|█···   21,900 ●OK    ██··|···    4,800 ●OK    ██·|····     −2,030        –  │
│ EURUSD     −120,000.00   −130,044   2,900 ◔STALE ···|····    9,400 ◔STALE █···|···     −700 ◔STALE ···|····       −190        –  │
│                                                                                                                                  │
│ Delta has no limit (ADR-014 s3): it is hedged, see the rehedge band below.  Theta and div sens are monitored, no limit.          │
│ Bar = |value| / hard limit, | = warn.  » = beyond hard.  * = MC std error above config max (greeks_flagged).  Last msg 0.4s ago. │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
┌─ Books ──────────────────────────────────────────────────────────┐ ┌─ Target / position / in-flight ─────────────────────────────┐
│ Book       State           Delta qty      Vega Val. age          │ │ AAPL (selected)   band = band_qty_e2, ADR-014 s11           │
│ D1-CORE    ●OK             12,000.00         –        —          │ │ Book         Target   Position  In-flight                   │
│ D1-ETF     ●OK                300.00         –        —          │ │ EXO-SP     8,000.00   7,100.00     500.00                   │
│ EXO-SP     ●OK             55,000.00    42,100       3s          │ │          [···P·»T···]  gap 400.00 inside band               │
│ EXO-FX     ■HALTED       −120,000.00     9,400    ◔ 47s          │ │ D1-CORE    4,300.00   4,300.00       0.00                   │
│ RATES-IR   ○WAIT                   –         –        –          │ │          [····PT····]  at target                            │
│                                                                  │ │ Firm      12,300.00  11,400.00     500.00                   │
│ EXO-FX: recon halt, D1 view ≠ EXO view (exo.alerts.recon).       │ │          P position  » in-flight  T target                  │
│ RATES-IR: awaiting_marks (exo.health book.5.state).              │ │          [ ] band edges, centred on target                  │
└──────────────────────────────────────────────────────────────────┘ └─────────────────────────────────────────────────────────────┘
┌─ Limit alerts   (d1.alerts.limits) ──────────────────────────────────────────────────────────────────────────────────────────────┐
│ 12:04:29Z  ✕HARD  MSFT vega 104,500.00 USD/vol-pt >= hard 100,000.00                                                             │
│ 12:03:58Z  ▲WARN  AAPL vega 62,000.00 USD/vol-pt >= warn 50,000.00                                                               │
│ 12:03:11Z  ▲WARN  SPX gamma 30,200.00 dDelta/1% >= warn 20,000.00                                                                │
│ 12:01:40Z  ●OK    SPX rho back inside warn   (cleared)                                                                           │
│ 12:00:02Z  ▲WARN  book 4: stale (no valuation for 15 s)   forced warn, ADR-014 s3                                                │
│ newest first, level changes only (no hysteresis), text rendered as text. Empty: "No limit alerts since connect 11:52:07Z".       │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2. Tracker scorecard

One card per tracker book. Left to right: live, stale, waiting.

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Desk Hedging    Risk board   [Tracker]   Blotter    Health                                         ● CONNECTED  12:04:31Z   dark │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
┌─ D1-CORE  vs SPX  ex-post  ●OK ─────────┐ ┌─ D1-ETF  vs SPX  ◔STALE 3m12s ──────────┐ ┌─ book 6  ○WAITING 1m05s ─────────────────┐
│ Tracking error (ann.)                   │ │ Tracking error (ann.)                   │ │ Tracking error (ann.)                    │
│      38.2 bps                           │ │      41.0 bps   (last known)            │ │      –                                   │
│ Tracking difference   −12.4 bps         │ │ Tracking difference   −15.0 bps         │ │ Tracking difference   –                  │
│ Cash drag              −3.1 bps         │ │ Cash drag              −2.2 bps         │ │ Cash drag             –                  │
│ Cash weight             0.8 %           │ │ Cash weight             1.1 %           │ │ Cash weight           –                  │
│                                         │ │                                         │ │                                          │
│ TE  ▁▂▃▃▄▅▅▆▅▄▄▃▃▄▅▆▇▆▅▅▄▄▅▅            │ │ TE  ▁▂▃▃▄▅▅▆▅▄▄▃▃▄▅▆▇▆▅▅▄▄▅▅            │ │ TE  ┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈             │
│     250 obs   window 12:04Z             │ │     250 obs   window 12:01Z             │ │     – obs                                │
│     updated 21s ago                     │ │     last message 12:01:19Z              │ │     no message on d1.tracker.6 yet       │
│ no tracker limit exists: neutral ink    │ │ values italic, text-3; sparkline kept   │ │ never 0.0: a zero would read as perfect  │
└─────────────────────────────────────────┘ └─────────────────────────────────────────┘ └──────────────────────────────────────────┘
┌─ Notes ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ One card per d1.tracker.<book>. Cards in book_id order. TE, TD, cash drag are ADR-010 ex-post values; units formatted in         │
│   format.ts.                                                                                                                     │
│ Sparkline = ring buffer of the last TE values received (neutral ink). Stale threshold = 2.5 x samplingIntervalS from the message.│
│ Empty (no tracker books configured): "No tracker books. d1.tracker.* has not been configured." Never shown while any message     │
│   exists.                                                                                                                        │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 3. Blotter

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Desk Hedging    Risk board    Tracker   [Blotter]   Health                                         ● CONNECTED  12:04:31Z   dark │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
┌─ Filters ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Kind [all ▾]  Book [all ▾]  Instrument [all ▾]  Status [all ▾]  Text [ClOrdID / CrossId]        1,284 events since connect       │
│   11:52:07Z                                                                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
┌─ Order and execution blotter   (d1.exec.<book>.<instrument>, d1.crosses) ────────────────────────────────────────────────────────┐
│   Time      Kind   ClOrdID / CrossId Book      Instr.   Side         Qty         Px  Status                Filled     Leaves     │
│ ▾ 12:04:29  CROSS  x-0000791         3 → 1     AAPL     —       2,000.00     241.18  residual cl-0000412 2,000.00       0.00     │
│ ▾ 12:04:29  ORDER  cl-0000412 (net)  0 firm    AAPL     BUY     5,300.00     241.20  PARTIALLY_FILLED    3,100.00   2,200.00     │
│   12:04:30  alloc  cl-0000412/3      3 EXO-SP  AAPL     BUY     1,900.00     241.20  PARTIALLY_FILLED    1,100.00     800.00     │
│   12:04:30  alloc  cl-0000412/1      1 D1-CORE AAPL     BUY     3,400.00     241.20  PARTIALLY_FILLED    2,000.00   1,400.00     │
│ ▸ 12:04:11  ORDER  cl-0000411 (net)  0 firm    SPX      SELL    1,000.00   5,200.50  FILLED              1,000.00       0.00     │
│ ▸ 12:03:58  ORDER  cl-0000409 (net)  0 firm    MSFT     SELL      900.00          –  ✕ REJECTED              0.00       0.00     │
│ ▸ 12:03:40  ORDER  cl-0000407 (net)  0 firm    AAPL     BUY       700.00     241.05  ◇ PENDING_CANCEL      300.00     400.00     │
│                                    ⋮  virtualized: 42 rows in DOM of 1,284, fixed 24 px row height  ⋮                            │
│ CROSS is its own row kind: buy book → sell book, ref px policy in the detail line (px_policy_id). It is a booked trade, not a    │
│   footnote.                                                                                                                      │
│ ORDER (net) = book 0 netting parent; alloc rows (allocation flag, book N) sit indented under it, each with its own               │
│   book_cum_qty_e2. Orders are keyed by ClOrdID; reports are deduped by (ExecID, book_id), ADR-014 s5. ▾ expanded, ▸ collapsed.   │
│ A CROSS links to its residual ORDER by netting_cycle_id plus the instrument id, never the cycle id alone (one cycle nets         │
│   many instruments; ADR-014 Amendment 1 s4). Status column: residual <ClOrdID> | transfer (DIRECT) | no residual | no link.      │
│ Status is plain text: no state colour. ✕ and ◇ are shape cues only. Newest first; the list does not auto-scroll while a row is   │
│   selected.                                                                                                                      │
│ Empty: status "none since connect 11:52:07Z"  (never "no orders": exec messages are a stream, there is no snapshot to   │
│   prove it)                                                                                                                      │
│ Before the first d1.exec message: "none since connect 11:52:07Z" as plain text (event subject: no WAITING chip, no clock).      │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4. System health

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Desk Hedging    Risk board    Tracker    Blotter   [Health]                                        ● CONNECTED  12:04:31Z   dark │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
┌─ Components ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Component        State        Detail (as published, d1.health / exo.health detail map)                                           │
│ Delta One        ✕FAULT       heartbeat 0.8s ago   ok = false: fix.session = logged_out                                          │
│ EXO              ●OK          heartbeat 0.6s ago   publish: ok   valuation_in_flight: false                                      │
│ NATS (UI link)   ●OK          connected   reconnects this session: 0                                                             │
│ NATS (D1 link)   ●OK          d1.health nats = connected   (connected | pending | disconnected)                                  │
│ Post-trade       ●OK          posttrade = running   posttrade.send_failures = 0                                                  │
│ FIX session      ✕FAULT       fix.session = logged_out   (ok = false)                                                            │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
┌─ Per-book state ───────────────────────────────────────────────┐ ┌─ Last valuation metadata (ValuationSnapshot.valuation) ───────┐
│ Book       State  (exo.health book.N.state) Last valuation     │ │ Book  model_id       seed  n_paths git_sha           as_of    │
│ 1 D1-CORE  ●OK  publishing                       12:04:30Z     │ │ 3     heston_lv     20260  200,000 4f2a9c1        12:04:28    │
│ 3 EXO-SP   ●OK  publishing                       12:04:28Z     │ │ 4     heston        20261  200,000 4f2a9c1-dirty  12:03:44    │
│ 4 EXO-FX   ■HALTED  halted:recon                 12:03:44Z     │ │       params_hash in the row detail, in full                  │
│ 5 RATES-IR ○WAIT  awaiting_marks                         –     │ │       PV std error per product line: drill-down               │
└────────────────────────────────────────────────────────────────┘ └───────────────────────────────────────────────────────────────┘
┌─ Provenance check (root invariant 7) ────────────────────────────────────────────────────────────────────────────────────────────┐
│ ▲WARN  git_sha mismatch: book 3 = 4f2a9c1, book 4 = 4f2a9c1-dirty, exo.health = 4f2a9c1                                          │
│ Compares every book's valuation git_sha with exo.health detail git_sha and with each other. Values verbatim, never hidden.       │
│ No mismatch: the strip is absent and a single ● "all valuations from one git_sha" line sits in the metadata panel header.        │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Disconnected (applies to every screen)

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Desk Hedging   [Risk board]   Tracker   Blotter   Health                      ⊘ DISCONNECTED since 12:04:09Z  (last data 22s old)│
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
┌─ Disconnected: whole board greyscale, values kept, last-known chips kept in grey ────────────────────────────────────────────────┐
│ Underlying   Delta qty   Notional   Gamma ...     (all values text-3, upright, no italic, no state colour)                       │
│ AAPL*        12,300.00   2,411,820   12,400 ●OK   ...   chips keep glyph + label so the last level stays legible, but grey       │
│ Reconnect: resubscribe, request fresh snapshots; each value reads stale (◔ age) until its first fresh message arrives.           │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Open questions found while designing (do not block this slice)

Questions 1, 2, 3 and 5 were open when this file was written, before the backend existed. They are settled by ADR-014 Amendment 1 and kept here only as a record.

1. **Headroom bars need the limit values.** Settled: `RiskSnapshot` carries them. Each `NetGreeks` has `vega_*`, `gamma_*` and `rho_*` `_level`, `_warn_*` and `_hard_*` fields (absolute caps from `d1.toml [limits.defaults]`, ADR-014 Amendment 1 s1), so the bars do not degrade to `limit n/a`. That text appears only for a hard cap that arrives as 0.
2. **Limit levels on connect.** Settled: `d1.risk.firm` is published every 1 s and carries the level, so a UI that connects mid-state learns it from the next snapshot, not only at the next alert. `LIMIT_LEVEL_UNSPECIFIED` means not populated and reads **waiting**, never OK.
3. **`d1.risk.firm` is published** (ADR-014 Amendment 1 s1; `protocol/nats-subjects.md`). The risk board's headline table reads it.
4. **No tracker limits exist** (`d1.toml [tracker]` has none; ADR-010 defines none), but `ui/CLAUDE.md` asks for "limit coloring" on TE, TD and cash drag. The cards stay neutral until a limit source exists.
5. **The `d1.health` detail keys** are named in ADR-014 Amendment 1 s2: `nats`, `fix.session`, `posttrade`, `posttrade.send_failures`, `limits.unattributable_rejects`, `limits.book.<id>.rejects` and `limits.book.<id>.valuation`. There is no lag key and no halted key. The health screen shows "not published yet" for a missing item.
6. **`ui/CLAUDE.md` lists the tracker scorecard as part of the risk board; the plan for this slice makes it its own screen.** Navigation follows the plan.
7. **Books and Target stacked.** The wireframe puts them side by side; Books has 13 columns and does not fit in half of 1265 px. Stacked full width is the layout until Books loses columns (model, paths and seed are also on the Health screen) or a wider floor is agreed.
8. **Arrow-key row selection** (Spacing, Focus) is specified but not built; no finding has asked for it yet.
