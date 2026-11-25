"""Kraken PUBLIC market data only. No keys, accounts, private paths or live broker."""
import json
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .core import Market, SYMBOLS, SafetyError, dec

PAIRS = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SafetyError("market feed redirect refused")


class PublicFeed:
    def request(self, operation, symbol):
        if operation not in ("OHLC", "Trades") or symbol not in SYMBOLS:
            raise SafetyError("only public OHLC/Trades GETs allowed; no live/private route")
        params = {"pair": PAIRS[symbol]}
        params.update({"interval": 60} if operation == "OHLC" else {"count": 1})
        url = "https://api.kraken.com/0/public/" + operation + "?" + urlencode(params)
        request = Request(url, headers={"User-Agent": "altare-local-paper/0.1"}, method="GET")
        try:
            with build_opener(NoRedirect()).open(request, timeout=10) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise SafetyError("oversize market response")
            data = json.loads(raw)
            if data["error"]:
                raise SafetyError("public feed API error")
            values = [v for k, v in data["result"].items() if k != "last"]
            if len(values) != 1:
                raise SafetyError("ambiguous public market result")
            return values[0]
        except (URLError, TimeoutError, ValueError, KeyError, TypeError, OSError) as exc:
            raise SafetyError("public feed unavailable or malformed; no fallback prices") from exc

    def snapshot(self):
        markets = {}
        try:
            for symbol in SYMBOLS:
                # Kraken's final OHLC entry is explicitly uncommitted: never use it.
                closed = self.request("OHLC", symbol)[:-1][-60:]
                if len(closed) < 30:
                    raise SafetyError("not enough completed candles")
                stamps = [int(row[0]) for row in closed]
                if any(b - a != 3600 for a, b in zip(stamps, stamps[1:])):
                    raise SafetyError("closed candle gap or duplicate")
                trade = self.request("Trades", symbol)[-1]
                markets[symbol] = Market(symbol, str(dec(trade[0])), float(dec(trade[2])),
                                         stamps[-1], tuple(str(dec(r[4])) for r in closed))
        except (IndexError, ValueError, TypeError, KeyError) as exc:
            raise SafetyError("malformed public market data") from exc
        return markets
