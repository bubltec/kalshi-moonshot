import json,statistics as S
R=json.load(open(__import__('sys').argv[1]))
R=[r for r in R if r['when']=='std']
R.sort(key=lambda r:r['close']); mid=R[len(R)//2]['close']
def line(name,sel):
    out=[]
    for lab,rs in (('all',sel),('H1',[r for r in sel if r['close']<mid]),('H2',[r for r in sel if r['close']>=mid])):
        if len(rs)<15: out.append(f"{lab}: n={len(rs)}"); continue
        out.append(f"{lab}: n={len(rs)} win {100*S.mean(r['won'] for r in rs):.1f}% ${S.mean(r['pnl'] for r in rs):+.2f}")
    print(f"{name:28}"+" | ".join(out))
for cls in ('crypto','commodity'):
    rs=[r for r in R if r['cls']==cls]; print('==',cls,len(rs)); line('baseline',rs)
    line('relapse (down,up,down)',[r for r in rs if r['relapse']])
    line('no relapse',[r for r in rs if not r['relapse']])
    line('lower_high relapse',[r for r in rs if r['lower_high']])
    for lo,hi in ((0,.34),(.34,.67),(.67,1.01)):
        line(f'retrace {lo:.2f}-{hi:.2f}',[r for r in rs if lo<=r['retr']<hi])
    for lo,hi in ((0,.5),(.5,.8),(.8,.95),(.95,1.01)):
        line(f'onside {lo}-{hi}',[r for r in rs if lo<=r['onside']<hi])
    for n in (0,1,2,3):
        line(f'crossings {n}'+('+' if n==3 else ''),[r for r in rs if (r['cross']==n if n<3 else r['cross']>=3)])
    for k in (0,1,2,3,4):
        line(f'legs {k}'+('+' if k==4 else ''),[r for r in rs if (r['nlegs']==k if k<4 else r['nlegs']>=4)])
    line('last leg down & big(>=2)',[r for r in rs if r['last_dir']==-1 and r['last_size']>=2])
    line('last leg up',[r for r in rs if r['last_dir']==1])
