"""WHAT EACH LANE DOES, IN PLAIN WORDS: reads the lane file and the env file as the bot does and prints, for every enabled lane, what it buys, how it buys, and every way out, from the settings that are switched on. Nothing is stored: it cannot drift from the config.
    .venv/bin/python scripts/lanes_summary.py [env_file=.env] [lane ...]
"""
import sys
from pathlib import Path

from kalshi_moonshot import config, lanes

args = sys.argv[1:]
env_file = Path(args.pop(0)) if args and args[0].endswith(".env") or args and args[0].startswith(".env") else Path(".env")
s = config.reload(env_file)
specs = lanes.lane_specs(s)
want = set(args)


def money(x):
    return f"${x:g}"


def describe(sp):
    live = "LIVE (real orders)" if s.mode == "trade" and (s.live_lanes is None or sp.name in s.live_lanes) else "paper (no orders)"
    out = [f"{sp.name} - {sp.label or ''}   [{live}]"]
    mk = ", ".join(sp.series) if sp.series else "any series"
    out.append(f"  BUYS     {'/'.join(sp.sides).upper()} on {mk}" + (f"; one per {sp.one_per}" if sp.one_per != "none" else ""))
    out.append(f"           when the ask is {sp.price[0]:.2f}-{sp.price[1]:.2f}, spread at most {sp.max_spread:.2f}" + (f", {sp.entry_elapsed[0]:g}-{sp.entry_elapsed[1]:g} min after the window opens" if sp.entry_elapsed != (0.0, 1e9) else f", {sp.hours[0]:g}-{sp.hours[1]:g} h to close"))
    if sp.entry_trend_seconds:
        out.append(f"           and only if its own price has been flat or rising over the last {sp.entry_trend_seconds:g} s")
    if sp.blackout_et:
        out.append(f"           never during {', '.join(sp.blackout_et)} (New York)")
    out.append(f"  SIZE     {money(sp.fixed_stake) if sp.fixed_stake else 'sized by the odds and balance'} a bet" + (f", at most {sp.max_open} open at once" if sp.max_open else ""))
    if sp.buy_rest_seconds > 0:
        how = f"rests a post-only buy {sp.buy_rest_below:g} under the ask for {sp.buy_rest_seconds:g} s (no fee), nothing if it does not fill"
    elif sp.entry_approach > 0:
        how = f"rests a bid at the price floor while the side climbs to it, closed after {sp.entry_arm_seconds:g} s"
    else:
        how = f"immediate order at the ask + {sp.buy_slip * 100:g}c"
        if sp.buy_retries:
            how += f"; a buy that fills nothing is re-read and resent up to {sp.buy_retries} time(s), paying up to {sp.buy_retry_chase * 100:g}c over the decided ask"
    out.append(f"  ORDER    {how}")
    exits = []
    if sp.stop_loss:
        exits.append(f"stop-loss {sp.stop_loss_at * 100:g}% under the price paid" if sp.stop_loss_at else "stop-loss at the global drop")
    else:
        exits.append("NO stop-loss (held to settlement unless something below sells)")
    if sp.watch_loss:
        exits.append(f"hard stop: {sp.watch_loss * 100:g}% under the price paid, after {sp.watch_loss_seconds:g} s")
    if sp.take_profit_x or sp.take_profit_at:
        exits.append("take profit at " + (f"{sp.take_profit_x:g}x the price paid" if sp.take_profit_x else f"a bid of {sp.take_profit_at:g}"))
    if sp.trail_drop:
        exits.append(f"trailing exit: {sp.trail_drop * 100:g}c off the peak")
    if sp.scalp_at:
        exits.append(f"scalp: follow the high once the bid prints {sp.scalp_at:g}")
    if sp.settlement_flip and sp.burst_ratio:
        exits.append(f"BURST (last {sp.burst_window:g} s): sell when the dollars bought against the holder in 3 s are {sp.burst_ratio:g}x the market's normal, at least ${sp.burst_min_dollars:g}, {sp.burst_share * 100:g}%+ of the flow, and the bid is at least {sp.burst_favorite:g}")
    elif sp.settlement_flip and sp.burst_dollars:
        exits.append(f"BURST (last {sp.burst_window:g} s): sell when ${sp.burst_dollars:g} is bought against the holder in 3 s and the bid is at least {sp.burst_favorite:g}")
    elif sp.settlement_flip:
        exits.append(f"BURST (last {sp.burst_window:g} s): sell on a 5x volume burst against the holder (older fixed-band rule)")
    if sp.drop_cents:
        wait = f", and only if the bid is still {(sp.drop_stay or sp.drop_cents) * 100:g}c under the high {sp.drop_confirm:g} s later" if sp.drop_confirm else ""
        exits.append(f"DROP (last {sp.drop_window:g} s): a bid fall of {sp.drop_cents * 100:g}c within {sp.drop_seconds:g} s{wait}")
    if sp.cash_left_seconds:
        exits.append(f"last {sp.cash_left_seconds:g} s: sell a bid that hovers under the price paid")
    out.append("  EXITS    " + ("\n           ".join(exits) if exits else "none: held to settlement"))
    hedge = []
    if sp.settlement_flip and (sp.burst_buy or sp.burst_buy_pct):
        hedge.append(f"after a burst sale, buys the other side with {sp.burst_buy_pct * 100:g}% of the stake" if sp.burst_buy_pct else f"after a burst sale, buys the other side with {money(sp.burst_buy)}")
    if sp.drop_cents and sp.drop_buy:
        hedge.append(f"when a fall is found (before the sale), buys the other side if it costs {lanes_band(sp)}")
    if hedge:
        out.append("  HEDGE    " + "\n           ".join(hedge))
    if sp.burst_hunt:
        out.append("  HUNTS    with no position, buys the cheap side of a market in the last 120 s when its own taker flow bursts")
    return "\n".join(out)


def lanes_band(sp):
    return f"5-{sp.drop_buy_max * 100:g}c"


for name, sp in specs.items():
    if (want and name not in want) or (not want and not sp.enabled):
        continue
    print(describe(sp) + "\n")
