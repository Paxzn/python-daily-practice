"""
Paxton Ecom OS — Replenishment Floor Calculator (FBA + FBM)
===========================================================

Why this file exists
--------------------
The Ecom OS workbook (INBOX -> RESEARCH -> ECONOMICS -> RISK -> DECISION ->
INVENTORY -> DASHBOARD) was built for *retail arbitrage*: find a deal, buy a
few units, flip them, done. It has no concept of a replenishment "floor" or a
case/box quantity. The moment you start buying wholesale in boxes of 20-40 and
restocking the same SKU over and over, you need two new numbers per SKU that
the workbook does not yet compute:

  1. REORDER POINT  -> the *floor*. When live + inbound units drop to this,
                       you send the next shipment. (A trigger, measured in
                       units.)
  2. REPLENISH QTY  -> how many to send. For FBA this is the box fill
                       (clamped 20-40). For FBM there is no box; you ship
                       singles, so the "qty" is just how deep you restock.

The key correction this file makes to the old mental model:
a box of "20-35 (maybe 40)" is NOT one fixed floor. The right box fill is
velocity-tuned and bounded on BOTH sides:

    Q_box = clamp( velocity * target_days_cover , ship_breakeven , box_cap )

  * Lower bound (ship_breakeven): a box so small that inbound shipping + prep
    labor eats your margin. This is the real "floor" on box size.
  * Upper bound (box_cap and storage-safe days): a box so big it sits in FBA
    long enough to rack up storage / aged-inventory surcharges and ties up
    cash you could be recycling.

FBA and FBM are different games and get different math:
  * FBA  -> Amazon charges a per-unit fulfillment fee + monthly storage. You
            ship in boxes. Inbound shipping + prep is a per-SHIPMENT cost, so
            tiny boxes are inefficient. Cash is tied up in transit + storage.
  * FBM  -> No Amazon fulfillment fee, no FBA storage, but you pay shipping +
            labor on EVERY single order. No box. The "floor" is pure safety
            stock so you never stock out between reorders.

"Be sure you know the condition of your flocks, give careful attention to your
herds" (Proverbs 27:23). This is that, for inventory.
"""

from dataclasses import dataclass, field
from math import ceil, sqrt


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class SKU:
    name: str
    channel: str                 # "FBA" or "FBM"
    sale_price: float            # list price you sell at
    all_in_cost: float           # landed cost per unit (cost + ship-in + prep)
    velocity_per_day: float      # units sold per day (Est. Monthly Sales / 30)

    # Amazon economics
    referral_pct: float = 0.15   # Amazon referral fee, ~15% of sale price
    fba_fee_per_unit: float = 0.0    # FBA pick/pack fulfillment fee (FBA only)
    fba_storage_per_unit_mo: float = 0.0  # monthly FBA storage per unit (FBA)
    fbm_ship_per_order: float = 0.0       # outbound shipping you pay (FBM only)
    fbm_labor_per_order: float = 0.0      # pack/handling labor per order (FBM)

    # Replenishment knobs
    lead_time_days: float = 14.0     # source -> live (FBA: incl. inbound + check-in)
    shipment_fixed_cost: float = 10.0  # box + inbound freight + prep labor PER box (FBA)
    box_cap: int = 40                  # physical max units per box
    max_overhead_fraction: float = 0.10  # cap inbound overhead at 10% of unit profit
    storage_safe_days: float = 60.0    # don't let a box sit longer than this in FBA
    demand_std_per_day: float = 0.0    # daily demand std-dev for safety stock; 0 -> simple buffer
    service_z: float = 1.65            # 95% service level
    simple_buffer_days: float = 7.0    # fallback safety buffer if no std-dev given


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------

def net_profit_per_unit(s: SKU) -> float:
    """Margin AFTER the channel's fulfillment costs. This is what funds growth."""
    referral = s.sale_price * s.referral_pct
    if s.channel.upper() == "FBA":
        fulfil = s.fba_fee_per_unit + s.fba_storage_per_unit_mo  # ~1 mo storage charged to the unit
    else:  # FBM
        fulfil = s.fbm_ship_per_order + s.fbm_labor_per_order
    return s.sale_price - s.all_in_cost - referral - fulfil


def safety_stock(s: SKU) -> float:
    if s.demand_std_per_day > 0:
        return s.service_z * s.demand_std_per_day * sqrt(s.lead_time_days)
    return s.velocity_per_day * s.simple_buffer_days


def reorder_point(s: SKU) -> float:
    """The FLOOR. When (live + inbound) units hit this, send the next shipment."""
    return s.velocity_per_day * s.lead_time_days + safety_stock(s)


def ship_breakeven_qty(s: SKU) -> int:
    """Smallest box that keeps inbound overhead under max_overhead_fraction of unit profit."""
    npu = net_profit_per_unit(s)
    if npu <= 0:
        return s.box_cap  # unprofitable: don't optimize box size, fix the buy first
    return max(1, ceil(s.shipment_fixed_cost / (s.max_overhead_fraction * npu)))


def fba_box_fill(s: SKU) -> int:
    """Velocity-tuned box quantity, bounded by ship-breakeven (low) and storage/cap (high).

    Returns the box cap when FBA is structurally wrong for the SKU (the smallest
    economic box would sit far longer than storage-safe days). The warning text
    flags this; the right move is FBM or PASS, not a literal box of this size.
    """
    target = s.velocity_per_day * s.storage_safe_days
    low = ship_breakeven_qty(s)
    high = min(s.box_cap, ceil(target))
    if high < low:
        # Margin too thin and/or velocity too slow: no FBA box makes sense.
        return min(low, s.box_cap)
    return int(min(max(target, low), high))


def is_fba_viable(s: SKU) -> bool:
    """FBA only makes sense if an economic box (>= ship-breakeven) clears in
    under storage_safe_days. Otherwise the SKU belongs on FBM or PASS."""
    breakeven_days = ship_breakeven_qty(s) / s.velocity_per_day if s.velocity_per_day else float("inf")
    return net_profit_per_unit(s) > 0 and breakeven_days <= s.storage_safe_days


def analyze(s: SKU) -> dict:
    npu = net_profit_per_unit(s)
    rop = reorder_point(s)
    out = {
        "sku": s.name,
        "channel": s.channel.upper(),
        "net_profit_per_unit": round(npu, 2),
        "roi_pct": round(100 * npu / s.all_in_cost, 1) if s.all_in_cost else None,
        "reorder_point_units": ceil(rop),
        "safety_stock_units": ceil(safety_stock(s)),
    }
    if s.channel.upper() == "FBA":
        q = fba_box_fill(s)
        days_cover = q / s.velocity_per_day if s.velocity_per_day else float("inf")
        out.update({
            "ship_breakeven_qty": ship_breakeven_qty(s),
            "recommended_box_fill": q,
            "days_of_cover_per_box": round(days_cover, 1),
            "reship_every_days": round(days_cover, 1),
            "inbound_overhead_per_unit": round(s.shipment_fixed_cost / q, 2),
            "warning": _fba_warning(s, q, days_cover),
        })
    else:
        out.update({
            "ship_qty": "1 (individual / merchant-fulfilled)",
            "restock_depth_units": ceil(rop + s.velocity_per_day * 14),  # ~2wk above floor
            "note": "No box. Floor is safety stock so you never stock out between buys.",
        })
    return out


def _fba_warning(s: SKU, q: int, days_cover: float) -> str:
    msgs = []
    if net_profit_per_unit(s) <= 0:
        msgs.append("UNPROFITABLE at current price/cost — fix the buy before shipping.")
    if not is_fba_viable(s):
        msgs.append("DO NOT FBA — margin/velocity can't justify a box that clears in time. "
                    "Route to FBM or PASS.")
        return " ".join(msgs)
    if days_cover > 90:
        msgs.append("Box holds >90 days of stock — long-term storage / aged-surcharge risk; "
                    "ship a smaller box more often.")
    if q <= ship_breakeven_qty(s) and days_cover < s.storage_safe_days:
        msgs.append("Pinned to ship-breakeven floor — slow velocity; consider FBM for this SKU.")
    if q >= s.box_cap and days_cover < 21:
        msgs.append("Fast mover filling the box but <3wk cover — consider a bigger case or 2 boxes.")
    return " ".join(msgs) if msgs else "OK"


# ---------------------------------------------------------------------------
# Demo using a few real INBOX SKUs (velocities/prices are ILLUSTRATIVE
# assumptions — replace with your RESEARCH-tab numbers).
# ---------------------------------------------------------------------------

def _demo():
    skus = [
        # Fast mover, healthy margin -> should fill toward a full box.
        SKU(name="Call of Duty: Black Ops 7 (PS5)", channel="FBA",
            sale_price=69.99, all_in_cost=31.90, velocity_per_day=1.2,
            fba_fee_per_unit=4.95, fba_storage_per_unit_mo=0.45,
            shipment_fixed_cost=11.0, box_cap=40, lead_time_days=14),

        # Slow mover, thin margin -> small box or move to FBM.
        SKU(name="Moulin Rouge (Blu-ray)", channel="FBA",
            sale_price=12.99, all_in_cost=5.89, velocity_per_day=0.15,
            fba_fee_per_unit=3.55, fba_storage_per_unit_mo=0.40,
            shipment_fixed_cost=11.0, box_cap=40, lead_time_days=14),

        # Mid mover via FBM (no box, individual ship).
        SKU(name="Watts Pressure Reducing Valve 3/4", channel="FBM",
            sale_price=89.99, all_in_cost=51.89, velocity_per_day=0.3,
            fbm_ship_per_order=8.50, fbm_labor_per_order=1.50, lead_time_days=10),
    ]

    print("=" * 74)
    print("PAXTON ECOM OS — REPLENISHMENT FLOORS  (illustrative inputs)")
    print("=" * 74)
    for s in skus:
        r = analyze(s)
        print(f"\n{r['sku']}  [{r['channel']}]")
        for k, v in r.items():
            if k in ("sku", "channel"):
                continue
            print(f"   {k:>28}: {v}")


if __name__ == "__main__":
    _demo()
