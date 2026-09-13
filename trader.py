"""Bybit V5 trading client — long/short, SL MarkPrice, trail, partial."""
from __future__ import annotations
import hashlib, hmac, json, logging, math, time
from typing import Optional
import aiohttp
from config import SL_TRIGGER

log = logging.getLogger("trader")


class BybitTrader:
    def __init__(self, key, secret, base_url="https://api-demo.bybit.com", session: Optional[aiohttp.ClientSession] = None):
        self.key, self.secret, self.base = key, secret, base_url.rstrip("/")
        self._session = session  # shared session from bot.main (optional)
        self._inst = {}
        self._mode = None
        self._owns_session = session is None

    def _sign(self, ts, payload):
        return hmac.new(self.secret.encode(), f"{ts}{self.key}5000{payload}".encode(), hashlib.sha256).hexdigest()

    async def _session_ctx(self):
        """Yield a session: shared if provided, else temporary."""
        if self._session is not None and not self._session.closed:
            return self._session, False  # shared, do not close
        return aiohttp.ClientSession(), True  # own, must close

    async def _req(self, method, path, params=None):
        params = params or {}
        ts = str(int(time.time() * 1000))
        s, own = await self._session_ctx()
        try:
            if method == "GET":
                q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                headers = {
                    "X-BAPI-API-KEY": self.key, "X-BAPI-SIGN": self._sign(ts, q),
                    "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": "5000",
                }
                url = f"{self.base}{path}" + (f"?{q}" if q else "")
                async with s.get(url, headers=headers, timeout=15) as r:
                    return await r.json(content_type=None)
            body = json.dumps(params, separators=(",", ":"))
            headers = {
                "X-BAPI-API-KEY": self.key, "X-BAPI-SIGN": self._sign(ts, body),
                "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": "5000",
                "Content-Type": "application/json",
            }
            async with s.post(f"{self.base}{path}", data=body, headers=headers, timeout=15) as r:
                return await r.json(content_type=None)
        except Exception as e:
            log.error(f"{method} {path}: {e}")
            return {"retCode": -1, "retMsg": str(e)}
        finally:
            if own:
                await s.close()

    async def balance(self) -> Optional[float]:
        r = await self._req("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        if r.get("retCode") != 0:
            return None
        try:
            for c in r["result"]["list"][0].get("coin", []):
                if c.get("coin") == "USDT":
                    return float(c.get("walletBalance") or 0)
        except Exception:
            return None
        return None

    async def instruments(self):
        if self._inst:
            return self._inst
        s, own = await self._session_ctx()
        try:
            cursor = ""
            while True:
                p = {"category": "linear", "limit": 1000}
                if cursor:
                    p["cursor"] = cursor
                async with s.get(f"{self.base}/v5/market/instruments-info", params=p, timeout=15) as r:
                    data = await r.json(content_type=None)
                for i in data.get("result", {}).get("list", []):
                    if not i.get("symbol", "").endswith("USDT"):
                        continue
                    self._inst[i["symbol"]] = {
                        "qty_step": float(i["lotSizeFilter"]["qtyStep"]),
                        "min_qty": float(i["lotSizeFilter"]["minOrderQty"]),
                        "tick": float(i["priceFilter"]["tickSize"]),
                        "max_lev": float(i["leverageFilter"]["maxLeverage"]),
                    }
                cursor = data.get("result", {}).get("nextPageCursor", "")
                if not cursor:
                    break
        finally:
            if own:
                await s.close()
        return self._inst

    async def ensure_mode(self):
        if self._mode is not None:
            return self._mode
        r = await self._req("POST", "/v5/position/switch-mode", {"category": "linear", "coin": "USDT", "mode": 0})
        self._mode = 0 if r.get("retCode") in (0, 110025) else 3
        return self._mode

    def idx(self, side="Buy"):
        return (1 if side == "Buy" else 2) if self._mode == 3 else 0

    @staticmethod
    def _rq(q, step):
        return math.floor(q / step) * step if step else q

    @staticmethod
    def _rp(p, tick):
        return round(round(p / tick) * tick, 10) if tick else p

    @staticmethod
    def _fmt(v, step):
        d = 0 if step >= 1 else max(0, -int(math.floor(math.log10(step))))
        return f"{v:.{d}f}"

    async def last_price(self, symbol) -> Optional[float]:
        s, own = await self._session_ctx()
        try:
            async with s.get(f"{self.base}/v5/market/tickers", params={"category": "linear", "symbol": symbol}, timeout=10) as r:
                d = await r.json(content_type=None)
            try:
                return float(d["result"]["list"][0]["lastPrice"])
            except Exception:
                return None
        finally:
            if own:
                await s.close()

    async def set_leverage(self, symbol, lev):
        await self._req("POST", "/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol,
            "buyLeverage": str(int(lev)), "sellLeverage": str(int(lev)),
        })

    async def positions(self, symbol=None):
        p = {"category": "linear", "settleCoin": "USDT"}
        if symbol:
            p["symbol"] = symbol
        r = await self._req("GET", "/v5/position/list", p)
        out = []
        for x in r.get("result", {}).get("list", []) if r.get("retCode") == 0 else []:
            try:
                sz = float(x.get("size") or 0)
                if sz > 0:
                    out.append({
                        "symbol": x["symbol"], "side": x["side"], "size": sz,
                        "entry_price": float(x.get("avgPrice") or 0),
                        "mark_price": float(x.get("markPrice") or 0),
                        "stop_loss": x.get("stopLoss") or "",
                    })
            except Exception:
                pass
        return out

    async def open_market(self, symbol, side, size_usd, sl_price, lev, tp_price=None):
        inst = (await self.instruments()).get(symbol)
        if not inst:
            return {"ok": False, "error": "no instrument"}
        px = await self.last_price(symbol)
        if not px:
            return {"ok": False, "error": "no price"}
        qty = self._rq(size_usd / px, inst["qty_step"])
        if qty < inst["min_qty"]:
            return {"ok": False, "error": f"qty {qty} < min"}
        lev = min(float(lev), inst["max_lev"])
        await self.set_leverage(symbol, lev)
        await self.ensure_mode()
        sl = self._rp(sl_price, inst["tick"])
        params = {
            "category": "linear", "symbol": symbol, "side": side,
            "orderType": "Market", "qty": self._fmt(qty, inst["qty_step"]),
            "stopLoss": self._fmt(sl, inst["tick"]),
            "tpslMode": "Full", "slOrderType": "Market",
            "slTriggerBy": SL_TRIGGER, "positionIdx": self.idx(side),
        }
        if tp_price:
            tp = self._rp(tp_price, inst["tick"])
            params["takeProfit"] = self._fmt(tp, inst["tick"])
            params["tpOrderType"] = "Market"
            params["tpTriggerBy"] = "LastPrice"
        r = await self._req("POST", "/v5/order/create", params)
        if r.get("retCode") != 0:
            return {"ok": False, "error": r.get("retMsg"), "code": r.get("retCode")}
        return {"ok": True, "qty": qty, "sl": sl, "tp": tp_price, "leverage": lev}

    async def set_sl(self, symbol, sl_price, side="Buy"):
        inst = (await self.instruments()).get(symbol)
        if not inst:
            return {"ok": False}
        await self.ensure_mode()
        sl = self._rp(sl_price, inst["tick"])
        r = await self._req("POST", "/v5/position/trading-stop", {
            "category": "linear", "symbol": symbol,
            "stopLoss": self._fmt(sl, inst["tick"]),
            "tpslMode": "Full", "slTriggerBy": SL_TRIGGER,
            "positionIdx": self.idx(side),
        })
        return {"ok": r.get("retCode") in (0, 34040), "sl": sl}

    async def set_trail(self, symbol, trail_pct, side="Buy"):
        inst = (await self.instruments()).get(symbol)
        pos = await self.positions(symbol)
        if not inst or not pos:
            return {"ok": False}
        mark = pos[0]["mark_price"] or pos[0]["entry_price"]
        dist = self._rp(mark * trail_pct / 100, inst["tick"])
        await self.ensure_mode()
        r = await self._req("POST", "/v5/position/trading-stop", {
            "category": "linear", "symbol": symbol,
            "trailingStop": self._fmt(dist, inst["tick"]),
            "tpslMode": "Full",
            "positionIdx": self.idx(side),
        })
        return {"ok": r.get("retCode") in (0, 34040)}

    async def close(self, symbol, side="Buy"):
        pos = await self.positions(symbol)
        if not pos:
            return {"ok": True}
        inst = (await self.instruments()).get(symbol)
        close_side = "Sell" if pos[0]["side"] == "Buy" else "Buy"
        await self.ensure_mode()
        r = await self._req("POST", "/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": close_side,
            "orderType": "Market", "qty": self._fmt(pos[0]["size"], inst["qty_step"]),
            "reduceOnly": True, "positionIdx": self.idx(pos[0]["side"]),
        })
        return {"ok": r.get("retCode") == 0}

    async def partial(self, symbol, pct=50):
        pos = await self.positions(symbol)
        if not pos:
            return {"ok": False}
        inst = (await self.instruments()).get(symbol)
        qty = self._rq(pos[0]["size"] * pct / 100, inst["qty_step"])
        if qty < inst["min_qty"]:
            return {"ok": False}
        close_side = "Sell" if pos[0]["side"] == "Buy" else "Buy"
        await self.ensure_mode()
        r = await self._req("POST", "/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": close_side,
            "orderType": "Market", "qty": self._fmt(qty, inst["qty_step"]),
            "reduceOnly": True, "positionIdx": self.idx(pos[0]["side"]),
        })
        return {"ok": r.get("retCode") == 0}

    async def has_sl(self, symbol) -> bool:
        r = await self._req("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
        for p in r.get("result", {}).get("list", []) if r.get("retCode") == 0 else []:
            if float(p.get("size") or 0) > 0:
                sl = p.get("stopLoss") or ""
                return sl not in ("", "0", 0)
        return False

    async def closed_pnl(self, symbol, limit=10):
        r = await self._req("GET", "/v5/position/closed-pnl", {"category": "linear", "symbol": symbol, "limit": str(limit)})
        return r.get("result", {}).get("list", []) if r.get("retCode") == 0 else []
