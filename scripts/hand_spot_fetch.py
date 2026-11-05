import json, time, datetime, urllib.request, urllib.parse, collections, sys
MON={m:i for i,m in enumerate('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split(),1)}
PROD={'KXBTC15M':'BTC-USD','KXETH15M':'ETH-USD','KXSOL15M':'SOL-USD','KXXRP15M':'XRP-USD','KXDOGE15M':'DOGE-USD'}
def close_utc(tk):
    seg=tk.split('-')[1]; yy,mo,dd,hh,mi=int(seg[0:2]),MON[seg[2:5]],int(seg[5:7]),int(seg[7:9]),int(seg[9:11])
    est = (mo==11 and dd>=1 and (hh>=2 or dd>1)) or mo==12
    off=5 if est else 4
    return datetime.datetime(2000+yy,mo,dd,hh,mi,tzinfo=datetime.timezone.utc).timestamp()+off*3600
def fetch(prod,a,b):
    q=urllib.parse.urlencode({'start':datetime.datetime.fromtimestamp(a,datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),'end':datetime.datetime.fromtimestamp(b,datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),'granularity':60})
    req=urllib.request.Request(f'https://api.exchange.coinbase.com/products/{prod}/candles?{q}',headers={'User-Agent':'moonshot-research'})
    with urllib.request.urlopen(req,timeout=20) as r: return json.loads(r.read())
R=json.load(open('/tmp/handstudy.json'))
tks=sorted({(r['ticker']) for r in R if r['series'] in PROD})
print('crypto markets in the hand study:',len(tks),flush=True)
try:
    t=tks[0]; c=close_utc(t); x=fetch(PROD[t.split('-')[0]],c-900-3700,c)
    print('test fetch ok:',len(x),'candles for',t,flush=True)
except Exception as e:
    print('FETCH FAILED:',repr(e)[:200]); sys.exit()
bars={}
n=0
for t in tks:
    ser=t.split('-')[0]; c=close_utc(t)
    for attempt in range(3):
        try:
            x=fetch(PROD[ser],c-900-3700,c); break
        except Exception as e:
            time.sleep(1.5*(attempt+1)); x=None
    if not x: continue
    bars[t]=sorted([[k[0],k[1],k[2],k[3],k[4]] for k in x])   # time, low, high, open, close
    n+=1
    if n%100==0: print('fetched',n,flush=True)
    time.sleep(0.12)
json.dump(bars,open('/tmp/spotbars.json','w'))
print('markets with spot bars',len(bars),'DONE',flush=True)
