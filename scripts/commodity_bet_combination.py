"""Do the readings the bot logged at the buy, TOGETHER, say which live commodity bets lose more than their price? (2026-11-19, user: "there's so many different variables in play and you keep isolating individual features like price or time when its a combination")
One question on the same bets (CLAUDE.md rule 4): the 826 live commodity bets since 11-09 exported by scripts/export_commodity_bets.py (data/commodity_bets_export.csv), held to settlement and as traded, buy fee on every bet, sale fee where the bet's own exit sold.
The model: a ridge logistic regression for the chance the bet WINS with the price as an offset (logit of the ask), so it learns only what the readings add to the price. Readings (all logged at the buy, none a market or coin name): ask, seconds left, elapsed minutes, spread, imbalance, imbalance times the side (+1 YES, -1 NO), move_90, range_90, log activity_60, rise_30, rise_90, dip_90, held_min, updates_60, the bot's vol tag, its cushion z, its spike z, the strike-band width, the model's chance for the side, the record weight and skill, the market type and the variety (one-hot, the commonest four of each). Missing values: the median plus a missing flag. The ridge strength is picked by cross-validation inside the training bets only.
The test is out of sample and in time order: train on the first 60% of the bets, test on the last 40% (also five expanding-window folds). Scores: log loss against the price alone, and what skipping the bets the model likes least would have done (the top 10, 20 and 30% by predicted shortfall of the win chance under the price) to the test bets' net dollars held and as traded, with the bets skipped and how many of them won. The same for a nearest-neighbour read (the 40 nearest earlier bets in the standardized readings). Then the full-data model's largest standardized weights, and how many of 200 bootstrap fits keep each sign (a weight that flips is noise). Settings (the 60/40 split, the folds, the skip shares, k) are chosen before the run.
Run on the Mac: .venv/bin/python scripts/commodity_bet_combination.py > studies/results/<date>-commodity-bet-combination.txt"""
import csv
import math

import numpy as np

lines = [x for x in open("data/commodity_bets_export.csv").read().splitlines() if x.strip()]
rows = [r for r in csv.DictReader(lines[[i for i, x in enumerate(lines) if x.startswith("ts,ticker")][0]:]) if r.get("ts", "").replace(".", "", 1).isdigit()]
rows.sort(key=lambda r: float(r["ts"]))
n = len(rows)


def num(r, k):
    v = r.get(k, "")
    try:
        return float(v) if v != "" else float("nan")
    except ValueError:
        return float("nan")


ask = np.array([num(r, "ask") for r in rows])
stake = np.array([num(r, "stake") for r in rows])
win = np.array([num(r, "result_win") for r in rows])
side = np.array([1.0 if r["side"] == "yes" else -1.0 for r in rows])
exit_p = np.array([num(r, "exit_price") for r in rows])
fee = lambda p: 0.07 * p * (1.0 - p)
held = (win - ask - fee(ask))
sold = ~np.isnan(exit_p)
trad = np.where(sold, exit_p - fee(np.nan_to_num(exit_p)), win) - ask - fee(ask)
dollars = lambda per_contract: per_contract * stake / ask  # contracts = stake / ask
D = lambda arr, idx: arr[idx] * stake[idx] / ask[idx]
names, cols = [], []


def add(name, vec):
    miss = np.isnan(vec)
    med = np.nanmedian(vec) if (~miss).any() else 0.0
    v = np.where(miss, med, vec)
    names.append(name); cols.append(v)
    if miss.any() and (~miss).any():
        names.append(name + "_missing"); cols.append(miss.astype(float))


add("ask", ask)
add("left", np.array([num(r, "left") for r in rows]))
for k in ("elapsed_min", "spread", "imbalance", "move_90", "range_90", "rise_30", "rise_90", "dip_90", "held_min", "updates_60", "vol", "z", "spike_z", "width_pct", "model_p_side", "rec_weight", "rec_skill"):
    add(k, np.array([num(r, k) for r in rows]))
add("imbalance_x_side", np.array([num(r, "imbalance") for r in rows]) * side)
add("log_activity_60", np.log1p(np.array([num(r, "activity_60") for r in rows])))
for col in ("mtype", "variety"):
    vals = [r[col] for r in rows]
    for v in sorted(set(vals) - {""}, key=lambda x: -vals.count(x))[:4]:
        add(f"{col}={v}", np.array([1.0 if r[col] == v else 0.0 for r in rows]))
X = np.column_stack(cols)
logit = lambda p: np.log(np.clip(p, 1e-4, 1 - 1e-4) / (1 - np.clip(p, 1e-4, 1 - 1e-4)))
off = logit(ask)


def fit(Xt, yt, ot, lam, iters=50):
    mu, sd = Xt.mean(0), Xt.std(0) + 1e-9
    Z = (Xt - mu) / sd
    Z = np.column_stack([np.ones(len(Z)), Z])
    b = np.zeros(Z.shape[1])
    pen = np.eye(Z.shape[1]) * lam
    pen[0, 0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(Z @ b + ot)))
        W = p * (1 - p) + 1e-9
        g = Z.T @ (yt - p) - pen @ b
        H = Z.T @ (Z * W[:, None]) + pen
        step = np.linalg.solve(H, g)
        b = b + step
        if np.max(np.abs(step)) < 1e-6:
            break
    return b, mu, sd


def predict(m, Xs, os_):
    b, mu, sd = m
    Z = np.column_stack([np.ones(len(Xs)), (Xs - mu) / sd])
    return 1 / (1 + np.exp(-(Z @ b + os_)))


def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def pick_lambda(Xt, yt, ot):
    best, bl = None, 1e9
    k = len(yt) // 4
    for lam in (3, 10, 30, 100, 300, 1000):
        ls = []
        for f in range(1, 4):  # expanding folds inside the training bets
            tr, te = slice(0, k * f), slice(k * f, k * (f + 1))
            m = fit(Xt[tr], yt[tr], ot[tr], lam)
            ls.append(logloss(predict(m, Xt[te], ot[te]), yt[te]))
        if np.mean(ls) < bl:
            best, bl = lam, float(np.mean(ls))
    return best


def skip_report(label, test_idx, risk, shares=(0.1, 0.2, 0.3)):
    print(f"  {label}")
    allnet_h, allnet_t = D(held, test_idx).sum(), D(trad, test_idx).sum()
    stake_all = stake[test_idx].sum()
    print(f"    all {len(test_idx)} test bets: held {allnet_h:+.2f}, as traded {allnet_t:+.2f} ({100 * allnet_t / stake_all:+.1f} per $100 traded)")
    order = test_idx[np.argsort(-risk)]
    for sh in shares:
        k = int(round(sh * len(test_idx)))
        out, keep = order[:k], order[k:]
        print(f"    skip top {int(sh * 100)}% ({k} bets, {int(win[out].sum())} of them won): skipped bets' held {D(held, out).sum():+.2f} / traded {D(trad, out).sum():+.2f}; kept bets' held {D(held, keep).sum():+.2f} / traded {D(trad, keep).sum():+.2f} ({100 * D(trad, keep).sum() / stake[keep].sum():+.1f} per $100 traded)")


print(f"{n} live commodity bets since 11-09, {X.shape[1]} readings; won {int(win.sum())}, held net {dollars(held).sum():+.2f}, as traded {dollars(trad).sum():+.2f}")
cut = int(0.6 * n)
tr, te = np.arange(cut), np.arange(cut, n)
lam = pick_lambda(X[tr], win[tr], off[tr])
m = fit(X[tr], win[tr], off[tr], lam)
p_model = predict(m, X[te], off[te])
print(f"=== train first {cut} bets, test last {n - cut} (ridge lambda {lam} chosen inside the training bets)")
print(f"  log loss on the test bets: price alone {logloss(ask[te], win[te]):.4f}, model with price offset {logloss(p_model, win[te]):.4f} (lower is better; the difference is what the readings add out of sample)")
skip_report("the ridge model", te, ask[te] - p_model)
# nearest neighbour read
mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
Zs = (X - mu) / sd
k = 40
shortfall = np.zeros(len(te))
for i, t in enumerate(te):
    d = np.sqrt(((Zs[tr] - Zs[t]) ** 2).sum(1))
    nn = tr[np.argsort(d)[:k]]
    shortfall[i] = (ask[nn] - win[nn]).mean()  # the neighbours' average shortfall: price less outcome
print(f"  nearest neighbours (k {k}): log loss not defined; skipping by the neighbours' average shortfall")
skip_report("nearest neighbours", te, shortfall)
print("=== five expanding-window folds (test fold 1 of 5 each, trained on everything before it)")
folds = [(int(n * a), int(n * (a + 0.1))) for a in (0.5, 0.6, 0.7, 0.8, 0.9)]
tot = {s: [0.0, 0.0] for s in (0.2,)}
for a, b_ in folds:
    trn, tst = np.arange(a), np.arange(a, b_)
    lam_f = pick_lambda(X[trn], win[trn], off[trn])
    mf = fit(X[trn], win[trn], off[trn], lam_f)
    pm = predict(mf, X[tst], off[tst])
    order = tst[np.argsort(-(ask[tst] - pm))]
    k_ = int(round(0.2 * len(tst)))
    out, keep = order[:k_], order[k_:]
    print(f"  fold {a}-{b_}: log loss price {logloss(ask[tst], win[tst]):.4f} model {logloss(pm, win[tst]):.4f}; skipping the top 20% ({k_} bets) changes the as-traded net by {-D(trad, out).sum():+.2f} ({int(win[out].sum())} of the skipped won)")
    tot[0.2][0] += -D(trad, out).sum()
print(f"  sum over the five folds of 'as-traded net change from skipping the top 20%': {tot[0.2][0]:+.2f}")
print("=== the full-data model: standardized weights and sign stability over 200 bootstrap fits")
lam_full = pick_lambda(X, win, off)
mfull = fit(X, win, off, lam_full)
rng = np.random.default_rng(7)
signs = np.zeros(X.shape[1])
for _ in range(200):
    ix = rng.integers(0, n, n)
    mb = fit(X[ix], win[ix], off[ix], lam_full)
    signs += (np.sign(mb[0][1:]) == np.sign(mfull[0][1:]))
order = np.argsort(-np.abs(mfull[0][1:]))
print(f"  ridge lambda {lam_full}; weight > 0 raises the chance of winning beyond what the price says")
for i in order[:14]:
    print(f"    {names[i]:24s} {mfull[0][1 + i]:+.3f}   sign kept in {int(signs[i])} of 200")
print("=== what the skipped 20% look like (the 60/40 test bets the model likes least, against the other 80%)")
risk = ask[te] - p_model
order = te[np.argsort(-risk)]
k20 = int(round(0.2 * len(te)))
out, keep = order[:k20], order[k20:]
col = {nm: cols[i] for i, nm in enumerate(names)}
for nm in ("ask", "left", "dip_90", "imbalance", "vol", "log_activity_60", "held_min", "rise_30", "range_90", "model_p_side"):
    if nm in col:
        print(f"  {nm:16s} skipped {np.mean(col[nm][out]):9.3f} | kept {np.mean(col[nm][keep]):9.3f}")
print(f"  share of the skipped bets bought at 0.88 or more: {np.mean(ask[out] >= 0.88):.0%}; of the kept: {np.mean(ask[keep] >= 0.88):.0%}")
print("DONE")
