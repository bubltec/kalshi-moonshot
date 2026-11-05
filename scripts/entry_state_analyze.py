import json, collections, statistics as S
R=json.load(open(__import__('sys').argv[1]))
def zb(z): return 'z<0.5' if z<0.5 else 'z0.5-1' if z<1 else 'z1-1.5' if z<1.5 else 'z1.5-2.5' if z<2.5 else 'z2.5+'
def sb(s): return 'toward' if s<=-0.5 else 'flat' if s<0.5 else 'away'
def cb(c): return 'x0-1' if c<=1 else 'x2-3' if c<=3 else 'x4+'
def lb(l): return 'L120-240' if l<240 else 'L240-360' if l<360 else 'L360-480'
for cls in ('crypto','commodity'):
    rs=[r for r in R if r['cls']==cls]; rs.sort(key=lambda r:r['close']); mid=rs[len(rs)//2]['close']
    print(f"\n=== {cls}: {len(rs)} moments in {len({r['w'] for r in rs})} windows; every moment's buy: {sum(r['pnl'] for r in rs)/len(rs):+.2f}/bet")
    def table(keyf,title,minn=150):
        d=collections.defaultdict(lambda:{'a':[],'1':[],'2':[]})
        for r in rs:
            k=keyf(r); d[k]['a'].append(r); d[k]['1' if r['close']<mid else '2'].append(r)
        print(title)
        for k in sorted(d):
            a=d[k]['a']
            if len(a)<minn: continue
            f=lambda L:(f"{S.mean(x['pnl'] for x in L):+.2f}" if len(L)>=40 else ' n/a ')
            print(f"  {' / '.join(k):28} n={len(a):5d} win {100*S.mean(x['won'] for x in a):.0f}%  all {f(a)}  H1 {f(d[k]['1'])}  H2 {f(d[k]['2'])}")
    table(lambda r:(zb(r['z']),sb(r['slope'])),'by cushion x slope:')
    table(lambda r:(cb(r['crosses']),),'by crossings:')
    table(lambda r:(lb(r['left']),),'by time left:')
    table(lambda r:(zb(r['z']),lb(r['left'])),'by cushion x time left:')
print('DONE')
