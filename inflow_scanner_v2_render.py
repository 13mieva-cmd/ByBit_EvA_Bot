# -*- coding: utf-8 -*-
"""
СКАНЕР ВЛИВАНИЙ v3 — ЛОНГ + СОПРОВОЖДЕНИЕ ПОЗИЦИИ (Telegram, для Railway)
========================================================================
Бот НЕ торгует сам. Он:
  1) подсвечивает ЛОНГ-сетапы (OI↑ + объём↑ + тренд вверх), расписывая логику;
  2) по кнопке "✅ Я вошёл" ведёт твою позицию: показывает P&L и комментирует
     "держать" (деньги ещё заходят) или "подумай о выходе" (приток выдыхается);
  3) по кнопке "❌ Выйти" фиксирует сделку в журнал с P&L (команда /log).

ЧЕСТНО: комментарии бота — ОПИСАНИЕ текущего состояния, не предсказание.
Edge направления мы измеряли — его нет. Решение и риск всегда на тебе.
Журнал входов/выходов нужен, чтобы посчитать реальную статистику твоего глаза.

Ключи через Environment: TG_TOKEN.   Команды: /start /scan /log /pos
"""
import os, time, json
import datetime as dt
import numpy as np
import requests

BYBIT="https://api.bybit.com"; QUOTE="USDT"
MAX_COINS=300; SCAN_EVERY_MIN=5; MAX_ALERTS=8
CHECK_POS_MIN=2; CALM_UPDATE_MIN=30
OI_4H_MIN=0.05; VOL_SPIKE_MIN=1.5; KNIFE_DD=-0.40; THIN_TURN=5_000_000
BTC_DUMP_1H=-0.02; HI_CORR=0.8
PRICE_UP_4H_MIN=0.005   # цена должна расти вместе с OI (иначе это шорты заходят)
RSI_MAX=78              # не ловить параболу на вершине (перекупленность)
MIN_BARS=200            # отсечь свежие листинги (казино)
COOLDOWN_H=4            # не показывать одну монету чаще, чем раз в 4ч
LAST_ALERT={}           # coin -> ts последней подсветки
WATCH={}                # coin -> {sym, zone_hi, zone_lo, ts, price0} — ждём ретеста
WATCH_HOURS=12          # сколько отслеживать ретест
WATCH_CHECK_SEC=15     # проверка ретеста раз в 15с (максимально быстро)
RETEST_NEED_BOUNCE=True # ретест = касание зоны + отбой (зелёная свеча)
TRADES=os.environ.get("TRADES_FILE","/tmp/scanner_trades.csv")
CHAT_FILE=os.environ.get("CHAT_FILE","/tmp/scanner_chat.txt")
TG_TOKEN=""   # данные с Bybit, ключ не нужен
SYM_CACHE={}            # coin -> symbol (для ведения позиции)
POSITIONS={}           # coin -> {entry, ts, sym, last_upd, last_state}

# ---------- Telegram ----------
def tg(method, **p):
    try:
        return requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",params=p,timeout=40).json()
    except requests.exceptions.ReadTimeout:
        return {}                      # штатный тайм-аут long-polling — молчим
    except Exception as e:
        print("TG:",e); return {}
def kb(rows): return json.dumps({"inline_keyboard":rows})
def tg_send(cid,t,buttons=None):
    p={"chat_id":cid,"text":t,"parse_mode":"HTML"}
    if buttons: p["reply_markup"]=kb(buttons)
    tg("sendMessage",**p)
def tg_answer(qid,text=""): tg("answerCallbackQuery",callback_query_id=qid,text=text)
def tg_send_doc(cid,path,caption=""):
    try:
        with open(path,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                          data={"chat_id":cid,"caption":caption},files={"document":f},timeout=60)
    except Exception as e: print("doc:",e)

# ---------- Bybit ----------
def bybit_price(coin):
    """Текущая цена перпа на Bybit. None если недоступно (напр. US-блок региона)."""
    try:
        r=requests.get("https://api.bybit.com/v5/market/tickers",
                       params={"category":"linear","symbol":coin+"USDT"}, timeout=8)
        if r.status_code!=200: return None
        j=r.json()
        lst=(j.get("result") or {}).get("list") or []
        return float(lst[0]["lastPrice"]) if lst else None
    except Exception:
        return None

def bget(path, params):
    r=requests.get(f"{BYBIT}{path}", params=params, timeout=20)
    if r.status_code!=200: raise RuntimeError(f"HTTP {r.status_code}")
    j=r.json()
    if j.get("retCode")!=0: raise RuntimeError(f"Bybit retCode {j.get('retCode')}: {j.get('retMsg')}")
    return j["result"]

_tickers_cache={"ts":0,"data":[]}
def all_tickers():
    """Все линейные тикеры Bybit (цена, оборот, funding) одним запросом, кэш 60с."""
    if time.time()-_tickers_cache["ts"]<60 and _tickers_cache["data"]:
        return _tickers_cache["data"]
    res=bget("/v5/market/tickers", {"category":"linear"})
    _tickers_cache["data"]=res["list"]; _tickers_cache["ts"]=time.time()
    return res["list"]

def universe():
    """Топ USDT-перпов Bybit по обороту 24ч."""
    rows=[x for x in all_tickers() if x["symbol"].endswith("USDT")]
    rows.sort(key=lambda x: float(x.get("turnover24h",0) or 0), reverse=True)
    seen=set(); out=[]
    for x in rows:
        b=x["symbol"][:-4]
        if b and b not in seen: seen.add(b); out.append((b,x["symbol"]))
    return out[:MAX_COINS]

def klines(symbol, limit=200):
    """Часовые свечи Bybit. Возврат: closes,highs,lows,vols (старые->новые)."""
    res=bget("/v5/market/kline", {"category":"linear","symbol":symbol,"interval":"60","limit":limit})
    k=res["list"][::-1]   # Bybit даёт новые->старые
    closes=[float(x[4]) for x in k]; highs=[float(x[2]) for x in k]
    lows=[float(x[3]) for x in k]; vols=[float(x[5]) for x in k]
    return closes,highs,lows,vols

def open_interest(symbol, limit=50):
    """История OI (часовая), старые->новые."""
    res=bget("/v5/market/open-interest", {"category":"linear","symbol":symbol,
                                           "intervalTime":"1h","limit":limit})
    oi=res["list"][::-1]
    return [float(x["openInterest"]) for x in oi]

def ticker_info(symbol):
    for t in all_tickers():
        if t["symbol"]==symbol:
            return dict(price=float(t["lastPrice"]),
                        funding=float(t.get("fundingRate",0) or 0),
                        turnover=float(t.get("turnover24h",0) or 0))
    return None

def rsi(closes, period=14):
    if len(closes)<period+1: return 50.0
    d=[closes[i+1]-closes[i] for i in range(len(closes)-1)]
    g=[x if x>0 else 0 for x in d]; l=[-x if x<0 else 0 for x in d]
    ag=sum(g[:period])/period; al=sum(l[:period])/period
    for i in range(period,len(d)):
        ag=(ag*(period-1)+g[i])/period; al=(al*(period-1)+l[i])/period
    if al==0: return 100.0
    return 100-100/(1+ag/al)

def ema(v,span):
    a=2/(span+1); e=v[0]
    for x in v[1:]: e=a*x+(1-a)*e
    return e
def corr(a,b):
    n=min(len(a),len(b))
    if n<10: return 0.0
    ra=np.diff(a[-n:]); rb=np.diff(b[-n:])
    if ra.std()==0 or rb.std()==0: return 0.0
    return float(np.corrcoef(ra,rb)[0,1])

def find_levels(highs, lows, closes, price, min_touches=3):
    """Сильные горизонтальные уровни. Касание = ОТДЕЛЬНЫЙ подход к уровню
    (цена подошла, ОТОШЛА минимум на 1.5%, потом вернулась). Не считаем
    каждую свечу у уровня — только реальные разы возврата."""
    if len(closes)<40: return []
    tol=price*0.010                      # зона уровня +-1.0%
    away=price*0.015                     # "отошла" = дальше 1.5% от уровня
    # свечи-экстремумы за окно
    H=highs[-150:]; L=lows[-150:]
    # кандидаты уровней — из локальных экстремумов
    cand=[]
    for i in range(2,len(H)-2):
        if H[i]>=max(H[i-2:i+3]): cand.append(H[i])   # локальный максимум
        if L[i]<=min(L[i-2:i+3]): cand.append(L[i])   # локальный минимум
    levels=[]
    used=[False]*len(cand)
    for i,base in enumerate(cand):
        if used[i]: continue
        cluster=[base]; used[i]=True
        for j in range(i+1,len(cand)):
            if not used[j] and abs(cand[j]-base)<=tol:
                cluster.append(cand[j]); used[j]=True
        lvl=sum(cluster)/len(cluster)
        # считаем ОТДЕЛЬНЫЕ подходы: идём по цене, +1 когда вернулись после отхода
        touches=0; state="away"
        for c in closes[-150:]:
            if abs(c-lvl)<=tol and state=="away":
                touches+=1; state="near"
            elif abs(c-lvl)>away:
                state="away"
        if touches>=min_touches:
            levels.append((lvl,touches))
    # убираем дубли-уровни близко друг к другу, оставляем сильнейший
    levels.sort(key=lambda x:-x[1]); out=[]
    for lv in levels:
        if all(abs(lv[0]-o[0])>tol*2 for o in out): out.append(lv)
    return out[:4]

def nearest_level(levels, price):
    """Ближайший уровень к цене + позиция (цена под/над ним)."""
    if not levels: return None
    lv=min(levels, key=lambda x:abs(x[0]-price))
    return dict(price=lv[0], touches=lv[1], dist=(price-lv[0])/price)

def core(coin,closes,highs,lows,vols,oic,btc,btc_p4=0.0):
    if len(closes)<MIN_BARS or len(oic)<25: return None   # свежие листинги отсекаем
    price=closes[-1]
    p4=closes[-1]/closes[-5]-1
    oi1=oic[-1]/oic[-2]-1 if oic[-2]>0 else 0
    oi4=oic[-1]/oic[-5]-1 if oic[-5]>0 else 0
    oi24=oic[-1]/oic[-25]-1 if oic[-25]>0 else 0
    vr=sum(vols[-4:]); vb=(sum(vols[-28:-4])/24*4) if len(vols)>=28 else vr
    spike=vr/vb if vb>0 else 0
    e21=ema(closes[-60:],21); e50=ema(closes[-60:],50)
    uptrend=price>e50 and e21>e50
    ext=(price-e21)/e21 if e21>0 else 0                 # насколько цена выше EMA21 (гонка за свечой)
    consol_base=min(lows[-8:]) if len(lows)>=8 else min(lows)   # база наторговки (недавняя опора)
    old_high=max(highs[-72:-4]) if len(highs)>76 else max(highs[:-4] or highs)  # старый хай (уровень)
    extended = ext>0.05
    # --- детектор треугольника (сужение диапазона -> крышка -> пробой) ---
    TRI=20
    # горизонтальные уровни (сильные = 3+ касания)
    levels=find_levels(highs,lows,closes,price,min_touches=3)
    lvl=nearest_level(levels,price)
    tri=None; tri_top=price
    if len(highs)>=TRI+2:
        wh=highs[-TRI-1:-1]; wl=lows[-TRI-1:-1]           # окно без текущего бара
        half=TRI//2
        r1=max(wh[:half])-min(wl[:half]); r2=max(wh[half:])-min(wl[half:])
        contracting = r1>0 and r2 < r1*0.8                # вторая половина уже первой = сужение
        tri_top=max(wh)                                    # крышка (сопротивление)
        if price>tri_top:            tri="breakout"        # закрытие выше крышки = пробой
        elif contracting and (tri_top-price)/price<=0.015: tri="ready"   # поджатие к крышке
        elif contracting:            tri="forming"        # просто сужается
    # --- детектор флага (импульс -> небольшой наклонный откат -> продолжение) ---
    flag=None; flag_top=price
    if len(closes)>=30:
        imp = closes[-15]/closes[-25]-1                      # был ли импульс ~10-25 баров назад
        pull = closes[-1]/closes[-15]-1                      # откат после импульса
        pull_range = (max(highs[-12:])-min(lows[-12:]))/price
        if imp>=0.05 and -0.06<=pull<=0.01 and pull_range<0.06:   # импульс вверх + неглубокий откат/консолидация
            flag_top=max(highs[-12:-1])
            flag = "breakout" if price>flag_top else "forming"
    hi7=max(highs[-168:]) if len(highs)>=168 else max(highs)
    dd=price/hi7-1
    turn=sum(vols[-24:])*price
    cor=corr(btc,closes)
    r=rsi(closes[-40:],14)
    # движение в основном за биткоином? (высокая корр + BTC двигался так же)
    btc_beta = cor>=HI_CORR and btc_p4>0 and abs(p4-btc_p4)<max(0.01,0.5*abs(btc_p4))
    tf=sum([oi1>0.01, oi4>=OI_4H_MIN, oi24>0.10])
    brk=price>max(highs[-168:-1]) if len(highs)>168 else False
    return dict(coin=coin,price=price,p4=p4,oi1=oi1,oi4=oi4,oi24=oi24,spike=spike,
                uptrend=uptrend,dd=dd,turn=turn,cor=cor,tf=tf,brk=brk,rsi=r,btc_beta=btc_beta,
                e21=e21,ext=ext,consol_base=consol_base,old_high=old_high,extended=extended,
                tri=tri,tri_top=tri_top,flag=flag,flag_top=flag_top,levels=levels,lvl=lvl)
def long_ok(m):
    return (m["oi4"]>=OI_4H_MIN and m["spike"]>=VOL_SPIKE_MIN and m["uptrend"]
            and m["dd"]>KNIFE_DD and m["turn"]>=THIN_TURN
            and m["p4"]>=PRICE_UP_4H_MIN            # цена растёт вместе с OI = ЛОНГИ заходят
            and m["rsi"]<RSI_MAX)                    # не парабола на вершине

# ---------- карточка с расписанной логикой ----------
def _bar(frac, width=10):
    frac=max(0.0,min(1.0,frac)); f=int(round(frac*width))
    return "\U0001F7E9"*f + "\u2B1C"*(width-f)   # 🟩 заполнено / ⬜ пусто — видно везде

def _score(m, ex):
    s=0
    s+= 2 if m["oi4"]>=0.10 else (1 if m["oi4"]>=0.05 else 0)
    s+= 2 if m["spike"]>=3 else (1 if m["spike"]>=1.5 else 0)
    s+= m["tf"]                                  # 0..3
    s+= 1 if m["brk"] else 0
    s+= 1 if 50<=m.get("rsi",50)<=70 else 0
    s+= 1 if not m.get("btc_beta") else 0
    s+= 1 if m["turn"]>=20_000_000 else 0
    # штраф за флаги перегрева
    if ex.get("funding",0)>0.01: s-=1
    if ex.get("liq_spike",0)>=2: s-=1
    return max(0,min(10,s))

def card(m, ex):
    cautions=[]
    if m.get("btc_beta"): cautions.append("движение в основном ЗА БИТКОМ — не её собственный приток")
    elif m["cor"]>=HI_CORR: cautions.append(f"сильно ходит за биткоином (corr {m['cor']:.2f})")
    if ex.get("funding",0)>0.01: cautions.append("перегрет плечом (высокий funding)")
    if ex.get("liq_spike",0)>=2: cautions.append(f"всплеск ликвидаций \u00d7{ex['liq_spike']:.1f}")
    if m.get("extended"): cautions.append("вход на пике импульса \u2014 лучше ждать откат")

    sc=_score(m,ex)
    head = "\U0001F7E2" if not cautions else "\U0001F7E1"
    arrow = "\u25B2" if m["p4"]>=0 else "\u25BC"
    rsi=int(m.get("rsi",50))
    tf_txt={3:"1ч+4ч+24ч \u2705",2:"2 интервала",1:"1 интервал \u26A0\uFE0F"}.get(m["tf"],"")

    # шкалы обычными строками (эмодзи-квадраты видно на любом телефоне)
    table=(
        f"\U0001F4B0 Приток OI  <b>{m['oi4']*100:+.0f}%</b>  {_bar(m['oi4']/0.20,5)}\n"
        f"\U0001F4C8 Объём      <b>\u00d7{m['spike']:.1f}</b>  {_bar(m['spike']/5,5)}\n"
        f"\U0001F321 RSI        <b>{rsi}</b>  {_bar(rsi/100,5)}\n"
        f"\U0001F4A7 Ликвидн.   <b>${m['turn']/1e6:.0f}M</b>  {_bar(min(m['turn']/100e6,1),5)}"
    )

    by=m.get("bybit")
    if by:
        spread=(by-m["price"])/m["price"]*100
        rel = "вровень" if abs(spread)<0.15 else (f"Bybit выше +{spread:.1f}%" if spread>0 else f"Bybit ниже {spread:.1f}%")
        price_line=f"\U0001F4B5 Binance <b>${m['price']:.5g}</b> | Bybit <b>${by:.5g}</b> ({rel})"
    else:
        price_line=f"\U0001F4B5 <b>${m['price']:.5g}</b> (Binance)  {arrow} {m['p4']*100:+.1f}% за 4ч   <i>Bybit: н/д</i>"
    lines=[
        f"{head} <b>{m['coin']}</b> \u00b7 лонг-сетап",
        price_line,
        "",
        f"\U0001F4AA <b>Сила сетапа:</b> {sc}/10  {_bar(sc/10,5)}",
        "",
        table,
        f"\U0001F4CA Подтверждение: {tf_txt}",
    ]
    reasons=[]
    reasons.append("деньги активно заходят" if m["oi4"]>=0.10 else "деньги заходят")
    reasons.append("тренд вверх (&gt;EMA50)")
    if m["brk"]: reasons.append("пробой 7д-максимума")
    if 50<=rsi<=70: reasons.append("RSI здоровый")
    lines.append("\u2705 " + ", ".join(reasons) + ".")

    if cautions:
        lines.append("")
        lines.append("\U0001F6E1 <b>Учти риски:</b>")
        for c in cautions: lines.append("\u26A0\uFE0F "+c)
    else:
        lines.append("\U0001F6E1 Риски: чисто \u2705 (не нож, ликвидность ок)")

    # зона входа — не гнаться за свечой, ждать откат к базе/EMA21
    e21=m.get("e21",m["price"]); base=m.get("consol_base",m["price"]); oh=m.get("old_high",m["price"])
    ext=m.get("ext",0)
    # --- блок треугольника ---
    tri=m.get("tri"); tt=m.get("tri_top",m["price"])
    if tri:
        lines.append("")
        if tri=="forming":
            lines.append("\U0001F53A <b>Треугольник: формируется</b>")
            lines.append(f"\u2022 цена поджимается, диапазон сужается \u2014 идёт наторговка в треугольник")
            lines.append(f"\u2022 крышка (сопротивление): <b>${tt:.5g}</b>")
            lines.append("\u2022 <i>ждём выхода за крышку, рано входить</i>")
        elif tri=="ready":
            lines.append("\u26A1 <b>Треугольник: готовность к пробою</b>")
            lines.append(f"\u2022 цена вплотную подошла к крышке <b>${tt:.5g}</b> и поджимается")
            lines.append(f"\u2022 <i>следи за закрытием свечи ВЫШЕ ${tt:.5g} \u2014 это будет пробой</i>")
            lines.append("\u2022 <i>не входи заранее: часто бывает ложный прокол вниз</i>")
        elif tri=="breakout":
            lines.append("\U0001F680 <b>Треугольник: ПРОБОЙ вверх</b>")
            lines.append(f"\u2022 цена закрылась выше крышки <b>${tt:.5g}</b> \u2014 треугольник пробит")
            lines.append(f"\u2022 \u26A0\uFE0F бывают ЛОЖНЫЕ пробои (снятие стопов) \u2014 подтверждение: удержание выше ${tt:.5g} или ретест крышки сверху")
    # --- блок флага ---
    fl=m.get("flag"); ft=m.get("flag_top",m["price"])
    if fl:
        lines.append("")
        if fl=="forming":
            lines.append("\U0001F6A9 <b>Флаг: откат после импульса</b>")
            lines.append("\u2022 был сильный импульс вверх, сейчас неглубокий откат-консолидация (флажок)")
            lines.append(f"\u2022 верх флага: <b>${ft:.5g}</b>")
            lines.append(f"\u2022 <i>классически цель \u2014 продолжение вверх при выходе за ${ft:.5g}</i>")
            lines.append("\u2022 <i>вход выгоднее у низа отката, чем на выходе</i>")
        elif fl=="breakout":
            lines.append("\U0001F6A9\U0001F680 <b>Флаг: пробой вверх</b>")
            lines.append(f"\u2022 цена вышла из флага выше <b>${ft:.5g}</b> \u2014 импульс продолжается")
            lines.append("\u2022 \u26A0\uFE0F подтверждение: удержание выше уровня; ложные выходы тоже бывают")
    lines.append("")
    # --- блок горизонтальных уровней ---
    lv=m.get("lvl")
    if lv:
        pos = "цена НА уровне" if abs(lv["dist"])<0.012 else ("цена НАД уровнем (уровень стал поддержкой)" if lv["dist"]>0 else "цена ПОД уровнем (уровень = сопротивление сверху)")
        lines.append("")
        strength = "очень сильный" if lv['touches']>=6 else ("сильный" if lv['touches']>=4 else "заметный")
        lines.append(f"\U0001F4CF <b>Уровень ${lv['price']:.5g}</b> \u2014 {strength}: цена подходила к нему <b>{lv['touches']} раз(а)</b>")
        lines.append(f"\u2022 {pos}")
        lines.append(f"\u2022 <i>чем больше подходов, тем важнее уровень (рынок его «уважает»)</i>")
        if abs(lv["dist"])<0.012:
            lines.append("\u2022 <i>цена наторговывает у уровня \u2014 это твоя зона, но вход ТОЛЬКО на ретесте с отбоем</i>")
    # правило входа — всегда на ретесте
    lines.append("")
    lines.append("\U0001F6D1 <b>Правило входа:</b> НЕ по рынку сейчас. Вход только на РЕТЕСТЕ уровня/зоны с отбоем (зелёная свеча). Бот позовёт.")
    if m.get("watching"):
        zlo,zhi=m["watching"]; wk=m.get("watch_kind","зоне")
        lines.append("")
        lines.append(f"\u23F3 <b>Взял на отслеживание</b> \u2014 позову на ретесте к {wk} ${zlo:.5g}\u2013${zhi:.5g}")
        if m.get("tri")=="breakout":
            lines.append("\u2022 <i>вход не на проколе, а на ретесте крышки сверху \u2014 защита от ложного пробоя</i>")
    lines.append("\U0001F4CD <b>Где входить:</b>")
    if m.get("extended"):
        lines.append(f"\u26A0\uFE0F цена на <b>+{ext*100:.0f}%</b> выше EMA21 \u2014 не гонись за свечой")
    lines.append(f"\u2022 зона отката (лимитка): <b>${e21:.5g}</b> (EMA21) \u2013 <b>${base:.5g}</b> (база наторговки)")
    hi_note = " \u2014 пробивается \U0001F680" if m["price"]>oh else " \u2014 цель"
    lines.append(f"\u2022 старый хай (уровень): <b>${oh:.5g}</b>{hi_note}")
    lines.append("\u2022 <i>выгоднее лимитка в зоне отката, чем по рынку на пике</i>")
    lines += ["", "\u2501"*16,
        "<i>\u26A0\uFE0F Подсветка, не приказ. Пойдёт ли вверх \u2014 не гарантия. "
        "Стоп на Bybit \u2014 обязателен.</i>"]
    return "\n".join(lines)

# ---------- сопровождение позиции ----------
def position_status(coin):
    p=POSITIONS.get(coin)
    if not p: return None,None
    try:
        closes,_,_,_=klines(p["sym"],limit=80); time.sleep(0.15)
        oic=open_interest(p["sym"],limit=10); time.sleep(0.15)
    except Exception: return None,None
    if len(closes)<55 or len(oic)<6: return None,None
    price=closes[-1]; pnl=price/p["entry"]-1
    oi1=oic[-1]/oic[-2]-1 if oic[-2]>0 else 0
    oi4=oic[-1]/oic[-5]-1 if oic[-5]>0 else 0
    e50=ema(closes[-60:],50)
    # ЯВНЫЙ разворот (жёсткие условия, чтобы меньше ложных тревог):
    reasons=[]
    if oi1<=-0.03: reasons.append(f"OI резко вниз ({oi1*100:+.0f}% за 1ч) — деньги выходят")
    if price<e50*0.995 and oi4<0: reasons.append("цена пробила EMA50 вниз, и OI больше не поддерживает")
    if pnl<=-0.03: reasons.append(f"цена ушла против входа на {pnl*100:.0f}%")
    if reasons:
        msg=(f"<b>{coin}</b>: похоже на РАЗВОРОТ — пора решать!\n"
             f"P&L: <b>{pnl*100:+.2f}%</b> (вход ${p['entry']:.5g} → ${price:.5g})\n"
             + "; ".join(reasons)+".\n"
             "<i>Если на бирже стоит стоп — он сработает сам. Решение твоё.</i>")
        return "reversal",msg
    msg=(f"🟢 <b>{coin}</b>: держится\n"
         f"P&L: <b>{pnl*100:+.2f}%</b> (вход ${p['entry']:.5g} → ${price:.5g})\n"
         f"Деньги ещё заходят (OI 4ч {oi4*100:+.0f}%), цена выше EMA50. Моментум цел.")
    return "ok",msg

def pos_buttons(coin): return [[{"text":"❌ Выйти / зафиксировать","callback_data":f"exit|{coin}"}]]

def close_trade(coin):
    p=POSITIONS.pop(coin,None)
    if not p: return None
    try:
        cc,_,_,_=klines(p["sym"],limit=2); price=cc[-1]
    except Exception: price=p["entry"]
    pnl=price/p["entry"]-1
    new=not os.path.exists(TRADES)
    with open(TRADES,"a") as f:
        if new: f.write("entry_ts,coin,entry_price,exit_ts,exit_price,pnl_pct\n")
        f.write(f"{p['ts']},{coin},{p['entry']:.6g},"
                f"{dt.datetime.now().isoformat(timespec='seconds')},{price:.6g},{pnl*100:.2f}\n")
    return pnl,p["entry"],price

# ---------- скан ----------
def run_scan(cid, announce=False):
    if announce: tg_send(cid,"🔍 Ищу лонг-сетапы, подожди пару минут...")
    try: coins=universe()
    except Exception as e: tg_send(cid,f"Ошибка данных: {e}"); return
    try:
        btc,_,_,_=klines("BTCUSDT",limit=30); time.sleep(0.15)
        btc_dump=len(btc)>2 and (btc[-1]/btc[-2]-1)<BTC_DUMP_1H
        btc_p4=(btc[-1]/btc[-5]-1) if len(btc)>=5 else 0.0
    except Exception: btc=[]; btc_dump=False; btc_p4=0.0
    hits=[]
    for coin,sym in coins:
        try:
            closes,highs,lows,vols=klines(sym,limit=200); time.sleep(0.15)
            oic=open_interest(sym,limit=50); time.sleep(0.15)
            if len(closes)<MIN_BARS or len(oic)<25: continue
            m=core(coin,closes,highs,lows,vols,oic,btc,btc_p4)
            if not (m and long_ok(m)): continue
            # антиспам: не повторять монету чаще COOLDOWN_H часов
            if time.time()-LAST_ALERT.get(coin,0) < COOLDOWN_H*3600: continue
            SYM_CACHE[coin]=sym; hits.append((m,sym))
        except Exception: continue
    if not hits:
        tg_send(cid,"Сейчас чистых лонг-сетапов нет. Это норма — лучше пропустить, чем войти в плохое."); return
    hits.sort(key=lambda x:x[0]["oi4"],reverse=True)
    if btc_dump: tg_send(cid,"‼️ Биток сейчас льёт — даже лонг-сетапы рискованны.")
    shown=0
    for m,sym in hits[:MAX_ALERTS]:
        ex={}
        try: ex=enrich(sym)
        except Exception: pass
        m["bybit"]=bybit_price(m["coin"])          # цена Bybit (или None)
        LAST_ALERT[m["coin"]]=time.time()
        # отслеживание ретеста: 1) пробой треугольника -> ждём ретест КРЫШКИ,
        #                        2) иначе цена оторвалась от зоны отката -> ждём зону
        lv=m.get("lvl")
        if lv and abs(lv["dist"])<0.03 and lv["touches"]>=3:
            base=lv["price"]
            WATCH[m["coin"]]=dict(sym=sym, zone_hi=base*1.008, zone_lo=base*0.99,
                                  ts=time.time(), price0=m["price"], kind=f"сильному уровню ${base:.5g}")
            m["watching"]=(base*0.99, base*1.008)
            m["watch_kind"]=f"уровню ${base:.5g} ({lv['touches']} касаний)"
        elif m.get("tri")=="breakout" and m.get("tri_top",0)>0:
            top=m["tri_top"]
            WATCH[m["coin"]]=dict(sym=sym, zone_hi=top*1.004, zone_lo=top*0.985,
                                  ts=time.time(), price0=m["price"], kind="пробой треугольника")
            m["watching"]=(top*0.985, top*1.004)
            m["watch_kind"]="крышке треугольника"
        else:
            zone_hi=m.get("e21",m["price"]); zone_lo=m.get("consol_base",m["price"])
            if m.get("extended") and zone_hi>0:
                WATCH[m["coin"]]=dict(sym=sym, zone_hi=zone_hi, zone_lo=zone_lo,
                                      ts=time.time(), price0=m["price"], kind="откат к зоне")
                m["watching"]=(zone_lo,zone_hi)
                m["watch_kind"]="зоне отката"
        btn=[[{"text":"✅ Я вошёл","callback_data":f"enter|{m['coin']}|{m['price']:.6g}"}]]
        tg_send(cid, card(m,ex), buttons=btn); shown+=1
    if shown==0:
        tg_send(cid,"Сетапы были, но недавно уже показаны (антиспам). Жди новых.")

def enrich(sym):
    """Доп.данные с Bybit: funding. (Ликвидаций в публичном REST нет.)"""
    out={}
    try:
        t=ticker_info(sym)
        if t: out["funding"]=t["funding"]
    except Exception: pass
    return out

def check_watchlist(chat):
    """Проверяет монеты в ожидании ретеста; зовёт, когда цена вернулась в зону и отбилась."""
    if not chat or not WATCH: return
    now=time.time()
    for coin in list(WATCH):
        w=WATCH[coin]
        if now-w["ts"]>WATCH_HOURS*3600:
            del WATCH[coin]; continue
        try:
            res=bget("/v5/market/kline", {"category":"linear","symbol":w["sym"],"interval":"60","limit":3})
            k=res["list"]; time.sleep(0.15)          # новые->старые, последняя = [0]
        except Exception:
            continue
        if len(k)<1: continue
        last=k[0]; o=float(last[1]); c=float(last[4]); lo=float(last[3])
        touched = lo <= w["zone_hi"]
        bounced = c >= o
        if c < w["zone_lo"]*0.97:                     # сетап развалился
            del WATCH[coin]; continue
        if touched and (bounced or not RETEST_NEED_BOUNCE):
            by=bybit_price(coin)
            byline = f"\nBybit: ${by:.5g}" if by else ""
            kind=w.get("kind","зоне")
            tg_send(chat,
                f"\U0001F3AF <b>{coin}: РЕТЕСТ ({kind})!</b>\n"
                f"Цена вернулась к ${w['zone_lo']:.5g}\u2013${w['zone_hi']:.5g} и отбивается (зелёная свеча).\n"
                f"Сейчас: <b>${c:.5g}</b>{byline}\n"
                f"<i>Вот безопасная точка входа, которую ты ждал. Проверь глазами, стоп обязателен.</i>",
                buttons=[[{"text":"\u2705 Я вошёл","callback_data":f"enter|{coin}|{c:.6g}"}]])
            del WATCH[coin]

# ---------- чат ----------
def save_chat(c):
    with open(CHAT_FILE,"w") as f: f.write(str(c))
def load_chat():
    try:
        with open(CHAT_FILE) as f: return f.read().strip()
    except: return None

def handle_callback(q):
    data=q.get("data",""); cid=str(((q.get("message") or {}).get("chat") or {}).get("id",""))
    tg_answer(q.get("id",""))
    if not cid: return
    parts=data.split("|")
    if parts[0]=="enter" and len(parts)>=3:
        coin=parts[1]; price=float(parts[2]); sym=SYM_CACHE.get(coin)
        if not sym: tg_send(cid,f"Не могу найти {coin} для ведения. Сделай /scan заново."); return
        POSITIONS[coin]=dict(entry=price,ts=dt.datetime.now().isoformat(timespec="seconds"),
                             sym=sym,last_upd=0,last_check=0,last_state="ok")
        tg_send(cid,f"✅ Веду позицию <b>{coin}</b> от <b>${price:.5g}</b>.\n"
                    f"Проверяю каждые {CHECK_POS_MIN} мин. Молчу, пока всё ок — крикну ‼️ при развороте.\n\n"
                    f"⚠️ <b>Сразу выстави стоп-ордер на Bybit</b> — это твоя мгновенная защита. "
                    f"Бот предупредит, но от резкого пролива спасает только стоп на бирже.",
                buttons=pos_buttons(coin))
    elif parts[0]=="exit" and len(parts)>=2:
        coin=parts[1]; res=close_trade(coin)
        if not res: tg_send(cid,f"Позиции по {coin} нет."); return
        pnl,e,x=res
        emo="🟢" if pnl>=0 else "🔴"
        tg_send(cid,f"{emo} Сделка по <b>{coin}</b> закрыта.\n"
                    f"Вход ${e:.5g} → выход ${x:.5g} = <b>{pnl*100:+.2f}%</b>\n"
                    f"Записал в журнал. Команда /log — скачать всю историю сделок.")

def main():
    global TG_TOKEN
    TG_TOKEN=os.environ.get("TG_TOKEN","").strip() or input("Токен бота: ").strip()
    if len(TG_TOKEN)<20: print("Нет валидного TG_TOKEN."); return
    me=tg("getMe")
    if not me.get("ok"): print("Не подключиться — проверь TG_TOKEN."); return
    print(f"Бот @{me['result']['username']} запущен (server mode).")
    offset=None; last_scan=0; chat=load_chat()
    while True:
        try:
            for u in tg("getUpdates",offset=offset,timeout=30).get("result",[]):
                offset=u["update_id"]+1
                if "callback_query" in u: handle_callback(u["callback_query"]); continue
                msg=u.get("message") or {}; text=(msg.get("text") or "").lower()
                cid=str((msg.get("chat") or {}).get("id",""))
                if not cid: continue
                if text.startswith("/start"):
                    chat=cid; save_chat(cid)
                    tg_send(cid,"✅ Сканер на сервере, работает 24/7.\n"
                                "/scan — искать лонг-сетапы\n/pos — мои позиции\n/watch — кого отслеживаю\n/log — журнал сделок\n/bybit — проверка доступа к Bybit\n\n"
                                "Подсвечу сетап → нажмёшь «Я вошёл» → буду вести позицию и комментировать. Решаешь ты.")
                elif text.startswith("/scan"): run_scan(cid, announce=True)
                elif text.startswith("/pos"):
                    if POSITIONS: tg_send(cid,"Открытые: "+", ".join(POSITIONS))
                    else: tg_send(cid,"Открытых позиций нет.")
                elif text.startswith("/bybit"):
                    # диагностика: доступен ли Bybit API с этого сервера
                    try:
                        r=requests.get("https://api.bybit.com/v5/market/tickers",
                                       params={"category":"linear","symbol":"BTCUSDT"}, timeout=10)
                        if r.status_code==200:
                            j=r.json()
                            p=(j.get("result") or {}).get("list",[{}])[0].get("lastPrice","?")
                            tg_send(cid, f"\u2705 <b>Bybit ДОСТУПЕН с сервера!</b>\n"
                                        f"BTC цена с Bybit: ${p}\n"
                                        f"Регион EU работает \u2014 можно переходить на Bybit-данные.")
                        else:
                            tg_send(cid, f"\u274C Bybit вернул код {r.status_code} (возможно, блок региона). "
                                        f"Ответ: {r.text[:200]}")
                    except Exception as e:
                        tg_send(cid, f"\u274C Bybit НЕдоступен с сервера: {type(e).__name__}. "
                                    f"Похоже, регион всё ещё блокируется.")
                elif text.startswith("/watch"):
                    if WATCH:
                        rows=[f"\u2022 {c}: жду ретест ${w['zone_lo']:.5g}\u2013${w['zone_hi']:.5g} ({w.get('kind','зона')})" for c,w in WATCH.items()]
                        tg_send(cid,"\u23F3 <b>На отслеживании:</b>\n"+"\n".join(rows))
                    else:
                        tg_send(cid,"Список ожидания пуст \u2014 никого не отслеживаю.")
                elif text.startswith("/log"):
                    if os.path.exists(TRADES) and os.path.getsize(TRADES)>0:
                        n=sum(1 for _ in open(TRADES))-1
                        tg_send_doc(cid,TRADES,f"Журнал сделок: {n}. Сохрани — на сервере файл сбрасывается при передеплое.")
                    else: tg_send(cid,"Журнал пуст — ещё не было закрытых сделок.")
            # авто-скан
            if chat and time.time()-last_scan>SCAN_EVERY_MIN*60:
                print(f'[scan] авто-скан {MAX_COINS} монет, chat={"есть" if chat else "НЕТ /start"}')
                run_scan(chat, announce=False); last_scan=time.time()
            # проверка ретестов из списка ожидания (раз в WATCH_CHECK_MIN минут)
            if time.time()-globals().get('_last_watch',0) > WATCH_CHECK_SEC:
                globals()['_last_watch']=time.time()
                try: check_watchlist(chat)
                except Exception as e: print('watch:',e)
            # сопровождение позиций: часто проверяем, тревога мгновенно, спокойное реже
            for coin in list(POSITIONS):
                p=POSITIONS[coin]; now=time.time()
                if now-p["last_check"]<CHECK_POS_MIN*60: continue
                p["last_check"]=now
                st,m=position_status(coin)
                if not m or not chat: continue
                if st=="reversal" and p["last_state"]!="reversal":
                    tg_send(chat,"‼️ "+m,buttons=pos_buttons(coin))
                    p["last_state"]="reversal"; p["last_upd"]=now
                elif st=="ok":
                    p["last_state"]="ok"
                    if now-p["last_upd"]>CALM_UPDATE_MIN*60:
                        tg_send(chat,m,buttons=pos_buttons(coin)); p["last_upd"]=now
            time.sleep(1)
        except Exception as e:
            print("loop:",e); time.sleep(10)

if __name__=="__main__":
    main()
