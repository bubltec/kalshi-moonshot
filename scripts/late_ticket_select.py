"""Fit a simple win-probability model on the first 60% of late-ticket entries, test its picks on the last 40%.
Needs the dump from:  python scripts/late_ticket_study.py moonshot.db 0.01 0.40 DIR/late_rows.json
Then:  python scripts/late_ticket_select.py DIR     (pure Python, no libraries)"""
import json, math, random, sys
S=sys.argv[1]
rows=json.load(open(S+"/late_rows.json"))
fam=lambda r: "crypto" if any(k in r["series"] for k in ("BTC","ETH","SOL","XRP","DOGE")) else "energy" if any(k in r["series"] for k in ("WTI","NATGAS")) else "metals"
def feats(r):
    m=max(min(r["model"],0.999),0.001); lg=math.log(m/(1-m)); pr=max(r["price"],0.005); lp=math.log(pr/(1-pr))
    return [1.0, lp, lg-lp, max(min(r["need"],4),-4), max(min(r["toward"],4),-4), r["left"]-2, min(r["spread"],0.1)*10,
            1.0*(fam(r)=="crypto"), 1.0*(fam(r)=="energy")]
def fee(p): return 0.07*p*(1-p)
def fit(X,y,l2=1.0,it=400,lr=0.05):
    w=[0.0]*len(X[0])
    for _ in range(it):
        g=[l2*wi*0.01 for wi in w]
        for x,t in zip(X,y):
            z=sum(a*b for a,b in zip(w,x)); p=1/(1+math.exp(-max(min(z,30),-30)))
            for j in range(len(w)): g[j]+=(p-t)*x[j]/len(X)
        w=[wi-lr*gj*10 for wi,gj in zip(w,g)]
    return w
def pred(w,x): return 1/(1+math.exp(-max(min(sum(a*b for a,b in zip(w,x)),30),-30)))
def roi(g):
    c=sum(r["price"]+fee(r["price"]) for r in g); return (sum(r["won"] for r in g)/c-1) if c else float("nan")
n=len(rows); cut=int(n*0.6)
tr,te=rows[:cut],rows[cut:]
w=fit([feats(r) for r in tr],[1.0*r["won"] for r in tr])
print("coef (const, logit price, model-minus-price, need, toward, left, spread, crypto, energy):",[round(x,2) for x in w])
print(f"train {len(tr)}  test {len(te)}; test ROI buying everything {roi(te):+.1%}")
for margin in (0.0,0.02,0.04,0.06,0.08):
    pick=[r for r in te if pred(w,feats(r))-r["price"]-fee(r["price"])>=margin]
    if len(pick)>=10:
        random.seed(2); by={}
        for r in pick: by.setdefault((r["series"],r["t"]),[]).append(r)
        ks=list(by); bs=sorted(roi([x for k in random.choices(ks,k=len(ks)) for x in by[k]]) for _ in range(300))
        print(f"  model edge >= {margin:.2f}: picks {len(pick):4} paid {sum(r['price'] for r in pick)/len(pick):.3f} won {sum(r['won'] for r in pick)/len(pick):5.1%} predicted {sum(pred(w,feats(r)) for r in pick)/len(pick):5.1%} test ROI {roi(pick):+6.1%}  90% [{bs[15]:+.0%},{bs[285]:+.0%}]")
