"""All-coin curves around the lane's worst windows. (2026-11-19, user: "pull them")

Draws, for chosen windows, the YES mid of every coin over the window before and the window the lane bought in (Kalshi's own quotes from moonshot.risk_readings, 15 s means), with each of the lane's bets as a dot (filled = YES, open = NO). Inputs are two text dumps of the host's tables (cloud.sh sql output) at /tmp/curves.txt and /tmp/post.txt; the output is an SVG. Nothing is scored here: the picture is for looking at the shape of a wave before a rule is written. Series names only label lines.
Run: python3 scripts/crypto_lane_wave_chart.py > studies/results/<date>-lane-wave-moments.svg"""
import collections
import datetime
import sys

cur = collections.defaultdict(list)
for l in open("/tmp/curves.txt"):
    p = [x.strip() for x in l.split("|")]
    if len(p) == 4 and p[0].startswith("KX"):
        cur[(p[1], p[0])].append((int(p[2]), float(p[3])))
bets = collections.defaultdict(list)
for l in open("/tmp/post.txt"):
    p = [x.strip() for x in l.split("|")]
    if len(p) >= 8 and p[1].startswith("KX"):
        t = p[1].split("-")[1]
        hh, mm, ss = map(int, p[5].split()[1].split(":"))
        d = int(p[5].split()[0])
        ts = datetime.datetime(2026, 11, d, hh, mm, ss, tzinfo=datetime.timezone.utc).timestamp() + 5 * 3600  # ET = UTC-5
        bets[t].append((p[0], ts, p[2], p[6]))


def closets(c):
    return datetime.datetime(2026, 11, int(c[5:7]), int(c[7:9]), int(c[9:11]), tzinfo=datetime.timezone.utc).timestamp() + 5 * 3600


def prev(c):
    dt = datetime.datetime.fromtimestamp(closets(c) - 900 - 5 * 3600, datetime.timezone.utc)
    return f"26NOV{dt.day:02d}{dt.hour:02d}{dt.minute:02d}"


picks = [("26NOV181900", "close 19:00 ET Nov 18: YES bets lost"), ("26NOV181930", "close 19:30: NO bets"), ("26NOV182100", "close 21:00: NO bets lost"), ("26NOV190700", "close 07:00 Nov 19: YES bets lost")]
colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
W, H = 620, 300
pad = (44, 24, 12, 30)
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W * 2}" height="{H * 2 + 60}" font-family="sans-serif" font-size="11">', '<rect width="100%" height="100%" fill="white"/>']
series = sorted({s for (_, s) in cur})
for i, (c, title) in enumerate(picks):
    ox, oy = (i % 2) * W, (i // 2) * H + 40
    t1 = closets(c)
    t0 = t1 - 1800

    def X(t):
        return ox + pad[0] + (t - t0) / (t1 - t0) * (W - pad[0] - 20)

    def Y(v):
        return oy + pad[1] + (1 - v) * (H - pad[1] - pad[3])

    svg.append(f'<text x="{ox + pad[0]}" y="{oy + 12}" font-weight="bold">{title}</text>')
    for v in (0, 0.25, 0.5, 0.75, 1):
        svg.append(f'<line x1="{X(t0)}" x2="{X(t1)}" y1="{Y(v)}" y2="{Y(v)}" stroke="#ddd"/><text x="{ox + 4}" y="{Y(v) + 4}" fill="#666">{v:.2f}</text>')
    svg.append(f'<line x1="{X(t1 - 900)}" x2="{X(t1 - 900)}" y1="{Y(1)}" y2="{Y(0)}" stroke="#999" stroke-dasharray="4"/>')
    for mn in (0, 5, 10, 15, 20, 25, 30):
        t = t0 + mn * 60
        dt = datetime.datetime.fromtimestamp(t - 5 * 3600, datetime.timezone.utc)
        svg.append(f'<text x="{X(t) - 14}" y="{oy + H - 8}" fill="#666">{dt.hour:02d}:{dt.minute:02d}</text>')
    for j, s in enumerate(series):
        pts = [(b, m) for cc in (prev(c), c) for (b, m) in cur.get((cc, s), []) if t0 <= b <= t1]
        if pts:
            svg.append(f'<polyline fill="none" stroke="{colors[j % 10]}" stroke-width="1.4" points="' + " ".join(f"{X(b):.1f},{Y(m):.1f}" for b, m in pts) + '"/>')
    for s, ts, side, out in bets[c]:
        pts = [(b, m) for (b, m) in cur.get((c, s), []) if abs(b - ts) <= 20]
        if pts and s in series:
            j = series.index(s)
            svg.append(f'<circle cx="{X(ts):.1f}" cy="{Y(pts[0][1]):.1f}" r="4" fill="{"#222" if side == "yes" else "white"}" stroke="{colors[j % 10]}" stroke-width="2"/>')
svg.append('<text x="8" y="16" font-weight="bold" font-size="14">YES mid of every coin: the window before, then the window the lane bought in (dashed = its open). Filled dot = YES bet, open dot = NO bet, where bought</text>')
svg.append("</svg>")
sys.stdout.write("\n".join(svg))
