"""What would the `curve` estimator's buys have made? (2026-11-12, user: "what about curve?"; yes, test it.)
The curve estimator (estimators.CurveEstimator, curve.py) has never left a settled bet. This replays it on the tape with its own code and its own stored table (data/curve_table.json, built 2026-10-25 from March-June one-minute data on five coins; carried in this script because the job image does not hold it). Stated before any result; nothing is fitted here and nothing is swept.
At the start of each of minutes 5 to 12 of every settled 15-minute crypto market in the window (DAYS, default 9, before the tape's end), the estimator's chance that the market finishes above its open is computed exactly as the estimator does: the index's minute closes since the open (a tick over 30 s old is no reading), the per-minute volatility of the 240 minutes before, curve.classify, and the table's shift on the random-walk baseline. A series the table has no entry for gets the baseline alone, as in the code.
The buy is the lane rule the estimator would drive: the first moment per market where the chance on a side beats that side's ask by the fee and min_edge = 0.015 (strategy.edge), two-sided quote no older than 20 seconds, $5 held to settlement with the fee as charged, against not buying. The same rule on the random-walk baseline alone is run beside it, so what the table adds can be seen.
Also: is the chance better than Kalshi's own mid at saying who wins (log-loss gain over the mid, per market, positive = better)? Reported by day, by the price paid, by whether the series is in the table (a label of the table's coverage, not a rule), and by curve shape: thirds of the table's own shift toward the side bought. The estimator reads the external settlement index, not Kalshi's quotes; that is what it is, and it is said here.
    scripts/cloud_job.sh scripts/curve_estimator_replay.py
"""

import bisect
import collections
import datetime as dt
import json
import math
import os
import statistics
import tempfile
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, curve, db
from kalshi_moonshot.kalshiws import RECORD_ONLY, index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import edge, order_fee

TABLE = {"made_at":1792951185.828475,"months":3,"coins":{"KXBTC15M":{"5":{"2":{"delta":0.0637,"n":670},"4":{"delta":-0.1122,"n":2272},"5":{"delta":-0.1119,"n":108},"9":{"delta":-0.0678,"n":100},"10":{"delta":0.0774,"n":298},"11":{"delta":-0.0007,"n":160},"13":{"delta":-0.0545,"n":267},"15":{"delta":0.0112,"n":109},"17":{"delta":-0.0657,"n":142},"20":{"delta":-0.0301,"n":220},"22":{"delta":0.1429,"n":255},"26":{"delta":0.0626,"n":116},"27":{"delta":0.032,"n":299},"28":{"delta":-0.0581,"n":152},"31":{"delta":0.0519,"n":2044},"33":{"delta":0.0279,"n":763},"34":{"delta":0.0272,"n":177}},"6":{"2":{"delta":-0.0254,"n":397},"4":{"delta":-0.0923,"n":2350},"5":{"delta":-0.0717,"n":199},"9":{"delta":0.0051,"n":117},"10":{"delta":-0.128,"n":267},"11":{"delta":-0.109,"n":291},"13":{"delta":0.0013,"n":174},"15":{"delta":0.0517,"n":133},"17":{"delta":-0.0184,"n":163},"20":{"delta":-0.0583,"n":142},"22":{"delta":0.1199,"n":301},"26":{"delta":0.0359,"n":190},"27":{"delta":-0.0448,"n":202},"28":{"delta":0.0608,"n":276},"31":{"delta":0.086,"n":1785},"33":{"delta":0.0824,"n":806},"34":{"delta":-0.0252,"n":201},"35":{"delta":-0.0053,"n":162}},"7":{"2":{"delta":-0.1772,"n":295},"4":{"delta":-0.1642,"n":2382},"5":{"delta":-0.1668,"n":299},"10":{"delta":-0.048,"n":235},"11":{"delta":-0.1631,"n":388},"13":{"delta":0.1195,"n":126},"15":{"delta":-0.073,"n":114},"17":{"delta":0.042,"n":174},"20":{"delta":-0.0113,"n":102},"22":{"delta":0.0405,"n":284},"26":{"delta":0.031,"n":164},"27":{"delta":0.0203,"n":170},"28":{"delta":0.0369,"n":353},"29":{"delta":0.091,"n":164},"31":{"delta":0.2194,"n":1678},"32":{"delta":0.0482,"n":119},"33":{"delta":0.1593,"n":778},"34":{"delta":0.0891,"n":237},"35":{"delta":0.0911,"n":223}},"8":{"2":{"delta":-0.1921,"n":173},"4":{"delta":-0.1741,"n":2463},"5":{"delta":-0.173,"n":413},"10":{"delta":-0.0209,"n":264},"11":{"delta":-0.1272,"n":395},"13":{"delta":0.0169,"n":134},"17":{"delta":-0.0444,"n":174},"22":{"delta":0.0164,"n":294},"26":{"delta":0.054,"n":169},"27":{"delta":0.0154,"n":141},"28":{"delta":0.1568,"n":364},"29":{"delta":0.0913,"n":229},"31":{"delta":0.2703,"n":1523},"32":{"delta":0.1657,"n":125},"33":{"delta":0.2128,"n":795},"34":{"delta":0.1115,"n":265},"35":{"delta":-0.0098,"n":308}},"9":{"2":{"delta":-0.0503,"n":105},"4":{"delta":-0.3005,"n":2421},"5":{"delta":-0.1198,"n":426},"10":{"delta":-0.1482,"n":285},"11":{"delta":-0.0699,"n":518},"17":{"delta":0.0402,"n":195},"22":{"delta":-0.0252,"n":301},"26":{"delta":0.0209,"n":161},"27":{"delta":0.1795,"n":102},"28":{"delta":0.1069,"n":386},"29":{"delta":0.1555,"n":237},"31":{"delta":0.3191,"n":1445},"32":{"delta":0.2851,"n":154},"33":{"delta":0.1019,"n":761},"34":{"delta":0.1342,"n":311},"35":{"delta":0.0458,"n":368}},"10":{"2":{"delta":-0.0189,"n":117},"4":{"delta":-0.4504,"n":2360},"5":{"delta":-0.205,"n":474},"10":{"delta":-0.0627,"n":260},"11":{"delta":-0.1748,"n":540},"17":{"delta":-0.1518,"n":200},"22":{"delta":0.0059,"n":297},"26":{"delta":0.1098,"n":178},"28":{"delta":-0.011,"n":371},"29":{"delta":0.143,"n":273},"31":{"delta":0.3939,"n":1361},"32":{"delta":0.1182,"n":181},"33":{"delta":0.4395,"n":733},"34":{"delta":0.0929,"n":353},"35":{"delta":0.1953,"n":444}},"11":{"4":{"delta":-0.3566,"n":2325},"5":{"delta":-0.2624,"n":569},"10":{"delta":-0.1047,"n":253},"11":{"delta":-0.2253,"n":549},"17":{"delta":-0.1636,"n":207},"22":{"delta":-0.0123,"n":264},"26":{"delta":0.2216,"n":133},"28":{"delta":0.1659,"n":485},"29":{"delta":-0.0165,"n":247},"31":{"delta":0.3803,"n":1265},"32":{"delta":0.0809,"n":205},"33":{"delta":0.3406,"n":733},"34":{"delta":0.1364,"n":374},"35":{"delta":0.1295,"n":455}},"12":{"4":{"delta":-0.2854,"n":2345},"5":{"delta":-0.2135,"n":609},"10":{"delta":-0.1561,"n":274},"11":{"delta":-0.3216,"n":541},"17":{"delta":-0.0827,"n":191},"22":{"delta":0.1638,"n":283},"26":{"delta":0.1107,"n":121},"28":{"delta":0.2407,"n":520},"29":{"delta":0.1245,"n":244},"31":{"delta":0.1473,"n":1177},"32":{"delta":0.0843,"n":202},"33":{"delta":0.4994,"n":714},"34":{"delta":0.2225,"n":452},"35":{"delta":0.0354,"n":477}}},"KXETH15M":{"5":{"2":{"delta":-0.0093,"n":305},"4":{"delta":-0.1746,"n":1303},"10":{"delta":-0.1929,"n":169},"11":{"delta":-0.1098,"n":109},"13":{"delta":-0.0356,"n":159},"20":{"delta":-0.0031,"n":157},"22":{"delta":0.0523,"n":136},"27":{"delta":0.0099,"n":192},"31":{"delta":0.0497,"n":1090},"33":{"delta":0.1854,"n":439}},"6":{"2":{"delta":-0.0882,"n":235},"4":{"delta":-0.1624,"n":1360},"5":{"delta":-0.0667,"n":131},"10":{"delta":-0.0555,"n":165},"11":{"delta":-0.213,"n":177},"17":{"delta":-0.0567,"n":100},"22":{"delta":-0.0363,"n":162},"26":{"delta":0.0009,"n":103},"27":{"delta":0.0605,"n":123},"28":{"delta":-0.057,"n":136},"31":{"delta":0.1325,"n":1051},"33":{"delta":0.0152,"n":414},"34":{"delta":0.2019,"n":112}},"7":{"2":{"delta":-0.1161,"n":140},"4":{"delta":-0.2943,"n":1383},"5":{"delta":-0.1538,"n":170},"10":{"delta":-0.0439,"n":140},"11":{"delta":-0.1296,"n":246},"17":{"delta":0.0314,"n":117},"22":{"delta":-0.0278,"n":175},"26":{"delta":0.036,"n":122},"27":{"delta":0.042,"n":104},"28":{"delta":0.0692,"n":165},"31":{"delta":0.2895,"n":920},"33":{"delta":0.1061,"n":423},"34":{"delta":0.1169,"n":132},"35":{"delta":-0.0036,"n":123}},"8":{"4":{"delta":-0.2585,"n":1379},"5":{"delta":-0.3162,"n":210},"10":{"delta":-0.0754,"n":180},"11":{"delta":-0.1781,"n":269},"17":{"delta":0.0004,"n":119},"22":{"delta":-0.0273,"n":182},"28":{"delta":0.0146,"n":197},"29":{"delta":0.1,"n":107},"31":{"delta":0.3387,"n":816},"33":{"delta":0.1747,"n":453},"34":{"delta":0.0618,"n":147},"35":{"delta":0.029,"n":157}},"9":{"4":{"delta":-0.4427,"n":1343},"5":{"delta":-0.16,"n":254},"10":{"delta":-0.229,"n":169},"11":{"delta":-0.1479,"n":306},"17":{"delta":-0.0802,"n":139},"22":{"delta":-0.0187,"n":186},"28":{"delta":0.117,"n":244},"29":{"delta":0.0164,"n":116},"31":{"delta":0.346,"n":812},"33":{"delta":0.2625,"n":404},"34":{"delta":0.0793,"n":169},"35":{"delta":0.0619,"n":194}},"10":{"4":{"delta":-0.5903,"n":1288},"5":{"delta":-0.2481,"n":309},"10":{"delta":-0.1217,"n":168},"11":{"delta":-0.2081,"n":325},"17":{"delta":-0.0781,"n":134},"22":{"delta":0.0761,"n":169},"28":{"delta":0.0191,"n":254},"29":{"delta":0.0118,"n":146},"31":{"delta":0.5117,"n":717},"32":{"delta":0.0946,"n":110},"33":{"delta":0.3961,"n":393},"34":{"delta":0.2792,"n":207},"35":{"delta":-0.0014,"n":242}},"11":{"4":{"delta":-0.5516,"n":1292},"5":{"delta":-0.4375,"n":345},"10":{"delta":-0.1667,"n":166},"11":{"delta":-0.1824,"n":326},"17":{"delta":-0.0904,"n":122},"22":{"delta":0.0137,"n":168},"28":{"delta":0.2738,"n":253},"29":{"delta":0.0726,"n":167},"31":{"delta":0.4655,"n":694},"33":{"delta":0.3307,"n":416},"34":{"delta":0.2757,"n":221},"35":{"delta":0.05,"n":264}},"12":{"4":{"delta":-0.4739,"n":1319},"5":{"delta":-0.4046,"n":325},"10":{"delta":-0.1402,"n":181},"11":{"delta":-0.3666,"n":364},"17":{"delta":-0.0471,"n":118},"22":{"delta":0.0774,"n":175},"28":{"delta":0.2284,"n":279},"29":{"delta":0.1772,"n":154},"31":{"delta":0.4496,"n":613},"32":{"delta":0.1317,"n":107},"33":{"delta":0.3861,"n":414},"34":{"delta":0.1414,"n":264},"35":{"delta":0.0828,"n":273}}},"KXSOL15M":{"5":{"2":{"delta":-0.0696,"n":576},"4":{"delta":-0.1654,"n":2286},"9":{"delta":0.112,"n":129},"10":{"delta":-0.1698,"n":356},"11":{"delta":-0.1697,"n":176},"13":{"delta":-0.1682,"n":234},"15":{"delta":-0.1255,"n":156},"16":{"delta":-0.0305,"n":119},"17":{"delta":-0.1915,"n":173},"20":{"delta":0.0622,"n":237},"22":{"delta":0.0918,"n":279},"26":{"delta":0.0277,"n":133},"27":{"delta":0.0331,"n":277},"28":{"delta":-0.0526,"n":148},"31":{"delta":0.1096,"n":1847},"33":{"delta":0.1252,"n":858},"34":{"delta":0.0736,"n":186},"35":{"delta":-0.0075,"n":108}},"6":{"2":{"delta":-0.0095,"n":351},"4":{"delta":-0.1694,"n":2427},"5":{"delta":-0.2122,"n":151},"9":{"delta":-0.052,"n":102},"10":{"delta":-0.1091,"n":362},"11":{"delta":-0.216,"n":304},"13":{"delta":0.0466,"n":176},"15":{"delta":-0.1186,"n":119},"17":{"delta":-0.1249,"n":217},"20":{"delta":-0.0334,"n":132},"22":{"delta":0.1088,"n":249},"26":{"delta":0.0339,"n":176},"27":{"delta":0.0711,"n":202},"28":{"delta":0.0673,"n":273},"31":{"delta":0.0794,"n":1679},"32":{"delta":0.0478,"n":102},"33":{"delta":0.1557,"n":868},"34":{"delta":0.0969,"n":222},"35":{"delta":0.0541,"n":176}},"7":{"2":{"delta":-0.092,"n":239},"4":{"delta":-0.2228,"n":2413},"5":{"delta":-0.234,"n":234},"10":{"delta":-0.0998,"n":339},"11":{"delta":-0.2191,"n":407},"13":{"delta":-0.0959,"n":131},"15":{"delta":-0.0606,"n":118},"17":{"delta":-0.0994,"n":233},"20":{"delta":0.0604,"n":105},"22":{"delta":0.0734,"n":293},"26":{"delta":0.023,"n":200},"27":{"delta":-0.0424,"n":152},"28":{"delta":0.0348,"n":328},"29":{"delta":-0.0255,"n":129},"31":{"delta":0.1392,"n":1485},"32":{"delta":0.0854,"n":114},"33":{"delta":0.1744,"n":858},"34":{"delta":0.0882,"n":291},"35":{"delta":0.0736,"n":265}},"8":{"2":{"delta":-0.0726,"n":169},"4":{"delta":-0.1979,"n":2502},"5":{"delta":-0.2465,"n":308},"10":{"delta":-0.064,"n":313},"11":{"delta":-0.2227,"n":444},"13":{"delta":-0.0469,"n":118},"17":{"delta":-0.0578,"n":215},"22":{"delta":0.0641,"n":294},"26":{"delta":0.151,"n":185},"27":{"delta":-0.0184,"n":117},"28":{"delta":0.0276,"n":340},"29":{"delta":-0.0155,"n":163},"31":{"delta":0.1779,"n":1360},"32":{"delta":0.3473,"n":161},"33":{"delta":0.1438,"n":819},"34":{"delta":0.0975,"n":346},"35":{"delta":-0.0124,"n":354}},"9":{"2":{"delta":-0.0581,"n":145},"4":{"delta":-0.3193,"n":2450},"5":{"delta":-0.1968,"n":353},"10":{"delta":-0.1161,"n":299},"11":{"delta":-0.2484,"n":544},"17":{"delta":-0.1353,"n":238},"22":{"delta":0.1034,"n":300},"26":{"delta":0.0984,"n":167},"28":{"delta":0.0861,"n":435},"29":{"delta":0.0675,"n":168},"31":{"delta":0.1058,"n":1266},"32":{"delta":0.0509,"n":167},"33":{"delta":0.3019,"n":795},"34":{"delta":0.2152,"n":412},"35":{"delta":0.0479,"n":392}},"10":{"2":{"delta":-0.1003,"n":112},"4":{"delta":-0.3052,"n":2351},"5":{"delta":-0.2599,"n":427},"10":{"delta":-0.1634,"n":323},"11":{"delta":-0.2696,"n":570},"17":{"delta":-0.2908,"n":250},"22":{"delta":0.1081,"n":296},"26":{"delta":0.2915,"n":151},"28":{"delta":-0.0406,"n":483},"29":{"delta":0.0448,"n":223},"31":{"delta":0.1582,"n":1167},"32":{"delta":0.1925,"n":174},"33":{"delta":0.2236,"n":704},"34":{"delta":0.2198,"n":443},"35":{"delta":0.0501,"n":479}},"11":{"4":{"delta":-0.3855,"n":2416},"5":{"delta":-0.2774,"n":486},"10":{"delta":-0.1743,"n":361},"11":{"delta":-0.3367,"n":521},"17":{"delta":-0.1689,"n":254},"22":{"delta":0.134,"n":281},"26":{"delta":0.1235,"n":143},"28":{"delta":0.3252,"n":521},"29":{"delta":0.1042,"n":229},"31":{"delta":0.0856,"n":1120},"32":{"delta":0.1146,"n":181},"33":{"delta":0.1934,"n":698},"34":{"delta":0.1591,"n":437},"35":{"delta":0.0905,"n":515}},"12":{"4":{"delta":-0.3209,"n":2457},"5":{"delta":-0.3269,"n":534},"10":{"delta":-0.325,"n":349},"11":{"delta":-0.2406,"n":511},"17":{"delta":-0.1783,"n":246},"22":{"delta":0.2884,"n":298},"26":{"delta":0.008,"n":105},"28":{"delta":0.2635,"n":557},"29":{"delta":0.2153,"n":242},"31":{"delta":0.026,"n":996},"32":{"delta":0.0362,"n":167},"33":{"delta":0.1012,"n":737},"34":{"delta":0.0737,"n":488},"35":{"delta":0.1703,"n":541}}},"KXXRP15M":{"5":{"2":{"delta":-0.0999,"n":573},"4":{"delta":-0.2043,"n":2259},"9":{"delta":-0.0037,"n":118},"10":{"delta":-0.0238,"n":332},"11":{"delta":-0.0096,"n":179},"13":{"delta":-0.162,"n":280},"15":{"delta":-0.0919,"n":149},"16":{"delta":-0.107,"n":117},"17":{"delta":-0.0415,"n":167},"20":{"delta":0.092,"n":251},"22":{"delta":0.0071,"n":246},"26":{"delta":0.1155,"n":139},"27":{"delta":0.0036,"n":282},"28":{"delta":0.0646,"n":176},"31":{"delta":-0.0319,"n":1828},"33":{"delta":0.1119,"n":852},"34":{"delta":-0.0459,"n":189},"35":{"delta":0.001,"n":104}},"6":{"2":{"delta":-0.0943,"n":383},"4":{"delta":-0.2138,"n":2368},"5":{"delta":-0.05,"n":215},"10":{"delta":-0.0948,"n":313},"11":{"delta":-0.2142,"n":293},"13":{"delta":-0.1158,"n":160},"15":{"delta":-0.119,"n":154},"17":{"delta":-0.0466,"n":192},"20":{"delta":0.0327,"n":144},"22":{"delta":0.0087,"n":287},"26":{"delta":0.0403,"n":160},"27":{"delta":-0.0105,"n":197},"28":{"delta":0.0668,"n":248},"31":{"delta":0.0751,"n":1723},"33":{"delta":0.0274,"n":833},"34":{"delta":0.0923,"n":258},"35":{"delta":0.0016,"n":184}},"7":{"2":{"delta":-0.2334,"n":269},"4":{"delta":-0.1989,"n":2422},"5":{"delta":-0.1598,"n":277},"10":{"delta":-0.0804,"n":289},"11":{"delta":-0.2734,"n":373},"13":{"delta":-0.0382,"n":120},"15":{"delta":-0.1664,"n":118},"17":{"delta":-0.0055,"n":244},"22":{"delta":-0.0038,"n":326},"26":{"delta":-0.0119,"n":173},"27":{"delta":-0.0359,"n":167},"28":{"delta":0.0385,"n":308},"29":{"delta":0.0126,"n":127},"31":{"delta":0.1328,"n":1523},"32":{"delta":0.0742,"n":136},"33":{"delta":0.181,"n":799},"34":{"delta":0.1428,"n":282},"35":{"delta":-0.019,"n":266}},"8":{"2":{"delta":-0.2274,"n":184},"4":{"delta":-0.2418,"n":2537},"5":{"delta":-0.0862,"n":306},"10":{"delta":-0.1906,"n":332},"11":{"delta":-0.2374,"n":435},"13":{"delta":-0.082,"n":100},"17":{"delta":-0.0501,"n":229},"22":{"delta":0.0529,"n":327},"26":{"delta":0.0367,"n":158},"27":{"delta":0.0293,"n":126},"28":{"delta":0.036,"n":345},"29":{"delta":0.0547,"n":167},"31":{"delta":0.1669,"n":1331},"32":{"delta":0.0557,"n":161},"33":{"delta":0.2535,"n":802},"34":{"delta":0.1121,"n":319},"35":{"delta":0.103,"n":333}},"9":{"2":{"delta":-0.1143,"n":125},"4":{"delta":-0.3944,"n":2548},"5":{"delta":-0.1678,"n":366},"10":{"delta":-0.2404,"n":333},"11":{"delta":-0.0976,"n":486},"17":{"delta":-0.1431,"n":240},"22":{"delta":0.0085,"n":304},"26":{"delta":0.1578,"n":137},"28":{"delta":0.0869,"n":424},"29":{"delta":0.1521,"n":191},"31":{"delta":0.1299,"n":1297},"32":{"delta":0.1003,"n":139},"33":{"delta":0.2947,"n":764},"34":{"delta":0.1499,"n":374},"35":{"delta":0.117,"n":354}},"10":{"4":{"delta":-0.4044,"n":2450},"5":{"delta":-0.28,"n":459},"10":{"delta":-0.3795,"n":335},"11":{"delta":-0.2619,"n":512},"17":{"delta":-0.213,"n":238},"22":{"delta":-0.0278,"n":290},"26":{"delta":0.1554,"n":144},"28":{"delta":0.16,"n":448},"29":{"delta":0.0951,"n":237},"31":{"delta":0.2603,"n":1214},"32":{"delta":0.1852,"n":196},"33":{"delta":0.3509,"n":671},"34":{"delta":0.2719,"n":398},"35":{"delta":0.1113,"n":464}},"11":{"4":{"delta":-0.5079,"n":2435},"5":{"delta":-0.4169,"n":516},"10":{"delta":-0.2351,"n":318},"11":{"delta":-0.3161,"n":532},"17":{"delta":-0.0782,"n":249},"22":{"delta":0.0884,"n":315},"26":{"delta":-0.0293,"n":130},"28":{"delta":0.2291,"n":452},"29":{"delta":0.1004,"n":233},"31":{"delta":0.2549,"n":1183},"32":{"delta":0.2813,"n":180},"33":{"delta":0.2681,"n":681},"34":{"delta":0.2159,"n":424},"35":{"delta":0.2423,"n":478}},"12":{"4":{"delta":-0.4269,"n":2461},"5":{"delta":-0.3828,"n":562},"10":{"delta":-0.2555,"n":326},"11":{"delta":-0.3072,"n":534},"17":{"delta":-0.1643,"n":208},"22":{"delta":0.0121,"n":296},"26":{"delta":0.243,"n":106},"28":{"delta":0.3776,"n":483},"29":{"delta":0.1289,"n":273},"31":{"delta":0.2013,"n":1065},"32":{"delta":0.1203,"n":164},"33":{"delta":0.2057,"n":707},"34":{"delta":0.312,"n":509},"35":{"delta":0.1259,"n":479}}},"KXDOGE15M":{"5":{"2":{"delta":-0.1296,"n":582},"4":{"delta":-0.2814,"n":2237},"9":{"delta":-0.0319,"n":134},"10":{"delta":0.0323,"n":378},"11":{"delta":-0.0627,"n":196},"13":{"delta":-0.1853,"n":255},"15":{"delta":0.0367,"n":150},"16":{"delta":-0.1459,"n":114},"17":{"delta":-0.1184,"n":193},"20":{"delta":0.0265,"n":219},"22":{"delta":0.0392,"n":242},"26":{"delta":0.0633,"n":147},"27":{"delta":0.0971,"n":307},"28":{"delta":0.0203,"n":173},"31":{"delta":0.1513,"n":1845},"33":{"delta":0.1165,"n":806},"34":{"delta":-0.0583,"n":192}},"6":{"2":{"delta":-0.0513,"n":338},"4":{"delta":-0.2915,"n":2407},"5":{"delta":-0.2065,"n":198},"9":{"delta":-0.1035,"n":117},"10":{"delta":-0.1692,"n":331},"11":{"delta":-0.182,"n":300},"13":{"delta":-0.0433,"n":196},"15":{"delta":-0.1602,"n":143},"17":{"delta":0.0175,"n":215},"20":{"delta":0.0391,"n":171},"22":{"delta":0.0177,"n":273},"26":{"delta":0.0307,"n":165},"27":{"delta":0.0982,"n":197},"28":{"delta":0.0819,"n":237},"31":{"delta":0.1624,"n":1716},"33":{"delta":0.1414,"n":771},"34":{"delta":0.1528,"n":240},"35":{"delta":0.0351,"n":200}},"7":{"2":{"delta":-0.1983,"n":270},"4":{"delta":-0.3358,"n":2416},"5":{"delta":-0.0802,"n":250},"10":{"delta":-0.1338,"n":316},"11":{"delta":-0.2852,"n":398},"13":{"delta":-0.04,"n":134},"15":{"delta":0.0489,"n":105},"17":{"delta":-0.0713,"n":253},"22":{"delta":0.0372,"n":328},"26":{"delta":0.098,"n":188},"27":{"delta":0.1007,"n":175},"28":{"delta":0.0228,"n":296},"31":{"delta":0.2122,"n":1562},"32":{"delta":0.0664,"n":126},"33":{"delta":0.1395,"n":782},"34":{"delta":0.199,"n":277},"35":{"delta":0.1391,"n":252}},"8":{"2":{"delta":-0.1074,"n":156},"4":{"delta":-0.3481,"n":2470},"5":{"delta":-0.196,"n":309},"10":{"delta":-0.1611,"n":387},"11":{"delta":-0.1481,"n":431},"13":{"delta":-0.1162,"n":115},"17":{"delta":-0.1303,"n":224},"22":{"delta":0.0669,"n":304},"26":{"delta":0.1083,"n":177},"27":{"delta":0.0776,"n":129},"28":{"delta":0.1121,"n":387},"29":{"delta":0.0244,"n":158},"31":{"delta":0.2942,"n":1403},"32":{"delta":0.2105,"n":127},"33":{"delta":0.1982,"n":822},"34":{"delta":0.096,"n":310},"35":{"delta":0.0519,"n":283}},"9":{"2":{"delta":-0.1414,"n":117},"4":{"delta":-0.3489,"n":2394},"5":{"delta":-0.3069,"n":416},"10":{"delta":-0.2547,"n":343},"11":{"delta":-0.2355,"n":501},"17":{"delta":-0.2417,"n":260},"22":{"delta":0.0111,"n":342},"26":{"delta":0.2348,"n":152},"27":{"delta":0.1413,"n":120},"28":{"delta":0.1045,"n":368},"29":{"delta":0.0097,"n":199},"31":{"delta":0.297,"n":1299},"32":{"delta":0.066,"n":173},"33":{"delta":0.2712,"n":770},"34":{"delta":0.1903,"n":364},"35":{"delta":0.055,"n":380}},"10":{"4":{"delta":-0.5461,"n":2372},"5":{"delta":-0.4082,"n":435},"10":{"delta":-0.1564,"n":309},"11":{"delta":-0.3163,"n":575},"17":{"delta":-0.0665,"n":250},"22":{"delta":0.0664,"n":332},"26":{"delta":0.2716,"n":164},"28":{"delta":0.2074,"n":469},"29":{"delta":0.132,"n":223},"31":{"delta":0.3153,"n":1208},"32":{"delta":0.212,"n":194},"33":{"delta":0.1768,"n":721},"34":{"delta":0.357,"n":388},"35":{"delta":-0.0346,"n":450}},"11":{"4":{"delta":-0.4414,"n":2364},"5":{"delta":-0.2807,"n":506},"10":{"delta":-0.2408,"n":354},"11":{"delta":-0.4685,"n":563},"17":{"delta":-0.1149,"n":258},"22":{"delta":0.0988,"n":306},"26":{"delta":0.0434,"n":129},"28":{"delta":0.2781,"n":489},"29":{"delta":0.0668,"n":248},"31":{"delta":0.3626,"n":1141},"32":{"delta":0.2509,"n":190},"33":{"delta":0.2348,"n":668},"34":{"delta":0.2395,"n":451},"35":{"delta":0.071,"n":484}},"12":{"4":{"delta":-0.4358,"n":2366},"5":{"delta":-0.4511,"n":555},"10":{"delta":-0.3128,"n":387},"11":{"delta":-0.3526,"n":559},"17":{"delta":-0.1185,"n":257},"22":{"delta":0.0756,"n":311},"26":{"delta":0.1722,"n":103},"28":{"delta":0.2975,"n":529},"29":{"delta":0.1424,"n":264},"31":{"delta":0.1956,"n":1011},"32":{"delta":0.4025,"n":203},"33":{"delta":0.4373,"n":657},"34":{"delta":0.2671,"n":476},"35":{"delta":0.1926,"n":514}}}},"made_from":"BTC: 2026-03-03T14:45:00.000 to 2026-06-01T14:15:00.000 (8,605 windows); ETH: 2026-03-03T02:00:00.000 to 2026-06-01T01:30:00.000 (4,887 windows); SOL: 2026-03-03T02:00:00.000 to 2026-06-01T01:30:00.000 (8,639 windows); XRP: 2026-03-03T02:00:00.000 to 2026-06-01T01:30:00.000 (8,618 windows); DOGE: 2026-03-03T02:00:00.000 to 2026-06-01T01:30:00.000 (8,639 windows); "}
s = config.reload()
ET = ZoneInfo("America/New_York")
DAYS = float(os.environ.get("DAYS", "9"))
MIN_EDGE, STAKE, STALE = 0.015, 5.0, 20.0
tmp = tempfile.mkdtemp()
with open(os.path.join(tmp, curve.TABLE_FILE), "w") as f:
    json.dump(TABLE, f)
table = curve.load_table(tmp)
in_table = set(TABLE["coins"])
store = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
tape_end = float(tape.execute("SELECT max(ts) FROM quotes").fetchone()[0])
wins = store.db.execute("SELECT ticker,series,close_ts,result FROM windows WHERE close_ts>? AND close_ts<=? AND result IN ('yes','no') ORDER BY series,close_ts", (tape_end - DAYS * 86400, tape_end)).fetchall()
wins = [w for w in wins if w["series"] not in RECORD_ONLY and w["series"].endswith("15M") and index_for_series(w["series"])]
t_first = min(float(w["close_ts"]) for w in wins) - 900 - 241 * 60
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
skipped = collections.Counter()
MOM = []  # (ticker, chance of yes, baseline, Kalshi's yes mid, yes won)
BUY = {"table": [], "baseline": []}  # (ticker, day, ask, won, in table, shift toward the side)
cur, ts, vs = None, [], []
for w in wins:
    ser = w["series"]
    if ser != cur:
        cur = ser
        r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (index_for_series(ser), t_first)).fetchall()
        ts, vs = [float(x[0]) for x in r], [float(x[1]) for x in r]

    def at(t, ts=ts, vs=vs):
        i = bisect.bisect_right(ts, t) - 1
        return vs[i] if i >= 0 and t - ts[i] < 30.0 else None

    close = float(w["close_ts"])
    opened = close - 900.0
    open_value = at(opened)
    before = [v for k in range(-240, 1) if (v := at(opened + 60.0 * k)) is not None]
    vol = curve.per_minute_vol(before)
    if not open_value or not vol:
        skipped["no index reading at the open, or under 30 minutes of index before it"] += 1
        continue
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 AND ask<1 ORDER BY ts", (w["ticker"], opened, close)).fetchall()
    qt = [float(x[0]) for x in q]
    yes_won = 1.0 if w["result"] == "yes" else 0.0
    day = dt.datetime.fromtimestamp(close, ET).strftime("%m-%d")
    done = {"table": False, "baseline": False}
    for m in range(curve.MIN_MINUTE, curve.MAX_MINUTE + 1):
        path = [at(opened + 60.0 * k) for k in range(1, m + 1)]
        if any(p is None for p in path):
            skipped["moments with a minute close missing"] += 1
            continue
        info = curve.classify(open_value, path, vol)
        if info is None:
            skipped["moments the classifier cannot read"] += 1
            continue
        p_up, base, _ = table.chance_up(ser, info)
        i = bisect.bisect_right(qt, opened + 60.0 * m) - 1
        if i < 0 or opened + 60.0 * m - qt[i] > STALE:
            skipped["moments with no two-sided quote"] += 1
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        MOM.append((w["ticker"], p_up, base, (yb + ya) / 2, yes_won))
        for name, p in (("table", p_up), ("baseline", base)):
            if done[name]:
                continue
            best = None
            for sign, ps, ask, won in ((1, p, ya, yes_won), (-1, 1 - p, 1 - yb, 1 - yes_won)):
                e = edge(ps, ask)
                if e >= MIN_EDGE and (best is None or e > best[0]):
                    best = (e, ask, won, sign * (p_up - base))
            if best:
                BUY[name].append((w["ticker"], day, best[1], best[2], ser.replace("15M", "") in in_table or ser in in_table, best[3]))
                done[name] = True

print(f"{len(MOM)} moments from {len({x[0] for x in MOM})} markets of {len(wins)} on the tape from {hm(tape_end - DAYS * 86400)} to {hm(tape_end)} ET; the table's coins: {sorted(in_table)}")
print("sat out: " + (", ".join(f"{k} {v}" for k, v in skipped.most_common()) or "none"))


def ll(p, won):
    p = min(0.995, max(0.005, p))
    return -(math.log(p) if won else math.log(1 - p))


print("\nis the chance better than Kalshi's mid at saying who wins? log-loss gain over the mid, x1000, a market's moments averaged first (positive = better than Kalshi)")
for name, ix in (("the curve table's chance", 1), ("the random-walk baseline alone", 2)):
    by = collections.defaultdict(list)
    for x in MOM:
        by[x[0]].append(ll(x[3], x[4]) - ll(x[ix], x[4]))
    v = [statistics.fmean(g) for g in by.values()]
    print(f"  {name:32s} {1000 * statistics.fmean(v):+8.2f} +- {1000 * statistics.stdev(v) / math.sqrt(len(v)):.2f}  ({len(v)} markets)")


def dollars(x):
    n = max(1, int(STAKE // x[2]))
    cost = n * x[2] + order_fee(x[2], n)
    return n * x[3] - cost, cost


def line(label, xs):
    if len(xs) < 2:
        return f"    {label:26s} n={len(xs)}"
    d = [dollars(x) for x in xs]
    v = [p / c for p, c in d]
    return f"    {label:26s} n={len(xs):5d} | dollars {sum(p for p, _ in d):+9.2f} on {sum(c for _, c in d):9.2f} | net per $1 {statistics.fmean(v):+.3f} (se {statistics.stdev(v) / math.sqrt(len(v)):.3f}) | beat not buying {sum(1 for p, _ in d if p > 0)}, lost to it {sum(1 for p, _ in d if p <= 0)} | mean ask {statistics.fmean(x[2] for x in xs):.2f}"


print(f"\nthe buy: first moment per market where the chance beats the ask by the fee and {MIN_EDGE}, ${STAKE:g} held to settlement, against not buying")
for name in ("table", "baseline"):
    xs = BUY[name]
    print(f"  on the {'curve table' if name == 'table' else 'random-walk baseline alone'}")
    print(line("all buys", xs))
    for lab, a, b in (("ask under 0.40", 0, 0.4), ("ask 0.40-0.60", 0.4, 0.6), ("ask 0.60-0.70", 0.6, 0.7), ("ask 0.70-0.90", 0.7, 0.9), ("ask 0.90 and over", 0.9, 1.01)):
        print(line(lab, [x for x in xs if a <= x[2] < b]))
    if name == "table":
        print(line("series in the table", [x for x in xs if x[4]]))
        print(line("series not in the table", [x for x in xs if not x[4]]))
        tb = [x for x in xs if x[4]]
        if len(tb) > 90:
            v = sorted(x[5] for x in tb)
            c1, c2 = v[len(v) // 3], v[2 * len(v) // 3]
            print(line("table's shift to side: low", [x for x in tb if x[5] < c1]))
            print(line("table's shift to side: mid", [x for x in tb if c1 <= x[5] < c2]))
            print(line("table's shift to side: high", [x for x in tb if x[5] >= c2]))
        for d_ in sorted({x[1] for x in xs}):
            print(line(d_, [x for x in xs if x[1] == d_]))
print("DONE")
