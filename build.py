#!/usr/bin/env python3
"""產生合約數據雷達的靜態頁。

行情來自 Ourbit 公開合約介面。讀取失敗時不覆蓋既有頁面與快照。
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import statistics
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
API = "https://futures.ourbit.com"
SITE_ORIGIN = "https://kalilkai.github.io/perp-radar"
OURBIT_URL = "https://www.ourbit.com/zh-TW/register?inviteCode=Ourbit666"
TV_URL = "https://www.tradingview.com/kalilkalilkai?aff_id=1174690"
X_URL = "https://x.com/kalilkalilkai"
DISCLOSURE = "透過連結註冊我會拿返佣"
DISCLAIMER = "數據僅供參考，不構成投資建議。"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TOP_PAGES = 150
TURNOVER_TOP_N = 40
OI_TOP_N = 40
MOVER_ABS = 0.08
PREMIUM_ABS = 0.003
FUNDING_FRACTION = 0.10
OI_CHANGE_ABS = 0.20
OI_CHANGE_MIN_NOTIONAL = 50_000
KLINE_KEEP = 120
CLOSE_TABLE = 30
FUNDING_KEEP = 12
SNAPSHOT_KEEP_DAYS = 14
SAFE_SYMBOL = re.compile(r"^[A-Z0-9_]+$")
STABLE_BASES = {
    "USDT",
    "USDC",
    "USD",
    "DAI",
    "TUSD",
    "FDUSD",
    "USDE",
    "USDS",
    "USD1",
    "USDEX",
    "BUSD",
    "USDD",
    "PYUSD",
    "USDP",
    "GUSD",
    "FRAX",
    "LUSD",
    "USDY",
    "USDG",
}

try:
    TAIPEI = ZoneInfo("Asia/Taipei")
except Exception:
    TAIPEI = timezone(timedelta(hours=8))


class FetchError(RuntimeError):
    pass


class CheckError(RuntimeError):
    pass


class Limiter:
    def __init__(self, per_sec: float) -> None:
        self.interval = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = now if self.next_at < now else self.next_at
            self.next_at = start + self.interval
            delay = start - now
        if delay > 0:
            time.sleep(delay)


LIMITER = Limiter(5)


def esc(value: object) -> str:
    return escape(str(value), quote=True)


def fnum(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def trim_number(text: str) -> str:
    if "." not in text:
        return text
    whole, frac = text.split(".", 1)
    frac = frac.rstrip("0")
    return whole if not frac else f"{whole}.{frac}"


def fmt_price(value: float | None, scale: int | None) -> str:
    if value is None:
        return "—"
    digits = 8 if scale is None else max(0, min(int(scale), 10))
    return trim_number(f"{value:,.{digits}f}")


def fmt_qty(value: float | None) -> str:
    if value is None:
        return "—"
    magnitude = abs(value)
    if magnitude >= 1000:
        return trim_number(f"{value:,.2f}")
    if magnitude >= 1:
        return trim_number(f"{value:,.4f}")
    return trim_number(f"{value:.8f}")


def fmt_int(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}"


def fmt_usdt(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1e8:
        body = trim_number(f"{magnitude / 1e8:.2f}") + " 億"
    elif magnitude >= 1e4:
        body = trim_number(f"{magnitude / 1e4:.2f}") + " 萬"
    else:
        body = trim_number(f"{magnitude:,.2f}")
    return sign + body


def fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    if abs(value) < 5e-7:
        return "0%"
    sign = "+" if value > 0 else "−"
    return sign + trim_number(f"{abs(value) * 100:.{digits}f}") + "%"


def fmt_fee(value: float | None) -> str:
    if value is None:
        return "—"
    return trim_number(f"{value * 100:.4f}") + "%"


def tone(value: float | None) -> str:
    if value is None or abs(value) < 1e-12:
        return "flat"
    return "up" if value > 0 else "down"


def attr_num(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.10g}"


def cycle_text(hours: int | None) -> str:
    if hours is None:
        return "—"
    return f"每 {hours} 小時"


def fmt_ms(ms: object) -> str:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return "—"
    try:
        moment = datetime.fromtimestamp(ms / 1000, TAIPEI)
    except (OverflowError, OSError, ValueError):
        return "—"
    return moment.strftime("%Y-%m-%d %H:%M")


def settle_short(ms: object) -> str:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return ""
    try:
        moment = datetime.fromtimestamp(ms / 1000, TAIPEI)
    except (OverflowError, OSError, ValueError):
        return ""
    return "結算 " + moment.strftime("%m-%d %H:%M")


def utc_day(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")


def premium_word(value: float | None) -> str:
    if value is None:
        return "暫無"
    if value >= 0.0005:
        return "溢價"
    if value <= -0.0005:
        return "折價"
    return "兩者接近"


def premium_cell(value: float | None) -> str:
    text = fmt_pct(value, 2)
    if value is None or abs(value) < 0.0005:
        return text
    return text + " " + premium_word(value)


def oi_qty_change(previous: float | None, current: float | None) -> float | None:
    if previous is None or current is None or previous <= 0:
        return None
    return (current - previous) / previous


def same_size(left: float, right: float) -> bool:
    return abs(left - right) <= max(1e-12, abs(left) * 1e-6)


def previous_point(snapshot: dict, symbol: str, today: str) -> tuple[str | None, dict | None]:
    dates = [
        day
        for day in snapshot.get("dates", [])
        if isinstance(day, str) and day < today
    ]
    dates.sort()
    if not dates:
        return None, None
    prev = dates[-1]
    day_map = (snapshot.get("series") or {}).get(prev) or {}
    point = day_map.get(symbol)
    if not isinstance(point, dict):
        return prev, None
    return prev, point


def fetch_bytes(url: str, timeout: int = 40, attempts: int = 3) -> bytes:
    last: Exception | None = None
    path = urllib.parse.urlparse(url).path
    for attempt in range(attempts):
        LIMITER.wait()
        request = urllib.request.Request(
            url,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    last = FetchError(f"HTTP {response.status} {path}")
                else:
                    return response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            last = exc
            if exc.code not in (403, 408, 429, 500, 502, 503, 504):
                raise FetchError(f"HTTP {exc.code} {path}") from exc
        except urllib.error.URLError as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"無法取得 {path}") from last


def fetch_json(url: str) -> object:
    path = urllib.parse.urlparse(url).path
    try:
        payload = json.loads(fetch_bytes(url))
    except json.JSONDecodeError as exc:
        raise FetchError(f"回應不是 JSON {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("success") is not True
        or payload.get("code") not in (0, "0")
    ):
        raise FetchError(f"行情回應異常 {path}")
    return payload.get("data")


def cached_json(key: str, loader) -> object:
    path = ROOT / ".cache" / f"{key}.json"
    if path.exists() and time.time() - path.stat().st_mtime < 18 * 3600:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    data = loader()
    if isinstance(data, list) and data:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError:
            pass
    return data


def parse_kline(data: object) -> list[dict]:
    if not isinstance(data, dict):
        return []
    times = data.get("time") or []
    closes = data.get("close") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    count = min(len(times), len(closes), len(highs), len(lows))
    points = []
    for index in range(count):
        try:
            stamp = int(times[index])
            close = float(closes[index])
            high = float(highs[index])
            low = float(lows[index])
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        points.append({"t": stamp, "c": close, "h": high, "l": low})
    if len(points) > KLINE_KEEP:
        points = points[-KLINE_KEEP:]
    return points


def parse_funding_history(data: object) -> list[dict]:
    if not isinstance(data, dict):
        return []
    rows = []
    for item in data.get("resultList") or []:
        if not isinstance(item, dict):
            continue
        rate = fnum(item.get("fundingRate"))
        settle = item.get("settleTime")
        if rate is None or not isinstance(settle, (int, float)):
            continue
        rows.append(
            {
                "rate": rate,
                "settle": int(settle),
                "cycle": as_int(item.get("collectCycle")),
            }
        )
    rows.sort(key=lambda row: row["settle"], reverse=True)
    return rows[:FUNDING_KEEP]


def load_kline(symbol: str) -> list[dict]:
    url = f"{API}/api/v1/contract/kline/{urllib.parse.quote(symbol)}?interval=Day1"

    def loader() -> list[dict]:
        return parse_kline(fetch_json(url))

    data = cached_json(f"kline-Day1-{symbol}", loader)
    return data if isinstance(data, list) else []


def load_funding_history(symbol: str) -> list[dict]:
    query = urllib.parse.urlencode(
        {"symbol": symbol, "page_num": 1, "page_size": FUNDING_KEEP}
    )
    url = f"{API}/api/v1/contract/funding_rate/history?{query}"

    def loader() -> list[dict]:
        return parse_funding_history(fetch_json(url))

    data = cached_json(f"fundhist-{symbol}", loader)
    return data if isinstance(data, list) else []


def load_snapshot() -> dict:
    path = ROOT / "data" / "oi-snapshot.json"
    empty = {"timezone": "Asia/Taipei", "dates": [], "series": {}}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("既有快照無法讀取，這次不計算持倉量變化。", flush=True)
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("dates", [])
    data.setdefault("series", {})
    return data


def snapshot_payload(previous: dict, today: str, rows: list[dict], generated_at: str) -> dict:
    series = dict(previous.get("series") or {})
    series[today] = {
        row["symbol"]: {
            "qty": round(row["qty"], 8),
            "notional": round(row["notional"], 2),
            "contractSize": row["contract_size"],
        }
        for row in rows
    }
    dates = sorted(day for day in series if isinstance(day, str))
    if len(dates) > SNAPSHOT_KEEP_DAYS:
        for day in dates[:-SNAPSHOT_KEEP_DAYS]:
            series.pop(day, None)
        dates = dates[-SNAPSHOT_KEEP_DAYS:]
    ordered = {}
    for day in dates:
        day_map = series.get(day) or {}
        ordered[day] = {symbol: day_map[symbol] for symbol in sorted(day_map)}
    return {
        "timezone": "Asia/Taipei",
        "generatedAt": generated_at,
        "dates": dates,
        "series": ordered,
    }


def write_snapshot(payload: dict) -> None:
    path = ROOT / "data" / "oi-snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def build_rows(tickers: list, funding_rows: list, details: list) -> list[dict]:
    funding = {
        item.get("symbol"): item
        for item in funding_rows
        if isinstance(item, dict) and item.get("symbol")
    }
    specs = {
        item.get("symbol"): item
        for item in details
        if isinstance(item, dict) and item.get("symbol")
    }
    rows = []
    dropped = 0
    for ticker in tickers:
        if not isinstance(ticker, dict):
            dropped += 1
            continue
        symbol = ticker.get("symbol")
        spec = specs.get(symbol)
        fund = funding.get(symbol)
        if not symbol or spec is None or fund is None:
            dropped += 1
            continue
        if spec.get("isHidden") or spec.get("state") not in (0, "0"):
            continue
        if spec.get("futureType") not in (1, "1"):
            continue
        if spec.get("quoteCoin") != "USDT" or spec.get("settleCoin") != "USDT":
            continue
        base = str(spec.get("baseCoin") or str(symbol).split("_")[0]).strip()
        if base.upper() in STABLE_BASES:
            continue
        last = fnum(ticker.get("lastPrice"))
        contract_size = fnum(spec.get("contractSize"))
        if last is None or last <= 0 or contract_size is None or contract_size <= 0:
            dropped += 1
            continue
        hold_vol = fnum(ticker.get("holdVol")) or 0.0
        amount = fnum(ticker.get("amount24")) or 0.0
        volume = fnum(ticker.get("volume24")) or 0.0
        rates = ticker.get("riseFallRates") if isinstance(ticker.get("riseFallRates"), dict) else {}
        index_price = fnum(ticker.get("indexPrice"))
        fair_price = fnum(ticker.get("fairPrice"))
        premium = None
        if index_price and index_price > 0 and fair_price is not None:
            premium = (fair_price - index_price) / index_price
        qty = hold_vol * contract_size
        rows.append(
            {
                "symbol": str(symbol),
                "base": base,
                "price_scale": as_int(spec.get("priceScale")),
                "last": last,
                "bid": fnum(ticker.get("bid1")),
                "ask": fnum(ticker.get("ask1")),
                "high24": fnum(ticker.get("high24Price")),
                "low24": fnum(ticker.get("lower24Price")),
                "amount": amount,
                "volume24": volume,
                "base_volume": volume * contract_size,
                "contract_size": contract_size,
                "hold_vol": hold_vol,
                "qty": qty,
                "notional": qty * last,
                "chg": fnum(ticker.get("riseFallRate")),
                "chg7": fnum(rates.get("r7")),
                "chg30": fnum(rates.get("r30")),
                "chg90": fnum(rates.get("r90")),
                "funding": fnum(fund.get("fundingRate")),
                "funding_max": fnum(fund.get("maxFundingRate")),
                "funding_min": fnum(fund.get("minFundingRate")),
                "cycle": as_int(fund.get("collectCycle")),
                "next_settle": fund.get("nextSettleTime"),
                "index": index_price,
                "fair": fair_price,
                "premium": premium,
                "is_new": bool(spec.get("isNew")),
                "is_hot": bool(spec.get("isHot")),
                "max_leverage": as_int(spec.get("maxLeverage")),
                "maker_fee": fnum(spec.get("makerFeeRate")),
                "taker_fee": fnum(spec.get("takerFeeRate")),
                "timestamp": ticker.get("timestamp"),
                "tags": [],
                "has_page": False,
                "kline": [],
                "funding_hist": [],
                "oi_chg": None,
            }
        )
    if dropped > 20:
        raise FetchError("可對上的合約過少，停止更新")
    if len(rows) < 100:
        raise FetchError("可列出的合約過少，停止更新")
    rows.sort(key=lambda row: (-row["amount"], row["symbol"]))
    return rows


def assert_magnitude(rows: list[dict]) -> None:
    ratios = []
    btc = next((row for row in rows if row["symbol"] == "BTC_USDT"), None)
    for row in rows:
        implied = row["volume24"] * row["contract_size"] * row["last"]
        if implied > 1000 and row["amount"] > 1000:
            ratios.append(row["amount"] / implied)
    if btc is None:
        raise FetchError("沒有 BTC_USDT，停止更新")
    if not (1000 <= btc["last"] <= 1_000_000):
        raise FetchError("BTC 價格數量級不合理，停止更新")
    if len(ratios) < 10:
        raise FetchError("成交額核對樣本不足，停止更新")
    median_ratio = statistics.median(ratios)
    btc_implied = btc["volume24"] * btc["contract_size"] * btc["last"]
    btc_ratio = btc["amount"] / btc_implied if btc_implied else 0
    print(f"成交額核對 中位數 {median_ratio:.3f} BTC {btc_ratio:.3f}", flush=True)
    if not (0.8 <= median_ratio <= 1.5) or not (0.5 <= btc_ratio <= 2):
        raise FetchError("每張數量的換算對不上成交額，停止更新")


def apply_tags(rows: list[dict], snapshot: dict, today: str) -> dict:
    prev_dates = [
        day
        for day in snapshot.get("dates", [])
        if isinstance(day, str) and day < today
    ]
    prev_date = max(prev_dates) if prev_dates else None
    for row in rows:
        _, point = previous_point(snapshot, row["symbol"], today)
        row["oi_chg"] = None
        if point is not None:
            prev_qty = fnum(point.get("qty"))
            prev_size = fnum(point.get("contractSize"))
            if prev_size is not None and same_size(prev_size, row["contract_size"]):
                row["oi_chg"] = oi_qty_change(prev_qty, row["qty"])
            point_notional = fnum(point.get("notional")) or 0.0
        else:
            point_notional = 0.0
        row["prev_notional"] = point_notional

    turnover = {row["symbol"] for row in rows[: min(TURNOVER_TOP_N, len(rows))]}
    by_oi = sorted(rows, key=lambda row: (-row["notional"], row["symbol"]))
    oi_top = {row["symbol"] for row in by_oi[: min(OI_TOP_N, len(by_oi))]}
    by_funding = sorted(rows, key=lambda row: (-abs(row["funding"] or 0), row["symbol"]))
    funding_n = max(1, math.ceil(len(by_funding) * FUNDING_FRACTION))
    funding_cutoff = abs(by_funding[funding_n - 1]["funding"] or 0)
    if funding_cutoff <= 0:
        funding_set = {row["symbol"] for row in by_funding[:funding_n]}
    else:
        funding_set = {
            row["symbol"]
            for row in by_funding
            if abs(row["funding"] or 0) >= funding_cutoff - 1e-15
        }
    for index, row in enumerate(rows, start=1):
        row["amount_rank"] = index
    for index, row in enumerate(by_oi, start=1):
        row["oi_rank"] = index

    for row in rows:
        tags = []
        if row["symbol"] in turnover:
            tags.append("turnover")
        if row["chg"] is not None and abs(row["chg"]) >= MOVER_ABS:
            tags.append("mover")
        if row["symbol"] in funding_set:
            tags.append("funding")
        change_hit = False
        if row["oi_chg"] is not None and abs(row["oi_chg"]) >= OI_CHANGE_ABS:
            if max(row["prev_notional"], row["notional"]) >= OI_CHANGE_MIN_NOTIONAL:
                change_hit = True
        if row["symbol"] in oi_top or change_hit:
            tags.append("oi")
        if row["premium"] is not None and abs(row["premium"]) >= PREMIUM_ABS:
            tags.append("premium")
        row["tags"] = tags

    page_rows = [row for row in rows if SAFE_SYMBOL.fullmatch(row["symbol"])][:TOP_PAGES]
    for row in page_rows:
        row["has_page"] = True
    nth_amount = rows[min(TURNOVER_TOP_N, len(rows)) - 1]
    nth_oi = by_oi[min(OI_TOP_N, len(by_oi)) - 1]
    return {
        "prev_date": prev_date,
        "turnover_n": min(TURNOVER_TOP_N, len(rows)),
        "oi_n": min(OI_TOP_N, len(by_oi)),
        "turnover_floor": fmt_usdt(nth_amount["amount"]),
        "oi_floor": fmt_usdt(nth_oi["notional"]),
        "funding_cutoff": fmt_pct(funding_cutoff, 4),
        "funding_cutoff_value": funding_cutoff,
        "page_n": len(page_rows),
        "counts": {
            "all": len(rows),
            "turnover": sum("turnover" in row["tags"] for row in rows),
            "mover": sum("mover" in row["tags"] for row in rows),
            "funding": sum("funding" in row["tags"] for row in rows),
            "oi": sum("oi" in row["tags"] for row in rows),
            "premium": sum("premium" in row["tags"] for row in rows),
        },
    }


def attach_series(rows: list[dict]) -> None:
    targets = [row for row in rows if row["has_page"]]

    def one(row: dict) -> tuple[str, list, list]:
        symbol = row["symbol"]
        try:
            kline = load_kline(symbol)
        except Exception as exc:
            print(f"日線失敗 {symbol}: {exc}", flush=True)
            kline = []
        try:
            history = load_funding_history(symbol)
        except Exception as exc:
            print(f"資金費率紀錄失敗 {symbol}: {exc}", flush=True)
            history = []
        return symbol, kline, history

    found: dict[str, tuple[list, list]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(one, row) for row in targets]
        for future in as_completed(futures):
            symbol, kline, history = future.result()
            found[symbol] = (kline, history)
            done += 1
            if done % 25 == 0 or done == len(targets):
                print(f"單幣資料 {done}/{len(targets)}", flush=True)
    for row in targets:
        kline, history = found.get(row["symbol"], ([], []))
        row["kline"] = kline
        row["funding_hist"] = history
    missing = sum(1 for row in targets if not row["kline"])
    print(f"日線缺少 {missing}", flush=True)
    btc = next((row for row in targets if row["symbol"] == "BTC_USDT"), None)
    if btc is None or not btc["kline"]:
        raise FetchError("BTC 日線是空的，停止更新")
    if targets and missing > len(targets) / 2:
        raise FetchError("日線缺少過多，停止更新")


def chart_svg(points: list[dict], label: str) -> str:
    if len(points) < 2:
        return ""
    width, height, pad = 720, 220, 8
    low = min(point["l"] for point in points)
    high = max(point["h"] for point in points)
    if high <= low:
        high = low + abs(low) * 0.01 + 1e-9

    def x_of(index: int) -> float:
        return pad + (width - 2 * pad) * index / (len(points) - 1)

    def y_of(value: float) -> float:
        return pad + (height - 2 * pad) * (1 - (value - low) / (high - low))

    upper = [f"{x_of(index):.1f},{y_of(point['h']):.1f}" for index, point in enumerate(points)]
    lower = [
        f"{x_of(index):.1f},{y_of(points[index]['l']):.1f}"
        for index in range(len(points) - 1, -1, -1)
    ]
    line = " ".join(
        f"{x_of(index):.1f},{y_of(point['c']):.1f}" for index, point in enumerate(points)
    )
    polygon = " ".join(upper + lower)
    return (
        f'<svg viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{esc(label)}"><title>{esc(label)}</title>'
        f'<polygon points="{polygon}" fill="#e4d3ae"/>'
        f'<polyline points="{line}" fill="none" stroke="#0f5c5c" stroke-width="2.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


def json_ld(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/ld+json">{raw}</script>'


def shell(
    *,
    title: str,
    description: str,
    canonical: str,
    body: str,
    depth: int,
    current: str,
    when_text: str,
    when_iso: str,
    extra_json: str = "",
    script: bool = False,
) -> str:
    prefix = "../" * depth
    index_href = f"{prefix}index.html"
    about_href = f"{prefix}about.html"
    style_href = f"{prefix}assets/style.css"
    index_current = ' aria-current="page"' if current == "index" else ""
    about_current = ' aria-current="page"' if current == "about" else ""
    script_tag = f'<script src="{prefix}assets/table.js"></script>' if script else ""
    icon = "data:image/svg+xml," + urllib.parse.quote(
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' rx='6' fill='#163e3e'/>"
        "<circle cx='16' cy='16' r='8' fill='none' stroke='#f4f0e6' stroke-width='2'/>"
        "<circle cx='16' cy='16' r='2' fill='#e2b657'/></svg>"
    )
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{esc(canonical)}">
<link rel="alternate" hreflang="zh-Hant" href="{esc(canonical)}">
<meta property="og:locale" content="zh_TW">
<meta property="og:type" content="website">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(canonical)}">
<link rel="icon" href="{icon}">
<link rel="stylesheet" href="{style_href}">
{extra_json}
</head>
<body>
<a class="skip" href="#main">跳到內容</a>
<div class="wrap">
<header class="top">
<div class="top-row">
<a class="brand" href="{index_href}">合約數據雷達</a>
<nav class="nav" aria-label="頁面">
<a href="{index_href}"{index_current}>總表</a>
<a href="{about_href}"{about_current}>關於與揭露</a>
</nav>
<div class="btns">
<a class="btn" href="{OURBIT_URL}" rel="sponsored noopener noreferrer" referrerpolicy="no-referrer">前往 Ourbit</a>
<a class="btn alt" href="{TV_URL}" rel="sponsored noopener noreferrer" referrerpolicy="no-referrer">前往 TradingView</a>
</div>
</div>
<p class="fine">{DISCLOSURE}。{DISCLAIMER}</p>
</header>
<main id="main">
{body}
</main>
<footer class="site-footer">
<p>{DISCLOSURE}。</p>
<p>{DISCLAIMER}</p>
<div class="btns">
<a class="btn" href="{OURBIT_URL}" rel="sponsored noopener noreferrer" referrerpolicy="no-referrer">前往 Ourbit</a>
<a class="btn alt" href="{TV_URL}" rel="sponsored noopener noreferrer" referrerpolicy="no-referrer">前往 TradingView</a>
</div>
<p class="fine">資料時間 <time datetime="{esc(when_iso)}">{esc(when_text)}</time></p>
<p class="fine">整理：<a href="{X_URL}" rel="noopener noreferrer" referrerpolicy="no-referrer">@kalilkalilkai</a></p>
</footer>
</div>
{script_tag}
</body>
</html>
"""


def stats_html(items: list[tuple[str, str, str | None]]) -> str:
    parts = ['<div class="stats">']
    for label, value, tone_name in items:
        klass = f' class="{tone_name}"' if tone_name else ""
        parts.append(
            f'<div class="stat"><span class="k">{esc(label)}</span>'
            f'<span class="v"{klass}>{esc(value)}</span></div>'
        )
    parts.append("</div>")
    return "".join(parts)


def row_html(row: dict) -> str:
    if row["has_page"]:
        name = f'<a href="coins/{esc(row["symbol"])}.html">{esc(row["base"])}</a>'
    else:
        name = f'<span class="name">{esc(row["base"])}</span>'
    badges = ""
    if row["is_new"]:
        badges += '<span class="tag">新</span>'
    if row["is_hot"]:
        badges += '<span class="tag">熱</span>'
    settle = settle_short(row["next_settle"])
    settle_html = f'<span class="sub">{esc(settle)}</span>' if settle else ""
    return (
        f'<tr data-pair="{esc(row["symbol"])}" data-base="{esc(row["base"])}" '
        f'data-last="{attr_num(row["last"])}" data-chg="{attr_num(row["chg"])}" '
        f'data-chg7="{attr_num(row["chg7"])}" data-amt="{attr_num(row["amount"])}" '
        f'data-fund="{attr_num(row["funding"])}" data-oi="{attr_num(row["notional"])}" '
        f'data-oichg="{attr_num(row["oi_chg"])}" data-prem="{attr_num(row["premium"])}" '
        f'data-tags="{" ".join(row["tags"])}">'
        f"<td>{name}{badges}<span class=\"pair\">{esc(row['symbol'])}</span></td>"
        f'<td class="num">{esc(fmt_price(row["last"], row["price_scale"]))}</td>'
        f'<td class="num {tone(row["chg"])}">{esc(fmt_pct(row["chg"]))}</td>'
        f'<td class="num {tone(row["chg7"])}">{esc(fmt_pct(row["chg7"]))}</td>'
        f'<td class="num">{esc(fmt_usdt(row["amount"]))}</td>'
        f'<td class="num">{esc(fmt_pct(row["funding"], 4))}'
        f'<span class="sub">{esc(cycle_text(row["cycle"]))}</span>{settle_html}</td>'
        f'<td class="num">{esc(fmt_usdt(row["notional"]))}</td>'
        f'<td class="num">{esc(fmt_pct(row["oi_chg"]))}</td>'
        f'<td class="num">{esc(premium_cell(row["premium"]))}</td>'
        "</tr>"
    )


def index_page(rows: list[dict], ctx: dict) -> str:
    counts = ctx["counts"]
    filters = [
        ("all", "全部", counts["all"]),
        ("turnover", "成交額前段", counts["turnover"]),
        ("mover", "波動較大", counts["mover"]),
        ("funding", "資金費率偏離", counts["funding"]),
        ("oi", "持倉規模或變化", counts["oi"]),
        ("premium", "指數價差", counts["premium"]),
    ]
    buttons = []
    for key, label, count in filters:
        pressed = "true" if key == "all" else "false"
        buttons.append(
            f'<button type="button" data-filter="{key}" data-label="{esc(label)}" '
            f'aria-pressed="{pressed}">{esc(label)}<span>{count}</span></button>'
        )
    if ctx["prev_date"]:
        oi_note = (
            f"持倉量變化比較 {ctx['prev_date']} 的持倉數量（基礎幣，不含價格漲跌）。"
        )
    else:
        oi_note = "目前還沒有前次持倉快照，持倉量變化從下一次整理開始才有數字。"
    rules = f"""
<section class="rules">
<h2>五個數據範圍</h2>
<p>下面只列出目前落在這些數字範圍的合約，沒有進出場建議。</p>
<ol>
<li><strong>成交額前段</strong>：24 小時成交額排名前 {ctx["turnover_n"]}，第 {ctx["turnover_n"]} 名約 {esc(ctx["turnover_floor"])}。</li>
<li><strong>波動較大</strong>：24 小時漲跌的絕對值達到 8%。</li>
<li><strong>資金費率偏離</strong>：絕對資金費率位於當日全部合約的前 10%，門檻 {esc(ctx["funding_cutoff"])}。儲存格會標出收取週期與下次結算時間（台北）。</li>
<li><strong>持倉規模或變化</strong>：名義持倉排名前 {ctx["oi_n"]}，第 {ctx["oi_n"]} 名約 {esc(ctx["oi_floor"])}。若已有前次快照，前次或這次的名義持倉至少有一邊達到 5 萬 USDT，且持倉數量變動的絕對值達到 20%，也會列入。</li>
<li><strong>指數價差</strong>：合理價格相對指數價格的偏離絕對值達到 0.30%。正值是溢價，負值是折價。</li>
</ol>
</section>
"""
    body = f"""
<h1>合約數據雷達</h1>
<p class="lede">USDT 永續合約的成交額、漲跌、資金費率、持倉與指數價差。共 {counts["all"]} 個交易中的合約，資料時間 {esc(ctx["when_text"])}。</p>
{rules}
<div class="filters" role="group" aria-label="數據範圍">
{"".join(buttons)}
</div>
<p id="status">目前顯示全部：{counts["all"]} 個。</p>
<div class="search">
<label for="q">搜尋代碼</label>
<input id="q" type="search" placeholder="例如 BTC" autocomplete="off">
</div>
<noscript><p>篩選與排序需要瀏覽器腳本。未執行時，表格是全部合約，並依 24 小時成交額由高到低排列。</p></noscript>
<p class="fine scroll-hint">表格可以左右滑動。</p>
<div class="table-wrap">
<table id="market">
<caption>USDT 永續。漲跌顏色依台灣習慣，漲為紅、跌為綠。{esc(oi_note)}</caption>
<thead>
<tr>
<th scope="col"><button type="button" data-sort="base">合約</button></th>
<th scope="col" class="num"><button type="button" data-sort="last">最新價</button></th>
<th scope="col" class="num"><button type="button" data-sort="chg">24 小時漲跌</button></th>
<th scope="col" class="num"><button type="button" data-sort="chg7">7 日漲跌</button></th>
<th scope="col" class="num" aria-sort="descending"><button type="button" data-sort="amt">成交額</button></th>
<th scope="col" class="num"><button type="button" data-sort="fund">資金費率</button></th>
<th scope="col" class="num"><button type="button" data-sort="oi">名義持倉</button></th>
<th scope="col" class="num"><button type="button" data-sort="oichg">持倉量變化</button></th>
<th scope="col" class="num"><button type="button" data-sort="prem">指數價差</button></th>
</tr>
</thead>
<tbody>
{"".join(row_html(row) for row in rows)}
</tbody>
</table>
</div>
<p id="empty" hidden>這個範圍目前沒有合約。</p>
<p class="fine">單幣頁收錄 24 小時成交額前 {ctx["page_n"]} 名，其餘合約只列在這張表。</p>
<p class="fine">{esc(oi_note)} 名義持倉＝持倉張數 × 每張數量 × 最新價。</p>
"""
    description = (
        "整理 Ourbit 公開的 USDT 永續合約行情：最新價、24 小時成交額、資金費率、"
        "持倉名義與指數價差。數據僅供參考，不構成投資建議。"
    )
    linked = json_ld(
        {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "合約數據雷達",
            "url": SITE_ORIGIN + "/",
            "inLanguage": "zh-Hant",
            "description": description,
            "author": {"@type": "Person", "name": "@kalilkalilkai", "url": X_URL},
        }
    )
    return shell(
        title="合約數據雷達｜USDT 永續成交額、資金費率與持倉",
        description=description,
        canonical=SITE_ORIGIN + "/",
        body=body,
        depth=0,
        current="index",
        when_text=ctx["when_text"],
        when_iso=ctx["when_iso"],
        extra_json=linked,
        script=True,
    )


def about_page(ctx: dict) -> str:
    body = f"""
<h1>關於與揭露</h1>
<h2>這個頁面在做什麼</h2>
<p>合約數據雷達把 USDT 永續合約的公開行情整理成一張總表，以及 24 小時成交額前 {ctx["page_n"]} 名的單幣頁。可以依成交額、漲跌幅度、資金費率、持倉與指數價差，查看目前落在這些範圍的合約。</p>
<p>這些範圍只描述數字落在哪裡，沒有進出場建議。</p>
<h2>資料來源</h2>
<p>行情來自 Ourbit 公開的合約介面（futures.ourbit.com），讀取時不使用金鑰。總表使用全市場報價、資金費率與合約規格。單幣頁另外放入日線，以及近期的資金費率結算紀錄。</p>
<p>這個介面沒有提供持倉的歷史序列。名義持倉與持倉數量是整理當下的數字。「持倉量變化」拿這次的持倉數量，和本站前一次成功整理時留下的數量相比，比較的是幣的數量，不是名義金額。第一次還沒有可比較的紀錄時，該欄是空白。</p>
<p>數字是整理當下讀到的，不是連續更新。請以交易所畫面上的資料為準。合約介面沒有寫進現貨文件，欄位以後可能調整。</p>
<h2>沒有列入的合約</h2>
<p>只收錄交易中、且沒有被隱藏的 USDT 永續。穩定幣對穩定幣的合約不列入。</p>
<h2>返佣</h2>
<p>頁面上的 Ourbit 與 TradingView 按鈕是我的推薦連結。{DISCLOSURE}。</p>
<h2>請自行確認合法性</h2>
<p>如果你在台灣或其他地區使用，請自己確認該交易所在當地是否可以合法使用，以及相關風險。這裡不是法律意見，也不是開戶說明。</p>
<h2>誰整理的</h2>
<p><a href="{X_URL}" rel="noopener noreferrer" referrerpolicy="no-referrer">@kalilkalilkai</a></p>
"""
    description = "合約數據雷達的資料來源與返佣揭露。數據僅供參考，不構成投資建議。"
    return shell(
        title="關於與揭露｜合約數據雷達",
        description=description,
        canonical=SITE_ORIGIN + "/about.html",
        body=body,
        depth=0,
        current="about",
        when_text=ctx["when_text"],
        when_iso=ctx["when_iso"],
        extra_json=json_ld(
            {
                "@context": "https://schema.org",
                "@type": "AboutPage",
                "name": "關於與揭露",
                "url": SITE_ORIGIN + "/about.html",
                "inLanguage": "zh-Hant",
                "isPartOf": {"@type": "WebSite", "name": "合約數據雷達", "url": SITE_ORIGIN + "/"},
                "author": {"@type": "Person", "name": "@kalilkalilkai", "url": X_URL},
            }
        ),
    )


def coin_page(row: dict, ctx: dict) -> str:
    base = row["base"]
    price = fmt_price(row["last"], row["price_scale"])
    if row["oi_chg"] is not None and ctx["prev_date"]:
        oi_sentence = f"持倉數量較 {ctx['prev_date']} {fmt_pct(row['oi_chg'])}。"
    else:
        oi_sentence = "持倉數量尚無前次可比較的快照。"
    cycle = cycle_text(row["cycle"]) if row["cycle"] else "結算週期見交易所頁面"
    lead = (
        f"截至 {ctx['when_text']}，{base}/USDT 永續最新價 {price}，"
        f"24 小時漲跌 {fmt_pct(row['chg'])}，24 小時成交額 {fmt_usdt(row['amount'])}。"
        f"資金費率 {fmt_pct(row['funding'], 4)}，{cycle}。"
        f"名義持倉約 {fmt_usdt(row['notional'])}。{oi_sentence}"
        f"指數價差 {fmt_pct(row['premium'], 2)}（{premium_word(row['premium'])}）。"
    )
    contract = "https://futures.ourbit.com/zh-TW/exchange/" + urllib.parse.quote(row["symbol"])
    badges = ""
    if row["is_new"]:
        badges += ' <span class="tag">新</span>'
    if row["is_hot"]:
        badges += ' <span class="tag">熱</span>'
    points = row["kline"]
    if len(points) >= 2:
        chart_label = f"{base} 近 {len(points)} 個 UTC 日線收盤"
        hi = max(point["h"] for point in points)
        lo = min(point["l"] for point in points)
        chart = (
            f'<div class="chart">{chart_svg(points, chart_label)}</div>'
            f'<p class="fine">區間最高 {esc(fmt_price(hi, row["price_scale"]))}，'
            f'最低 {esc(fmt_price(lo, row["price_scale"]))}。'
            f'左 {esc(utc_day(points[0]["t"]))}，右 {esc(utc_day(points[-1]["t"]))}（UTC 日界）。'
            f"圖只呈現價格序列。</p>"
        )
    else:
        chart = "<p>這次沒有取到日線。</p>"
    recent = list(reversed(points[-CLOSE_TABLE:]))
    if recent:
        close_rows = []
        for point in recent:
            close_rows.append(
                "<tr>"
                f"<td>{esc(utc_day(point['t']))}</td>"
                f'<td class="num">{esc(fmt_price(point["c"], row["price_scale"]))}</td>'
                f'<td class="num">{esc(fmt_price(point["h"], row["price_scale"]))}</td>'
                f'<td class="num">{esc(fmt_price(point["l"], row["price_scale"]))}</td>'
                "</tr>"
            )
        close_table = (
            "<details><summary>最近 "
            f"{len(recent)} 個日線收盤</summary>"
            '<table class="plain"><thead><tr>'
            "<th>日期（UTC）</th><th class=\"num\">收盤</th>"
            "<th class=\"num\">最高</th><th class=\"num\">最低</th>"
            "</tr></thead><tbody>"
            + "".join(close_rows)
            + "</tbody></table></details>"
        )
    else:
        close_table = ""
    if row["funding_hist"]:
        fund_rows = []
        for item in row["funding_hist"]:
            fund_rows.append(
                "<tr>"
                f"<td>{esc(fmt_ms(item['settle']))}</td>"
                f'<td class="num">{esc(fmt_pct(item["rate"], 4))}</td>'
                f"<td>{esc(cycle_text(item['cycle']))}</td>"
                "</tr>"
            )
        funding_table = (
            '<table class="plain"><thead><tr>'
            "<th>結算時間（台北）</th><th class=\"num\">費率</th><th>週期</th>"
            "</tr></thead><tbody>"
            + "".join(fund_rows)
            + "</tbody></table>"
        )
    else:
        funding_table = "<p>這次沒有取到結算紀錄。</p>"
    leverage = f"{row['max_leverage']} 倍" if row["max_leverage"] else "—"
    body = f"""
<h1>{esc(base)} 永續合約{badges}</h1>
<p class="pair">{esc(row["symbol"])}</p>
<p class="lede">{esc(lead)}</p>
<p>24 小時成交額排名第 {row["amount_rank"]}，名義持倉排名第 {row["oi_rank"]}。</p>
<p><a href="{esc(contract)}" rel="noopener noreferrer" referrerpolicy="no-referrer">在 Ourbit 查看 {esc(base)}/USDT 永續</a></p>
<h2>價格</h2>
{stats_html([
    ("最新價", price, None),
    ("24 小時漲跌", fmt_pct(row["chg"]), tone(row["chg"])),
    ("7 日漲跌", fmt_pct(row["chg7"]), tone(row["chg7"])),
    ("30 日漲跌", fmt_pct(row["chg30"]), tone(row["chg30"])),
    ("90 日漲跌", fmt_pct(row["chg90"]), tone(row["chg90"])),
    ("24 小時高", fmt_price(row["high24"], row["price_scale"]), None),
    ("24 小時低", fmt_price(row["low24"], row["price_scale"]), None),
    ("買一", fmt_price(row["bid"], row["price_scale"]), None),
    ("賣一", fmt_price(row["ask"], row["price_scale"]), None),
])}
<p class="fine">7 日、30 日、90 日漲跌來自交易所以 UTC+8 統計的區間漲跌。</p>
<h2>成交與持倉</h2>
{stats_html([
    ("24 小時成交額", fmt_usdt(row["amount"]), None),
    ("24 小時成交量", fmt_qty(row["base_volume"]) + " " + base, None),
    ("持倉張數", fmt_int(row["hold_vol"]), None),
    ("每張數量", fmt_qty(row["contract_size"]) + " " + base, None),
    ("持倉數量", fmt_qty(row["qty"]) + " " + base, None),
    ("名義持倉", fmt_usdt(row["notional"]), None),
    ("持倉量變化", fmt_pct(row["oi_chg"]), None),
])}
<h2>資金費率與指數價差</h2>
{stats_html([
    ("資金費率", fmt_pct(row["funding"], 4), None),
    ("收取週期", cycle_text(row["cycle"]), None),
    ("下次結算（台北）", fmt_ms(row["next_settle"]), None),
    ("費率下限", fmt_pct(row["funding_min"], 4), None),
    ("費率上限", fmt_pct(row["funding_max"], 4), None),
    ("指數價格", fmt_price(row["index"], row["price_scale"]), None),
    ("合理價格", fmt_price(row["fair"], row["price_scale"]), None),
    ("指數價差", premium_cell(row["premium"]), None),
])}
<p class="fine">指數價差＝（合理價格 − 指數價格）÷ 指數價格。</p>
<h2>日線</h2>
<p class="fine">近端 UTC 日線的最高、最低與收盤。</p>
{chart}
{close_table}
<h2>近期資金費率結算</h2>
{funding_table}
<h2>合約規格</h2>
{stats_html([
    ("每張數量", fmt_qty(row["contract_size"]) + " " + base, None),
    ("交易所標示的槓桿上限", leverage, None),
    ("結算幣", "USDT", None),
    ("掛單費率", fmt_fee(row["maker_fee"]), None),
    ("吃單費率", fmt_fee(row["taker_fee"]), None),
])}
<p class="fine">費率是交易所公布的數字，這裡沒有試算部位或手續費。</p>
<h2>這些數字怎麼讀</h2>
<ul>
<li>最新價是最近一筆成交價。24 小時漲跌是近 24 小時的漲跌幅。</li>
<li>成交額是近 24 小時成交的名目金額，單位是 USDT。成交量是換成基礎幣之後的數量。</li>
<li>資金費率是到下次結算前公告的費率。正負號代表支付方向，結算規則以交易所頁面為準。</li>
<li>名義持倉用持倉張數乘每張數量，再乘最新價。持倉量變化只比較幣的數量。</li>
<li>溢價、折價只描述合理價格和指數價格的差距。</li>
</ul>
<p><a href="../index.html">回到總表</a></p>
"""
    title = f"{base} 永續合約｜資金費率、持倉與價格｜合約數據雷達"
    description = (
        f"{base}/USDT 永續最新價 {price}，24 小時 {fmt_pct(row['chg'])}，"
        f"成交額 {fmt_usdt(row['amount'])}。資金費率 {fmt_pct(row['funding'], 4)}，"
        f"名義持倉約 {fmt_usdt(row['notional'])}。資料來自 Ourbit 公開行情，僅供參考，不構成投資建議。"
    )
    canonical = f"{SITE_ORIGIN}/coins/{row['symbol']}.html"
    return shell(
        title=title,
        description=description,
        canonical=canonical,
        body=body,
        depth=1,
        current="coin",
        when_text=ctx["when_text"],
        when_iso=ctx["when_iso"],
        extra_json=json_ld(
            {
                "@context": "https://schema.org",
                "@type": "WebPage",
                "name": title,
                "url": canonical,
                "inLanguage": "zh-Hant",
                "description": description,
                "isPartOf": {"@type": "WebSite", "name": "合約數據雷達", "url": SITE_ORIGIN + "/"},
                "about": f"{base}/USDT 永續合約",
                "author": {"@type": "Person", "name": "@kalilkalilkai", "url": X_URL},
            }
        ),
    )


def sitemap(rows: list[dict], today: str) -> str:
    locs = [SITE_ORIGIN + "/", SITE_ORIGIN + "/about.html"]
    locs.extend(
        f"{SITE_ORIGIN}/coins/{row['symbol']}.html" for row in rows if row["has_page"]
    )
    body = ['<?xml version="1.0" encoding="UTF-8"?>']
    body.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for loc in locs:
        body.append(f"<url><loc>{escape(loc)}</loc><lastmod>{today}</lastmod></url>")
    body.append("</urlset>\n")
    return "\n".join(body)


def write_site(build_dir: Path, rows: list[dict], ctx: dict) -> None:
    assets = build_dir / "assets"
    coins = build_dir / "coins"
    assets.mkdir(parents=True)
    coins.mkdir()
    shutil.copyfile(ROOT / "templates" / "style.css", assets / "style.css")
    shutil.copyfile(ROOT / "templates" / "table.js", assets / "table.js")
    (build_dir / "index.html").write_text(index_page(rows, ctx), encoding="utf-8")
    (build_dir / "about.html").write_text(about_page(ctx), encoding="utf-8")
    for row in rows:
        if row["has_page"]:
            (coins / f"{row['symbol']}.html").write_text(coin_page(row, ctx), encoding="utf-8")
    (build_dir / "sitemap.xml").write_text(sitemap(rows, ctx["today"]), encoding="utf-8")
    (build_dir / "robots.txt").write_text(
        "User-agent: *\nAllow: /\n\nSitemap: "
        + SITE_ORIGIN
        + "/sitemap.xml\n",
        encoding="utf-8",
    )
    (build_dir / ".nojekyll").write_text("", encoding="utf-8")


ALLOWED_URL_PREFIXES = (
    SITE_ORIGIN + "/",
    SITE_ORIGIN + '"',
    OURBIT_URL,
    TV_URL,
    "https://futures.ourbit.com/",
    X_URL,
    "https://schema.org",
)
BANNED_SNIPPETS = (
    "自動化",
    "人工智慧",
    "機器人",
    "做多",
    "做空",
    "開多",
    "開空",
    "訊號",
    "勝率",
    "跟單",
    "績效",
    "買進",
    "賣出",
    "建議買",
    "建議賣",
)
BANNED_WORD = re.compile(
    r"(?i)(?<![A-Za-z])(bot|chatgpt|gpt|openai|claude|cursor)(?![A-Za-z])"
)
BARE_AI = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])")


def strip_contracts(text: str, rows: list[dict]) -> str:
    tokens = sorted(
        {row["symbol"] for row in rows} | {row["base"] for row in rows},
        key=len,
        reverse=True,
    )
    if not tokens:
        return text
    pattern = re.compile("|".join(re.escape(token) for token in tokens))
    return pattern.sub("", text)


def check_copy(text: str, label: str, rows: list[dict] | None = None) -> None:
    cleaned = strip_contracts(text, rows or [])
    for snippet in BANNED_SNIPPETS:
        if snippet in text:
            raise CheckError(f"{label} 含有不該出現的字：{snippet}")
    if BANNED_WORD.search(cleaned) or BARE_AI.search(cleaned):
        raise CheckError(f"{label} 含有不該出現的字")
    lowered = text.lower()
    for needle in ("gmail", "noreply", "/workspace", "/home/", "127.0.0.1", "localhost"):
        if needle in lowered:
            raise CheckError(f"{label} 含有不該出現的內容")
    if DISCLOSURE not in text or DISCLAIMER not in text:
        raise CheckError(f"{label} 缺少揭露或參考說明")
    for match in re.finditer("投資建議", text):
        if text[max(0, match.start() - 3) : match.start()] != "不構成":
            raise CheckError(f"{label} 的投資建議用語不在免責句裡")
    urls = re.findall(r"https://[^\s\"'<>\\]+", text)
    for url in urls:
        url = url.rstrip(").,")
        if not any(url == prefix or url.startswith(prefix) for prefix in ALLOWED_URL_PREFIXES):
            raise CheckError(f"{label} 有未允許的連結 {url}")
    if "http://" in text:
        raise CheckError(f"{label} 含有未加密連結")


def audit(build_dir: Path, rows: list[dict], ctx: dict) -> None:
    index_html = (build_dir / "index.html").read_text(encoding="utf-8")
    about_html = (build_dir / "about.html").read_text(encoding="utf-8")
    check_copy(index_html, "總表", rows)
    check_copy(about_html, "關於", rows)
    if 'lang="zh-Hant"' not in index_html or "name=\"viewport\"" not in index_html:
        raise CheckError("總表缺少語言或手機設定")
    for key in ("turnover", "mover", "funding", "oi", "premium"):
        if f'data-filter="{key}"' not in index_html:
            raise CheckError(f"總表缺少篩選 {key}")
    tags = re.findall(r'data-tags="([^"]*)"', index_html)
    if len(tags) != len(rows):
        raise CheckError("總表列數和資料不一致")
    if sum("turnover" in item.split() for item in tags) != min(TURNOVER_TOP_N, len(rows)):
        raise CheckError("成交額前段的數量不對")
    for row in rows:
        mover = row["chg"] is not None and abs(row["chg"]) >= MOVER_ABS
        if mover != ("mover" in row["tags"]):
            raise CheckError(f"波動標記不一致 {row['symbol']}")
        premium_hit = row["premium"] is not None and abs(row["premium"]) >= PREMIUM_ABS
        if premium_hit != ("premium" in row["tags"]):
            raise CheckError(f"價差標記不一致 {row['symbol']}")
    btc = next(row for row in rows if row["symbol"] == "BTC_USDT")
    if not btc["has_page"]:
        raise CheckError("BTC 沒有單幣頁")
    btc_html = (build_dir / "coins" / "BTC_USDT.html").read_text(encoding="utf-8")
    check_copy(btc_html, "BTC", rows)
    if "<svg" not in btc_html or "資金費率" not in btc_html:
        raise CheckError("BTC 頁面缺少圖或資金費率")
    pages = [row for row in rows if row["has_page"]]
    if len(pages) != ctx["page_n"] or len(list((build_dir / "coins").glob("*.html"))) != len(pages):
        raise CheckError("單幣頁數量不對")
    if len(pages) < 100:
        raise CheckError("單幣頁過少")
    for row in pages:
        text = (build_dir / "coins" / f"{row['symbol']}.html").read_text(encoding="utf-8")
        check_copy(text, row["symbol"], rows)
        if f"<title>{esc(row['base'])} 永續合約" not in text:
            raise CheckError(f"{row['symbol']} 標題不對")
        srcs = re.findall(r"<script[^>]+src=\"([^\"]+)\"", text)
        if srcs:
            raise CheckError(f"{row['symbol']} 不該載入額外腳本")
    index_srcs = re.findall(r"<script[^>]+src=\"([^\"]+)\"", index_html)
    if index_srcs != ["assets/table.js"]:
        raise CheckError("總表腳本不對")
    sitemap_text = (build_dir / "sitemap.xml").read_text(encoding="utf-8")
    if sitemap_text.count("<loc>") != 2 + len(pages):
        raise CheckError("網站地圖數量不對")
    if "BTC_USDT.html" not in sitemap_text:
        raise CheckError("網站地圖沒有 BTC")
    style = (build_dir / "assets" / "style.css").read_text(encoding="utf-8")
    if "http://" in style or "https://" in style:
        raise CheckError("樣式表含有外部連結")


def replace_tree(src: Path, dest: Path) -> None:
    backup = dest.with_name(dest.name + ".bak")
    if backup.exists():
        shutil.rmtree(backup)
    if dest.exists():
        dest.rename(backup)
    try:
        src.rename(dest)
    except Exception:
        if not dest.exists() and backup.exists():
            backup.rename(dest)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def prepare() -> tuple[list[dict], dict, str, str, str]:
    print("開始讀取全市場行情", flush=True)
    tickers = fetch_json(API + "/api/v1/contract/ticker")
    funding_rows = fetch_json(API + "/api/v1/contract/funding_rate")
    details = fetch_json(API + "/api/v1/contract/detail")
    if not isinstance(tickers, list) or not isinstance(funding_rows, list) or not isinstance(details, list):
        raise FetchError("清單格式不對")
    rows = build_rows(tickers, funding_rows, details)
    assert_magnitude(rows)
    stamps = [stamp for stamp in (row["timestamp"] for row in rows) if isinstance(stamp, (int, float))]
    moment = datetime.fromtimestamp((max(stamps) if stamps else time.time() * 1000) / 1000, TAIPEI)
    today = moment.date().isoformat()
    when_text = moment.strftime("%Y-%m-%d %H:%M") + "（台北時間）"
    when_iso = moment.isoformat(timespec="minutes")
    snapshot = load_snapshot()
    extra = apply_tags(rows, snapshot, today)
    print(
        f"合約 {len(rows)} 單幣頁 {extra['page_n']} "
        f"BTC {fmt_price(next(row['last'] for row in rows if row['symbol']=='BTC_USDT'), 1)}",
        flush=True,
    )
    attach_series(rows)
    ctx = {
        "today": today,
        "when_text": when_text,
        "when_iso": when_iso,
        "generated_at": moment.isoformat(timespec="seconds"),
        **extra,
    }
    return rows, snapshot, today, when_iso, ctx


def self_check() -> None:
    if abs((oi_qty_change(100, 120) or 0) - 0.2) > 1e-9:
        raise CheckError("持倉變化計算不對")
    if oi_qty_change(0, 5) is not None or oi_qty_change(None, 5) is not None:
        raise CheckError("持倉變化的空白情況不對")
    snapshot = {
        "dates": ["2026-11-13", "2026-11-14"],
        "series": {
            "2026-11-14": {"BTC_USDT": {"qty": 2, "notional": 3, "contractSize": 0.0001}},
            "2026-11-13": {"BTC_USDT": {"qty": 1, "notional": 1, "contractSize": 0.0001}},
        },
    }
    day, point = previous_point(snapshot, "BTC_USDT", "2026-11-14")
    if day != "2026-11-13" or not point or point["qty"] != 1:
        raise CheckError("前次快照沒有跳過同一天")
    if trim_number("1,000.00") != "1,000":
        raise CheckError("數字格式會吃掉千分位")
    if fmt_pct(-0.0191) != "−1.91%":
        raise CheckError("漲跌幅格式不對")
    if DISCLOSURE != "透過連結註冊我會拿返佣":
        raise CheckError("揭露文字不對")
    if not DISCLAIMER.startswith("數據僅供參考"):
        raise CheckError("參考說明不對")


def main() -> int:
    try:
        self_check()
    except Exception as exc:
        print(f"內部檢查失敗：{exc}", flush=True)
        return 1
    try:
        rows, snapshot, today, _when_iso, ctx = prepare()
    except FetchError as exc:
        print(f"行情讀取失敗，保留既有頁面：{exc}", flush=True)
        return 1
    except Exception:
        traceback.print_exc()
        print("整理失敗，保留既有頁面。", flush=True)
        return 1
    build_dir = ROOT / "site-build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    try:
        write_site(build_dir, rows, ctx)
        audit(build_dir, rows, ctx)
    except Exception:
        shutil.rmtree(build_dir, ignore_errors=True)
        traceback.print_exc()
        print("頁面檢查失敗，保留既有頁面。", flush=True)
        return 1
    try:
        payload = snapshot_payload(snapshot, today, rows, ctx["generated_at"])
        write_snapshot(payload)
        replace_tree(build_dir, ROOT / "site")
    except Exception:
        traceback.print_exc()
        print("寫入失敗。", flush=True)
        return 1
    print(f"完成 {today}，頁面在 site/", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
