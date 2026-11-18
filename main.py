"""BTC race bot, variant A: mean-reversion dip-buy, small sleeves.

Strategy (tuned on the dev window; see BUILD-REPORT.json):
- Warm up on the first 60 bars: build the 60-bar rolling mean/std and the
  12/48 EMAs, trade nothing.
- Sleeve 1 (25% of equity) when the close dips DIP1_BPS below the 60-bar
  mean and the medium trend is not strongly down (EMA12 >= EMA48).
- Sleeve 2 (another 25%) only on a deeper dip (DIP2_BPS) while sleeve 1
  is still held. Never more than 50% invested.
- Each sleeve exits at TP_BPS above its fill, SL_BPS below its fill, or
  when the close recovers to the rolling mean. 15-bar cooldown after any
  stop-loss before a new buy.
- Orders are sized against the latest close; max_slippage_bps caps the
  fill move from that close (2 bps modelled slippage + impact included).

Determinism: integer math only, no clock, no environment, no randomness.
The bot speaks agent-fun/bars-replay/v1 through the vendored SDK shim.
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- strategy parameters (grid-tuned on the dev window) ---
WARMUP_BARS = 30
MEAN_WIN = 60
EMA_FAST = 12
EMA_SLOW = 48
DIP1_BPS = 60
DIP2_BPS = 120
TP_BPS = 150
SL_BPS = 35
SLEEVE_BPS = 2500
STD_MULT_BPS = 100
TREND_TOL_BPS = -100
MAX_SLIP_BPS = 100
COOLDOWN_BARS = 5
N_BARS = 1440


def isqrt(n):
    return math.isqrt(n)


class Lot:
    """One bought sleeve: its filled quantity, fill price and exit state."""

    __slots__ = ("lot_id", "qty", "price", "exiting")

    def __init__(self, lot_id, qty, price):
        self.lot_id = lot_id
        self.qty = qty
        self.price = price
        self.exiting = False


class Bot:
    def __init__(self):
        self.closes = []          # closes seen, oldest first
        self.ema_fast = None      # integer EMAs, seeded at the first close
        self.ema_slow = None
        self.lots = {}            # lot_id -> Lot
        self.pending_sells = {}   # sell client_id -> lot_id
        self.last_sl_seq = -10 ** 9
        self.counter = 0

    def _next_id(self, prefix):
        self.counter += 1
        return "%s%d" % (prefix, self.counter)

    def _ema_update(self, close):
        if self.ema_fast is None:
            self.ema_fast = close
            self.ema_slow = close
            return
        # ema = (2*close + (n-1)*ema) / (n+1), integer-truncated, deterministic
        self.ema_fast = (2 * close + (EMA_FAST - 1) * self.ema_fast) // (EMA_FAST + 1)
        self.ema_slow = (2 * close + (EMA_SLOW - 1) * self.ema_slow) // (EMA_SLOW + 1)

    def _mean_std_prev(self):
        """Mean and std (bps of the mean) of the 60 closes before the current bar."""
        window = self.closes[-MEAN_WIN - 1:-1]
        n = len(window)
        mean = sum(window) // n
        var = sum((c - mean) * (c - mean) for c in window) // n
        std = isqrt(var)
        std_bps = std * 10000 // mean if mean > 0 else 0
        return mean, std_bps

    def _apply_fills(self, batch):
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL:
                continue
            cid = fill.get("client_id", "")
            status = fill.get("status")
            if status != "filled":
                # A rejected sell frees its lot to exit again later.
                if cid in self.pending_sells:
                    lot = self.lots.get(self.pending_sells.pop(cid))
                    if lot is not None:
                        lot.exiting = False
                continue
            if fill.get("side") == "buy":
                qty = int(fill["qty"])
                price = int(fill["price"])
                if qty > 0:
                    self.lots[cid] = Lot(cid, qty, price)
            elif fill.get("side") == "sell":
                lot_id = self.pending_sells.pop(cid, None)
                if lot_id is not None:
                    self.lots.pop(lot_id, None)

    def on_batch(self, state, batch):
        self._apply_fills(batch)
        bar = state.latest.get(SYMBOL)
        if bar is None:
            return []
        close = state.close(SYMBOL)
        if close is None:
            return []
        self.closes.append(close)
        self._ema_update(close)
        seq = state.seq
        if state.warming_up or seq < WARMUP_BARS or seq >= N_BARS - 1:
            return []
        if len(self.closes) <= MEAN_WIN:
            return []
        mean, std_bps = self._mean_std_prev()
        orders = []
        sold_this_bar = False
        # --- exits: one sell order per lot that hit TP, SL or mean recovery ---
        for lot in list(self.lots.values()):
            if lot.exiting:
                continue
            tp_price = lot.price * (10000 + TP_BPS) // 10000
            sl_price = lot.price * (10000 - SL_BPS) // 10000
            if close <= sl_price:
                self.last_sl_seq = seq
                exit_now = True
            elif close >= tp_price or close >= mean:
                exit_now = True
            else:
                exit_now = False
            if exit_now:
                cid = self._next_id("x")
                self.pending_sells[cid] = lot.lot_id
                lot.exiting = True
                sold_this_bar = True
                orders.append({"side": "sell", "symbol": SYMBOL, "qty": lot.qty,
                               "max_slippage_bps": MAX_SLIP_BPS, "client_id": cid})
        if sold_this_bar:
            return orders
        # --- entries: at most two sleeves, 25% of equity each ---
        if seq - self.last_sl_seq < COOLDOWN_BARS:
            return orders
        held = sum(1 for lot in self.lots.values() if not lot.exiting)
        if held >= 2:
            return orders
        dip_bps = (mean - close) * 10000 // mean if close < mean else 0
        if dip_bps < DIP1_BPS:
            return orders
        # The dip must also clear STD_MULT_BPS (in bps, 100 = 1.0x) of the
        # 60-bar close std, so a dip is significant against local noise.
        if dip_bps < STD_MULT_BPS * std_bps // 10000:
            return orders
        # "Not strongly down": the fast EMA may sit up to TREND_TOL_BPS
        # below the slow EMA; a deeper gap is a strong downtrend.
        trend_gap_bps = (self.ema_fast - self.ema_slow) * 10000 // self.ema_slow
        trend_up = trend_gap_bps >= TREND_TOL_BPS
        if held == 0:
            if not trend_up:
                return orders
        elif held == 1:
            if dip_bps < DIP2_BPS:
                return orders
        notional = state.equity * SLEEVE_BPS // 10000
        if notional > state.cash:
            notional = state.cash
        if notional < 1000000:
            return orders
        cid = self._next_id("b")
        orders.append({"side": "buy", "symbol": SYMBOL, "notional_quote_units": notional,
                       "max_slippage_bps": MAX_SLIP_BPS, "client_id": cid})
        return orders


BOT = Bot()


def on_batch(state, batch):
    return BOT.on_batch(state, batch)


makefun_bars.run(on_batch)
