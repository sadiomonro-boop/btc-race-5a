# Critic report — make.fun BTC race bot, variant A

- **Bot under review:** `~/workspace/makefun-btc-race-5/variant-a/main.py` (199 lines, Python 3.13, vendored `vendor/makefun_bars.py` shim, protocol `agent-fun/bars-replay/v1`)
- **Rules:** `~/workspace/makefun-btc-race-5/kit/btc-race-24h-20261118-1444-kit/RULES.txt` + harness source (`contracts/bars-protocol.ts`, `sim/bars/engine.ts` in `harness/harness-src.tar.gz`)
- **Competition:** `btc-race-24h-20261118-1444`, $250, top 10 by net PnL on an unseen 24 h race window (1,440 one-minute cbBTC bars)
- **Claimed local result (taken as given, not re-run):** dev net PnL +$4.98, maxDD 0.17%, 10 trades, 0 rejections, deterministic (two runs byte-identical)
- **Dev window verified independently from the kit dataset:** 1,440 bars, **zero** missing minutes (all steps exactly 60 s), first close → last close −281 bps (−2.81%), 152 flat bars, 1-min |close-to-close| moves: median 2 bps, p90 9 bps, p99 25 bps, **max 63 bps**; open depth ≈ $0.67–1.0M. These numbers are used below to size several findings.
- **Scope:** defects, bugs, and risks only. No strategy redesign is proposed.

## Verdict in one paragraph

The bot is protocol-clean on the dev window: it cannot breach any cap, cannot crash on any reachable input, and is deterministic. Its real problems are quieter. The single most important finding is **D1**: the volatility filter the strategy claims to apply (`STD_MULT_BPS`, "dip must clear 1.0× std") is vacuous because of a factor-100 scaling error — on the dev window its threshold is literally 0 bps, so the filter never fires and the "tuned" parameters were tuned against a different strategy than the one documented. The other live defects are a missing-bar blind spot (D2), a hardcoded window-length gate that is both unnecessary and fragile (D3), and risk controls that do not do what their names promise (D4–D6). Strategically, the edge is razor-thin (+$4.98 over 10 trades, ≈ $0.50/trade against ≈ $0.40 round-trip cost per $250 sleeve), so the unseen window needs only a small regime shift to flip the sign.

## Counts

- **28 items total: 7 real defects, 11 accepted risks (incl. strategy weaknesses), 10 non-issues.**

---

## A. Real defects

### D1 — The std volatility gate is vacuous: factor-100 scaling error (MOST IMPORTANT)
- **Location:** `main.py:39` (`STD_MULT_BPS = 100`), `main.py:167-170`, docstring `main.py:1-18`.
- **Behavior:** The gate is
  ```python
  if dip_bps < STD_MULT_BPS * std_bps // 10000:
      return orders
  ```
  Python evaluates this as `(100 * std_bps) // 10000 = std_bps // 100`. The comment claims `STD_MULT_BPS` in bps with "100 = 1.0x", but in bps convention 100 bps = 1%, so 1.0× std would need `STD_MULT_BPS = 10000` (or the divisor to be 100, not 10000). As coded, the threshold is **1% of one standard deviation**.
- **Evidence it never binds:** `std_bps` is the 60-bar close std in bps of the mean. On the dev window 1-min moves are median 2 bps / p99 25 bps, so 60-bar `std_bps` is of order 15–30 bps and the threshold is `15//100 … 30//100 = 0` bps. The gate only binds if `std_bps > 6000` (a 60% 60-bar std — impossible for BTC 1-min closes). `DIP1_BPS = 60` already requires `dip_bps >= 60`, which is always ≥ the 0–1 bp std threshold. The filter therefore **never rejects anything on any realistic window**.
- **Why it matters:** the strategy as coded is not the strategy as documented and tuned. Whatever grid search produced `DIP1/DIP2/TP/SL` was run with this filter effectively absent, so the "tuned" configuration carries no evidence that a real 1.0×-std significance filter would behave the same. On a high-volatility race window the gate that was supposed to demand a significant dip does nothing, and the bot buys every 60 bp dip even in noise regimes. This is a bug, not a tuning choice: code and documented intent disagree by exactly 100×.

### D2 — Missing-bar batches are traded on a stale close, and duplicate closes corrupt the 60-bar window
- **Location:** `main.py:118-127` (`on_batch` never inspects `batch["bars"]`).
- **Behavior:** Per RULES ("Prices and bars"), a minute with no recorder sample has **no bar**: CBBTC is absent from that batch's `bars`. The shim's `state.latest` keeps the *last* bar, so on such a batch `state.close(SYMBOL)` returns the previous minute's close. The bot then (a) appends that stale close to `self.closes` (line 126), (b) updates both EMAs on the same price again (line 127), and (c) can emit buy/sell orders decided against a price minutes old.
- **Consequences:**
  1. The 60-bar mean/std window (`_mean_std_prev`, lines 85-93) can contain up to N duplicated closes; with the 28 missing minutes a valid window may contain, nearly half the window can be one repeated value, biasing the mean toward the stale price and shrinking std. The dip signal is then computed against a distorted window.
  2. A buy decided on a stale close fills at the *next* bar's open; `max_slippage_bps` is measured from the stale close, so if the missing minutes concealed a >~96 bp move the fill is either rejected (benign) or, worse, fills up to 100 bps away from the price the dip was measured at.
  3. `seq`-based gates (warmup, cooldown, last-batch) still advance on gap batches, so the effective trading window shrinks by the number of gaps.
- **Severity:** low–medium. The dev window has zero gaps, so this never fired locally (consistent with 0 rejections), and a valid race window has at most 28 gaps. But it is a genuine blind spot: the bot cannot distinguish a missing bar from a flat bar because it never looks at `batch["bars"]`, and the failure mode (trading on stale data) is silent. The correct handling would be to still apply fills on such a batch (fills are delivered regardless of bars) but return `[]` without appending a close or deciding orders.

### D3 — Hardcoded `N_BARS = 1440` last-batch gate: unnecessary and fragile to seq numbering
- **Location:** `main.py:43` (`N_BARS = 1440`), `main.py:129` (`seq >= N_BARS - 1: return []`).
- **Behavior:** the bot refuses all orders from seq 1439 on, assuming exactly 1,440 batches numbered from 0 with no warm-up.
- **Why it is unnecessary:** an order decided on the last batch is rejected `after_end` by the harness (protocol header, `sim/bars/engine.ts` `decide()`), reported in the action log only. That rejection is *not* a protocol violation, costs nothing, and cannot fill. The gate therefore buys zero safety.
- **Why it is fragile:** if `hello` ever carries `warmup_bars = W` (the protocol supports 1–1440; `state.warming_up` is already handled at line 129, so the author anticipated warm-up), total batches become 1440 + W and the last scored batch is seq 1439 + W. The bot would then stop trading W bars early — including refusing exits — for the whole tail of the scored window. Likewise if a window ever had a different length, the gate misfires in either direction (early stop, or orders on the true last batch, which are harmlessly rejected anyway). The length is not in `hello`; the robust behavior is to not gate on it at all, since the harness already rejects last-batch orders safely.
- **Severity:** low as shipped — RULES.txt never mentions a warm-up for this competition, and the dev window is exactly 1,440 bars, so the gate almost certainly never fires wrongly. It is dead risk carried for no benefit.

### D4 — Stop-loss cooldown starts at the *decision*, even when the stop-loss sell is rejected and no loss was realized
- **Location:** `main.py:142-143` (`self.last_sl_seq = seq` inside the exit loop), `main.py:159-160` (cooldown check).
- **Behavior:** `last_sl_seq` is set the moment a close prints at or below the stop level, before the sell is sent and before anyone knows whether it fills. If that sell is then rejected at the next open (`max_slippage` after a gap, or `no_bar` if the next minute has no bar), `_apply_fills` correctly frees the lot (`exiting = False`, lines 102-107) — but `last_sl_seq` is **not** rolled back. The bot then refuses new buys for 5 bars after a stop-loss that never happened, while the original lot is still held and still trying to exit.
- **Related wart:** while a rejected SL sell keeps failing, the lot re-triggers the SL branch every bar and re-sets `last_sl_seq` each time, so the cooldown keeps refreshing — conservative, but it means the cooldown clock measures "last bar the stop level printed", not "last filled stop-loss", which is not what the docstring ("15-bar cooldown after any stop-loss", itself wrong — see D7) describes.
- **Severity:** low. On the dev window no sell was ever rejected, so this never fired. It only costs a few bars of missed entries in the (rare) rejection case.

### D5 — `MAX_SLIP_BPS = 100` is ~3× the 35 bp stop, so the stop barely controls the realized loss
- **Location:** `main.py:37` (`SL_BPS = 35`), `main.py:41` (`MAX_SLIP_BPS = 100`), `main.py:154-155` (sells carry the same 100 bp allowance).
- **Behavior:** exits are decided on the close and fill at the *next* bar's open, with the fill allowed to be up to 100 bps below that close. A stop-loss exit intended to cap the loss at −35 bps can therefore realize roughly −(35 + 100) bps plus fees ≈ −135 bps (≈ $3.40 on a $250 sleeve) instead of the intended −35 bps (≈ $0.88). The same looseness applies to buys: a dip-buy decided 60 bps below the mean can fill up to 100 bps *above* the decision close — i.e. ~40 bps *above* the mean — so the lot can be opened at a price that violates the very dip premise it was bought on. TP/SL levels are recomputed from the actual fill price (lines 108-112, 140-141), so the exit math adapts, but the entry-quality damage is silent.
- **Mitigating fact:** on the dev window the max 1-min move was 63 bps, so a 100 bp allowance never bound and no fill ever came close to the limit. The exposure only materializes in a fast race-window minute (flash move / gap), which is exactly when loss control matters most.
- **Severity:** low–medium. It is a parameter inconsistency rather than a code bug, but the stop's stated purpose (cap the loss at 35 bps) is not what the code enforces.

### D6 — No late-window entry cutoff: the bot can open a sleeve at seq 1438 that is flattened one minute later
- **Location:** `main.py:129` (only the last batch is gated), `main.py:181-188` (buy path has no horizon check).
- **Behavior:** a buy decided on batch 1438 fills at bar 1439's open and is then flattened at bar 1439's close by the harness (RULES: everything held at window end is sold at the last close). That trade captures at most one minute of price movement while paying the full round trip — ≈ 4 bps fee + 2 bps slippage + ≈ 2.5 bps impact each way, ≈ 16–17 bps ≈ $0.42 on a $250 sleeve — with no time for TP (+150 bps) or mean recovery to plausibly trigger. Its expectancy is roughly −$0.40 before any price move.
- **Severity:** low (at most one such trade per run, ≈ −$0.40 expected). The same logic applies with decaying force to buys in the last ~10–30 bars, which have little time to reach either exit.

### D7 — Docstring / shipped-file mismatches (documentation defects)
- **Location:** `main.py:1-18` (module docstring), `main.py:42` (`COOLDOWN_BARS = 5`), `main.py:30` (`WARMUP_BARS = 30`).
- **Items:**
  1. Docstring line 11: "15-bar cooldown after any stop-loss" — code is **5** bars (`COOLDOWN_BARS = 5`, line 42; check at line 159). The 15-vs-5 discrepancy is a 3× error in a risk parameter, and a reader (or a later tuner) cannot tell which value was actually validated.
  2. Docstring line 3: "see BUILD-REPORT.json" — **no such file exists** in `variant-a/` (directory contains only `LICENSE`, `main.py`, `vendor/`). The tuning evidence the docstring points to is not shipped with the entry.
  3. Docstring line 4: "Warm up on the first 60 bars" vs `WARMUP_BARS = 30` (line 30). Effective behavior *is* ~60 bars because of the `len(self.closes) <= MEAN_WIN` gate (line 131), so the docstring is accidentally right and the constant is misleading dead weight.
  4. Docstring line 13: "Orders are sized against the latest close" — orders are sized as 25% of equity in quote units (line 181); the close is never used for sizing. Cosmetic, but wrong.
- **Severity:** low for grading (the grader runs code, not comments), but real for review: the entry's own description of itself is unreliable in four places, which weakens confidence in the tuning story behind D1.

---

## B. Accepted risks — strategy weaknesses on the UNSEEN race window

These are not code bugs; they are properties of the chosen strategy that decide whether the thin dev edge survives a different 24 hours. They are listed as accepted risks because fixing any of them would be a strategy change, which is out of scope for this review.

### R1 — The edge is razor-thin and estimated from 10 trades: the dev gate itself is barely passed
- **Numbers:** dev net PnL +$4.98 on $1,000 (+0.50%) across 10 trades ⇒ ≈ **+$0.50/trade** average. A $250 sleeve's round trip costs ≈ 4 bps fee + 2 bps slippage + ≈ 2.5 bps impact, each way — ≈ 16–17 bps ≈ **$0.40–0.43** per round trip. So the *gross* edge is only ≈ $0.90/trade, and the net edge is about one round-trip cost per trade. A small regime shift (slightly fewer bounces, slightly more stops) flips the sign. Ten trades is also far too few to distinguish skill from noise — the standard error on the mean trade is of the same order as the mean itself.
- **Consequence:** the entry passes the dev gate (+$4.98 > 0) by a margin that is well within noise. Nothing about the dev result predicts a positive race-window PnL with any confidence. This is the top *strategic* risk (the top *finding overall* remains D1, because D1 means even the dev evidence is about a different filter than documented).

### R2 — Sleeve 2 has no trend filter: the bot doubles down into a falling market
- **Location:** `main.py:175-180`. When `held == 0` the buy requires `trend_up` (fast EMA no more than 100 bps below slow EMA, lines 173-174). When `held == 1` the only extra condition is a deeper dip (`dip_bps >= DIP2_BPS`); the trend check is skipped entirely.
- **Behavior on a downtrend:** sleeve 1 is bought on a dip, price keeps falling, the dip deepens to 120 bps, and sleeve 2 is bought with no regard to the EMA gap that would have blocked sleeve 1. In a waterfall decline both sleeves can be filled and then stopped out in sequence, and the cycle can repeat after the 5-bar cooldown. Each stopped cycle loses ≈ 35 bps + ≈ 16 bps costs ≈ 51 bps on $250 ≈ **$1.28**; a few such cycles erase the entire dev profit.
- **Note:** the docstring describes sleeve 2 as "deeper dip while sleeve 1 is still held", so this matches the documented design — it is a design weakness, not a spec violation.

### R3 — No time-based exit: an underwater sleeve can sit for hours, capital locked, then flatten at a loss
- **Location:** `main.py:137-155` (exits are only TP / SL / mean-recovery; no holding-period or end-of-window exit).
- **Behavior:** if price drifts sideways below the mean but above the stop, a sleeve is held indefinitely — it cannot be redeployed, and with 2 sleeves held no new entries are possible (`held >= 2`, line 162). At window end the harness flattens everything at the last close, realizing whatever loss accrued. The 35 bp stop bounds the single-sleeve loss only if exits fill (see D5); otherwise the exposure is open-ended in time, though bounded in price by the stop level on closes.
- **Severity:** low–medium. The stop usually bounds the damage; the cost is mostly opportunity cost (dead capital in a 24 h race).

### R4 — Flat / rangeless race window ⇒ zero trades ⇒ guardrail failure
- **Location:** entry gates at `main.py:164-170` (dip ≥ 60 bps below the 60-bar mean *and* the std gate).
- **Behavior:** the guardrails (RULES) require **at least 1 trade** on the race window (the final flatten does not count) and maxDD ≤ 25%. On the dev window the bot traded 10 times, but that window fell 281 bps with plenty of 60 bp dips. In a genuinely flat 24 h window, closes hug the mean, dips of ≥ 60 bps may never print, and the bot makes **0 trades** — ranking below every entry that meets the bar and taking no prize regardless of PnL (which would be 0). Probability is low (BTC 1-min prices rarely stay within ±60 bps of their 60-min mean for a full day; the dev window had 152 flat bars yet still produced 10 trades), but the impact is total.

### R5 — The mean-recovery exit target falls in a downtrend
- **Location:** `main.py:85-93` (`_mean_std_prev` excludes the current bar), `main.py:145` (`close >= mean`).
- **Behavior:** the exit target is the trailing 60-bar mean, which itself declines in a downtrend. A sleeve bought on a dip can "recover to the mean" at a progressively lower price, so the exit locks in less than the dip it bought — and in a steady bleed the mean-recovery exit can trigger at a price *below* the fill, i.e. the "winning" exit realizes a small loss after costs. This is inherent to trailing-mean exits, not a bug.

### R6 — The +150 bp take-profit is mostly decorative; the mean exit dominates
- **Location:** `main.py:36` (`TP_BPS = 150`), `main.py:145`.
- **Evidence:** on the dev window the largest single 1-min move was 63 bps, so reaching +150 bps above a fill requires a sustained multi-minute run; the mean-recovery exit (dip of 60–120 bps) triggers first in the normal case. The effective payoff per winning trade is therefore the dip size minus round-trip costs (≈ 60–120 bps gross, ≈ 44–104 bps net), not the advertised 150 bps. The 4.3:1 TP:SL ratio in the parameter list overstates the realized payoff asymmetry.

### R7 — No depth / impact awareness in sizing
- **Location:** `main.py:181-183` (notional is 25% of equity, capped by cash; `state.depth_at()` / `state.impact_bps()` are never called).
- **Behavior:** at the frozen depth (≈ $1M) a $250 sleeve pays ≈ 2.5 bps impact — negligible, so this is harmless as shipped. But pool depth varies bar to bar (dev `o_depth` ranged ≈ $0.67–1.0M), and the bot is blind to it: if race-window depth thins sharply, impact grows silently and is only discovered via fills. Accepted risk at this size; it would matter more at larger sleeves.

### R8 — Same-bar re-entry is blocked after any exit, and the 5-bar post-SL cooldown misses V-bounces
- **Location:** `main.py:156-157` (`sold_this_bar → return orders`), `main.py:159-160` (cooldown).
- **Behavior:** after a TP/mean exit the bot cannot re-enter on the same bar even if a fresh dip signal is present (one-bar delay); after a stop it sits out 5 bars, so a sharp V-shaped recovery in the minutes after a stop is missed. Cost is a few bars of delay per event — small, and the sold-this-bar rule also correctly prevents sells from funding same-reply buys (RULES: sell proceeds never fund a buy of the same reply).

### R9 — Fill-latency gap risk is inherent: every decision is one bar stale at fill time
- **Location:** `main.py:140-155` (TP/SL/mean evaluated on the close; fills occur at the next bar's open per RULES).
- **Behavior:** between the decision close and the fill open, price can move against the bot: a TP exit can fill below its TP level, an SL exit below its stop, a dip-buy above its dip. On the dev window this was negligible (median 1-min move 2 bps), but it is the mechanism behind D5's worst case and it applies to every single trade. Accepted risk — the protocol imposes the latency; no 1-min bot can avoid it.

### R10 — Hardcoded assumptions about `hello` (caps, fees, min notional, window length) are never validated
- **Location:** `main.py:39-43` (hardcoded `1000000` min notional at line 184, `N_BARS = 1440`, `MAX_SLIP_BPS = 100`); the bot never reads `state.hello["caps"]` / `["fees"]`.
- **Behavior:** if the grader's `hello` ever differed from RULES.txt (different min notional, different `max_weight_bps`, different fee/slippage), the bot would silently mis-size or mis-gate. Per RULES the values are fixed for this competition (1,000 USDC start, max 1 position, 100% weight, 16 orders, $1 min notional, 4 bp fee, 2 bp slippage), and the local grade confirms them, so this is an accepted robustness risk, not a live defect. D3 is the one place where a hardcoded assumption can actually bite.

### R11 — The trend filter is very permissive
- **Location:** `main.py:40` (`TREND_TOL_BPS = -100`), `main.py:173-174`.
- **Behavior:** sleeve 1 is allowed whenever the fast EMA is no more than 100 bps (1%) below the slow EMA. In a −2.8%/day drift like the dev window, that condition is satisfied most of the time, so the filter screens out only steep downtrends. Combined with R2 (no filter at all on sleeve 2), trend protection is thin. Accepted risk — it is the documented design.

---

## C. Non-issues (checked, no defect)

### N1 — Cap compliance: no cap can be breached
| Cap (RULES) | Bot behavior | Verdict |
|---|---|---|
| Max 1 open position | Single symbol `CBBTC`; max 2 lots = 1 position (lines 161-163) | ✓ cannot breach |
| Max weight 100% of equity | Max 50% invested at cost (2 × 25%); position value / equity < 100% while cash > 0 (cash stays ≥ ~$500) | ✓ cannot breach |
| Max 16 orders / reply (>64 fails run) | At most 2 sells (lines 137-155, early return at 156-157) *or* 1 buy (lines 186-188); max 2 per reply | ✓, reply is a few hundred bytes vs the 64 KiB limit |
| Min notional $1 (1,000,000 units) — buys | Enforced at lines 184-185 (`notional < 1000000 → return`) | ✓ |
| Min notional — sells | Sells are full-lot quantities worth ≈ $250 at the latest close, far above $1 | ✓ at any realistic price (see N9 for the pathological case) |
| Buys ≤ cash held; sells never fund same-reply buys | Notional capped by cash (lines 182-183); only one buy per reply; `sold_this_bar` returns before any buy (lines 156-157) | ✓ `insufficient_cash` unreachable |
| Client ids | `_next_id` (lines 72-74) yields unique `b<n>` / `x<n>`; never starts with `_` (reserved); matches the shim's regex; no id is ever reused | ✓ `duplicate_client_id` unreachable |
| `max_slippage_bps` range | Constant 100, within 0–10,000 (line 41) | ✓ |
| No shorts / leverage | Only buy-then-sell of one long-only symbol | ✓ |
| Last batch | No orders at seq ≥ 1439 (line 129) — belt-and-braces; the harness would reject them `after_end` harmlessly anyway | ✓ |

### N2 — Determinism: no nondeterminism hazard in `main.py`
- Integer math only: `//` floor division on positive prices, `math.isqrt` (lines 46-47, 82-83, 89-92, 140-141, 164, 173, 181). No floats anywhere in the bot. No clock, no `os.environ` reads (`os` is used only for the `__file__`-based `sys.path` insert, lines 20-24), no `random`, no time, no file reads.
- `self.lots` is a `dict` keyed by strings, iterated in insertion order (deterministic in CPython; `PYTHONHASHSEED=0` is also set by the runtime). No set is iterated; `pending_sells` is only looked up by key. The multi-sell reply order is therefore deterministic.
- `state.equity` / `state.cash` / closes all come from the harness as ints. Consistent with the claimed two byte-identical local runs. The docstring's determinism claim (line 16) is true.

### N3 — Fill / lot state machine is correct
- **Rejected sell frees its lot** (lines 102-107): `exiting` reset to `False`, so the lot retries its exit on the next bar. Correct.
- **Rejected buy is a no-op** (lines 101-107): the client id is not in `pending_sells`, no lot is created, cash was never spent (the engine rejects before moving cash). The bot simply retries while the signal persists. Correct.
- **No double-sell:** each lot is sold at most once per bar (`exiting` flag, lines 138, 152); two lots selling in one reply sell `lot1.qty` then `lot2.qty`, and the engine's `insufficient_position` check (`available = held − sold-earlier-in-reply`) admits exactly that (verified against `sim/bars/engine.ts` `decide()`). Correct.
- **Sells of exiting lots:** at decision time no lot can still be `exiting` — fills (including rejections) are always delivered in the very next batch and `_apply_fills` runs before decisions (line 119) — so the `exiting` flag and the `held`-excludes-exiting count (line 161) are effectively transient/dead state, but harmless. The only exception (orders decided on the last batch, whose `after_end` rejections are never delivered) cannot occur because of the line-129 gate.
- **Fill price/qty are authoritative:** lots are built from the fill's actual `qty`/`price` (lines 108-112), not from the decision-time close, so TP/SL track the true cost basis. Correct.
- **Flatten fills (`_flatten_*`)** arrive in `done`, not in a batch; the bot defines no `on_done` and never sees them — irrelevant, the run is over and the harness does the flattening. Correct.

### N4 — Crash safety: no reachable exception path
- `_mean_std_prev` (lines 85-93): called only when `len(self.closes) > 60`, so `n = 60` always; `mean > 0` for any positive price (guarded at line 92 anyway).
- `trend_gap_bps` (line 173): division by `ema_slow`, an EMA seeded at a real close (~$65k in price units) — never 0.
- `dip_bps` (line 164): division by `mean > 0`.
- `int(fill["qty"])` / `int(fill["price"])` (lines 109-110): a `KeyError`/`ValueError` is possible only on a malformed fill, which the schema-validated harness never sends. No defensive `.get` is strictly needed, though it would be cheap insurance.
- No recursion, no unbounded growth (`self.closes` holds ≤ 1,440 ints).

### N5 — Think time and memory are trivial
- Per-bar work is O(60) integer ops (mean/std over a 60-element window); 1,440 bars is milliseconds against the 1,800 s budget. Memory is a few KB. No timeout risk.

### N6 — Integer truncation effects are negligible
- EMA `//` truncation (lines 82-83) biases each EMA down by < 1 price unit per update (~1e-7 relative at ~$65k prices); TP/SL `//` (lines 140-141) shifts levels by < 1 unit; mean `//` (line 89) likewise. All deterministic and far below any decision threshold (60–150 bps). Non-issues.

### N7 — End-of-window flatten needs no bot action
- RULES: everything still held is sold at the last close with the same fee/slippage/impact and no `max_slippage_bps`. The bot's refusal to order on the last batch (line 129) is consistent with this; there is nothing to do in `on_done`.

### N8 — Restart / replay determinism
- The bot keeps no files and no cross-run state; a live-race restart replays the same batches through a fresh process, and the bot's replies are a pure function of the batch stream, so the reply-hash chain holds. No divergence risk from bot state.

### N9 — Sell min-notional subtlety (first of two same-reply sells)
- The engine exempts a sell from the $1 min-notional check only when it (with the reply's earlier accepted sells) *empties the position* (`sim/bars/engine.ts` `decide()`). When two lots exit on the same bar, the *first* sell does not empty the position, so the check applies to it. Each lot is worth ≈ $250 at the latest close, so this only bites after a >99.6% price collapse. Non-issue at any realistic price; noted for completeness.

### N10 — Sizing uses live account values
- `notional = state.equity * 2500 // 10000`, capped by `state.cash` (lines 181-183), both read from the current batch's account (post-fills). Sleeve 2's 25% is of equity *including* sleeve 1's marked value, which is the documented behavior. No stale-balance bug.

---

## D. Guardrail / ranking sanity (race window)

- **Drawdown:** max 50% invested with 35 bp stops ⇒ expected maxDD is a few percent at most (dev: 0.17%). The 25% guardrail is very unlikely to bind, even allowing for D5's worst-case realized stop (≈ 135 bps on half the book ≈ 0.7% equity per sleeve). The binding guardrail risk is the **trade count** (R4), not drawdown.
- **Trade count:** needs ≥ 1 filled order on the race window. Dev produced 10; a flat window is the only realistic way to fail this.
- **Fees at 2×:** the archive's "net PnL at 2× fees" would roughly double the ≈ $0.40/trade round-trip cost, turning the dev +$4.98 into approximately +$1 or less — another way of seeing how thin R1's margin is. (Estimate only; the exact 2×-fee figure depends on the per-trade split of fees vs. slippage/impact.)

---

## E. Ranked defect list (fix order, defects only)

1. **D1 — std-gate scaling (×100 error).** The filter the strategy claims is vacuous; the tuned parameters were validated against a different strategy than documented. Highest priority because it invalidates the tuning evidence, not just one trade.
2. **D2 — missing-bar blind spot.** Trade only on batches that actually carry a new CBBTC bar; still apply fills on gap batches.
3. **D3 — drop (or derive) the hardcoded `N_BARS` gate.** It protects against a harmless rejection and misfires if seq numbering ever shifts.
4. **D5 — align `MAX_SLIP_BPS` with the 35 bp stop** (or size the stop to the slippage it can actually realize). As coded, the stop's worst case is ~4× its nominal value.
5. **D4 — set the SL cooldown only on a filled stop-loss**, not on the decision that a stop level printed.
6. **D6 — add a late-window entry cutoff** (no new sleeve in the final minutes that cannot reach an exit).
7. **D7 — fix the docstring** (cooldown 15 → 5; ship or remove the `BUILD-REPORT.json` reference; `WARMUP_BARS` 30 vs. effective 60; "sized against the latest close").

## F. What was verified and what was not

- **Verified by reading:** `main.py` (all 199 lines), `vendor/makefun_bars.py` (all 389 lines), RULES.txt (protocol, caps, guardrails, sandbox sections), `contracts/bars-protocol.ts` and `sim/bars/engine.ts` (decision-check order, fill math, `max_slippage`/`below_min_notional`/`after_end`/`no_bar` semantics), and the dev dataset itself (1,440 bars, zero gaps, −281 bps, move distribution, depth ≈ $0.67–1.0M).
- **Not verified:** the claimed local replay (+$4.98, 10 trades, 0 rejections, byte-identical runs) was taken as given and not re-executed; the server-side harness block for this competition (exact `hello` caps/warm-up) is not in the kit, so D3's warm-up branch and R10 rest on RULES.txt, which never mentions a warm-up. No live-browser or submission-path checks were in scope.
