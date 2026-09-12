"""Minimal JSON state."""
import json, os, time, csv, logging
log = logging.getLogger("storage")


class State:
    def __init__(self, path: str):
        self.path = path
        self.data = {
            "enabled": False,
            "daily_pnl": 0.0,
            "daily_date": "",
            "consec_losses": 0,
            "blocked_until": 0,
            "blocked_reason": "",
            "positions": {},
            "cooldown": {},
        }
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self.data.update(json.load(f))
            except Exception as e:
                log.error(e)

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            log.error(e)

    def enabled(self): return self.data.get("enabled", False)
    def set_enabled(self, v): self.data["enabled"] = v; self.save()
    def blocked(self): return time.time() < self.data.get("blocked_until", 0)
    def block(self, reason, hours=24):
        self.data["blocked_until"] = time.time() + hours * 3600
        self.data["blocked_reason"] = reason
        self.save()
    def unblock(self):
        self.data["blocked_until"] = 0
        self.data["blocked_reason"] = ""
        self.data["consec_losses"] = 0
        self.save()

    def reset_day(self, today):
        if self.data.get("daily_date") != today:
            self.data["daily_pnl"] = 0.0
            self.data["daily_date"] = today
            self.save()

    def add_pnl(self, x):
        self.data["daily_pnl"] = self.data.get("daily_pnl", 0) + x
        if x < 0:
            self.data["consec_losses"] = self.data.get("consec_losses", 0) + 1
        elif x > 0:
            self.data["consec_losses"] = 0
        self.save()

    @property
    def positions(self): return self.data.setdefault("positions", {})

    def add_pos(self, symbol, **kw):
        self.positions[symbol] = {**kw, "opened_at": time.time()}
        self.save()

    def remove_pos(self, symbol):
        self.positions.pop(symbol, None)
        self.save()

    def cool(self, symbol, hours):
        self.data.setdefault("cooldown", {})[symbol] = time.time() + hours * 3600
        self.save()

    def is_cool(self, symbol):
        cd = self.data.get("cooldown", {})
        now = time.time()
        expired = [s for s, t in cd.items() if t <= now]
        for s in expired:
            del cd[s]
        if expired:
            self.save()
        return symbol in cd


def append_csv(path, row: dict):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()), extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log.warning(e)
