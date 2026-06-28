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

Ключи через Environment: TG_TOKEN, CA_KEY.   Команды: /start /scan /log /pos
"""
import os, time, json
import datetime as dt
import numpy as np
import requests

COINALYZE="https://api.coinalyze.net/v1"; QUOTE="USDT"
MAX_COINS=50; SCAN_EVERY_MIN=30; MAX_ALERTS=6
CHECK_POS_MIN=2; CALM_UPDATE_MIN=30
OI_4H_MIN=0.05; VOL_SPIKE_MIN=1.5; KNIFE_DD=-0.40; THIN_TURN=5_000_000
BTC_DUMP_1H=-0.02; HI_CORR=0.8
TRADES=os.environ.get("TRADES_FILE","/tmp/scanner_trades.csv")
CHAT_FILE=os.environ.get("CHAT_FILE","/tmp/scanner_chat.txt")
TG_TOKEN=""; CA_KEY=""
SYM_CACHE={}            # coin -> symbol (для ведения позиции)
POSITIONS={}           # coin -> {entry, ts, sym, last_upd, last_state}

# ---------- Telegram ----------
def tg(method, **p):
    try: return requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",params=p,timeout=35).json()
    except Exception as e: print("TG:",e); return {}
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

# ---------- Coinalyze ----------
def ca(path,params):
    params=dict(params); params["api_key"]=CA_KEY
    r=requests.get(f"{COINALYZE}{path}",params=params,timeout=30)
    if r.status_code!=200: raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()
_markets=None
def universe():
    global _markets
    if _markets is None: _markets=ca("/future-markets",{})
    perps=[x for x in _markets if x.get("is_perpetual") and x.get("quote_asset")==QUOTE
           and x.get("symbol","").endswith(".A")]
    seen=set(); out=[]
    for x in perps:
        b=x.get("base_asset")
        if b and b not in seen: seen.add(b); out.append((b,x["symbol"]))
    return out[:MAX_COINS]
def H(path,sym,frm,to,keys,usd=False):
    pr={"symbols":sym,"interval":"1hour","from":frm,"to":to}
    if usd: pr["convert_to_usd"]="true"
    j=ca(path,pr)
    if not j or "history" not in j[0]: return []
    out=[]
    for h in j[0]["history"]:
        try: out.append(tuple(float(h[k]) for k in keys))
        except Exception: pass
    return out

# ---------- математика ----------
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

def core(coin,closes,highs,lows,vols,oic,btc):
    if len(closes)<60 or len(oic)<25: return None
    price=closes[-1]
    p4=closes[-1]/closes[-5]-1
    oi1=oic[-1]/oic[-2]-1 if oic[-2]>0 else 0
    oi4=oic[-1]/oic[-5]-1 if oic[-5]>0 else 0
    oi24=oic[-1]/oic[-25]-1 if oic[-25]>0 else 0
    vr=sum(vols[-4:]); vb=(sum(vols[-28:-4])/24*4) if len(vols)>=28 else vr
    spike=vr/vb if vb>0 else 0
    e21=ema(closes[-60:],21); e50=ema(closes[-60:],50)
    uptrend=price>e50 and e21>e50
    hi7=max(highs[-168:]) if len(highs)>=168 else max(highs)
    dd=price/hi7-1
    turn=sum(vols[-24:])*price
    cor=corr(btc,closes)
    tf=sum([oi1>0.01, oi4>=OI_4H_MIN, oi24>0.10])
    brk=price>max(highs[-168:-1]) if len(highs)>168 else False
    return dict(coin=coin,price=price,p4=p4,oi1=oi1,oi4=oi4,oi24=oi24,spike=spike,
                uptrend=uptrend,dd=dd,turn=turn,cor=cor,tf=tf,brk=brk)
def long_ok(m): return m["oi4"]>=OI_4H_MIN and m["spike"]>=VOL_SPIKE_MIN and m["uptrend"] and m["dd"]>KNIFE_DD and m["turn"]>=THIN_TURN

# ---------- карточка с расписанной логикой ----------
def card(m, ex):
    tf_word={3:"за 1 час, за 4 часа и за сутки — на всех трёх",
             2:"на двух интервалах",1:"только на одном интервале"}.get(m["tf"],"")
    cautions=[]
    if m["cor"]>=HI_CORR: cautions.append(f"монета сильно ходит за биткоином (corr {m['cor']:.2f}) — если биток польёт, утянет и её")
    if ex.get("funding",0)>0.01: cautions.append("высокий funding — набилось много плечевых лонгов, монета хрупкая")
    if ex.get("liq_spike",0)>=2: cautions.append(f"всплеск ликвидаций ×{ex['liq_spike']:.1f} — выносят чьи-то стопы")
    head="🟢" if not cautions else "🟡"
    lines=[f"{head} <b>{m['coin']}</b> — лонг-сетап", "",
        "<b>Почему подсвечено:</b>",
        f"• <b>Деньги заходят:</b> открытый интерес (OI) вырос на <b>{m['oi4']*100:+.0f}%</b> за 4 часа, "
        f"а объём торгов в <b>{m['spike']:.1f}×</b> выше обычного. В монету активно вливают деньги прямо сейчас.",
        f"• <b>Заход устойчивый:</b> рост OI виден {tf_word}. {'Не разовый выброс.' if m['tf']>=2 else 'Пока слабое подтверждение.'}",
        "• <b>Тренд вверх:</b> цена выше своей средней (EMA50) — растущая структура, а не падающий нож.",
        f"• <b>Риски в норме:</b> не обвал ({m['dd']*100:+.0f}% от недавнего максимума), ликвидности хватает (~${m['turn']/1e6:.0f}M оборот).",
    ]
    if m["brk"]: lines.append("• <b>Пробой:</b> цена обновила 7-дневный максимум — сила покупателей.")
    if cautions:
        lines.append(""); lines.append("<b>⚠️ Но учти:</b>")
        for c in cautions: lines.append("• "+c)
    lines += ["", f"Цена сейчас: <b>${m['price']:.5g}</b> ({m['p4']*100:+.1f}% за 4ч)", "",
        "<i>Это не приказ покупать. Бот показал, что деньги заходят — пойдёт ли цена "
        "вверх, не гарантирует никто. Решаешь и рискуешь ты.</i>"]
    return "\n".join(lines)

# ---------- сопровождение позиции ----------
def position_status(coin):
    p=POSITIONS.get(coin)
    if not p: return None,None
    to=int(time.time()); frm=to-8*24*3600
    try:
        px=H("/ohlcv-history",p["sym"],frm,to,["c"]); time.sleep(1.0)
        oi=H("/open-interest-history",p["sym"],frm,to,["c"],usd=True); time.sleep(1.0)
    except Exception: return None,None
    if len(px)<55 or len(oi)<6: return None,None
    closes=[x[0] for x in px]; oic=[x[0] for x in oi]
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
    to=int(time.time()); frm=to-2*24*3600
    try:
        px=H("/ohlcv-history",p["sym"],frm,to,["c"]); price=px[-1][0]
    except Exception: price=p["entry"]
    pnl=price/p["entry"]-1
    new=not os.path.exists(TRADES)
    with open(TRADES,"a") as f:
        if new: f.write("entry_ts,coin,entry_price,exit_ts,exit_price,pnl_pct\n")
        f.write(f"{p['ts']},{coin},{p['entry']:.6g},"
                f"{dt.datetime.now().isoformat(timespec='seconds')},{price:.6g},{pnl*100:.2f}\n")
    return pnl,p["entry"],price

# ---------- скан ----------
def run_scan(cid):
    tg_send(cid,"🔍 Ищу лонг-сетапы (деньги заходят + тренд вверх)...")
    try: coins=universe()
    except Exception as e: tg_send(cid,f"Ошибка данных: {e}"); return
    to=int(time.time()); frm=to-9*24*3600
    try:
        btc=[x[0] for x in H("/ohlcv-history","BTCUSDT.A",frm,to,["c"])]; time.sleep(1.2)
        btc_dump=len(btc)>2 and (btc[-1]/btc[-2]-1)<BTC_DUMP_1H
    except Exception: btc=[]; btc_dump=False
    hits=[]
    for coin,sym in coins:
        try:
            px=H("/ohlcv-history",sym,frm,to,["c","h","l","v"]); time.sleep(1.2)
            oi=H("/open-interest-history",sym,frm,to,["c"],usd=True); time.sleep(1.2)
            if len(px)<60 or len(oi)<25: continue
            m=core(coin,[a[0] for a in px],[a[1] for a in px],[a[2] for a in px],
                   [a[3] for a in px],[a[0] for a in oi],btc)
            if m and long_ok(m): SYM_CACHE[coin]=sym; hits.append((m,sym))
        except Exception: continue
    if not hits:
        tg_send(cid,"Сейчас чистых лонг-сетапов нет. Это норма — лучше пропустить, чем войти в плохое."); return
    hits.sort(key=lambda x:x[0]["oi4"],reverse=True)
    if btc_dump: tg_send(cid,"‼️ Биток сейчас льёт — даже лонг-сетапы рискованны.")
    for m,sym in hits[:MAX_ALERTS]:
        ex={}
        try: ex=enrich(sym)
        except Exception: pass
        btn=[[{"text":"✅ Я вошёл","callback_data":f"enter|{m['coin']}|{m['price']:.6g}"}]]
        tg_send(cid, card(m,ex), buttons=btn)

def enrich(sym):
    to=int(time.time()); frm=to-2*24*3600; out={}
    try:
        liq=H("/liquidation-history",sym,frm,to,["l","s"],usd=True); time.sleep(1.1)
        if liq:
            rec=sum(a+b for a,b in liq[-4:]); base=(sum(a+b for a,b in liq[-28:-4])/24*4) if len(liq)>=28 else rec
            out["liq_spike"]=rec/base if base>0 else 0
    except Exception: pass
    try:
        fr=H("/funding-rate-history",sym,int(time.time())-10*24*3600,to,["c"])
        if fr: out["funding"]=fr[-1][0]
    except Exception: pass
    return out

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
    global TG_TOKEN,CA_KEY
    TG_TOKEN=os.environ.get("TG_TOKEN","").strip() or input("Токен бота: ").strip()
    CA_KEY=os.environ.get("CA_KEY","").strip() or input("Ключ Coinalyze: ").strip()
    if len(TG_TOKEN)<20 or len(CA_KEY)<10: print("Нет валидных TG_TOKEN/CA_KEY."); return
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
                                "/scan — искать лонг-сетапы\n/pos — мои позиции\n/log — журнал сделок\n\n"
                                "Подсвечу сетап → нажмёшь «Я вошёл» → буду вести позицию и комментировать. Решаешь ты.")
                elif text.startswith("/scan"): run_scan(cid)
                elif text.startswith("/pos"):
                    if POSITIONS: tg_send(cid,"Открытые: "+", ".join(POSITIONS))
                    else: tg_send(cid,"Открытых позиций нет.")
                elif text.startswith("/log"):
                    if os.path.exists(TRADES) and os.path.getsize(TRADES)>0:
                        n=sum(1 for _ in open(TRADES))-1
                        tg_send_doc(cid,TRADES,f"Журнал сделок: {n}. Сохрани — на сервере файл сбрасывается при передеплое.")
                    else: tg_send(cid,"Журнал пуст — ещё не было закрытых сделок.")
            # авто-скан
            if chat and time.time()-last_scan>SCAN_EVERY_MIN*60:
                run_scan(chat); last_scan=time.time()
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
