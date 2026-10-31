"""Run the lead-lag study (kalshi_moonshot leadlag) on the commodity 15-minute series against the cloud or local tape. Read-only.
Does the Kalshi quote trail the Hyperliquid feed it is priced from, and is the gap worth more than the spread and fees?
The feed prices are sparse (one tick every 6-18 s for the commodities), so a lag shorter than that cannot be seen.
    scripts/cloud.sh run scripts/leadlag_commodities.py [SERIES=KXGOLD15M,...]
"""
import argparse
import os

from kalshi_moonshot import config
from kalshi_moonshot.__main__ import cmd_leadlag

s = config.reload()
series = os.environ.get("SERIES", "KXGOLD15M,KXSILVER15M,KXPLATINUM15M,KXPALLADIUM15M,KXCOPPER15M,KXWTI15M,KXBRENT15M,KXNATGAS15M")
cmd_leadlag(s, argparse.Namespace(series=series))
print("DONE")
