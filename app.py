"""Local web server for high-dividend A-share watchlist (not investment advice)."""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import engine

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / ".market_cache.json"
LOCK = threading.RLock()
STATE = {
    "stocks": {code: {**record, "price": None, "change_pct": None, "quote_time": None,
                       "yield": None, "dividend_yield": None, "rly_details": None, "ttm_yield": None, "yield_percentile": None,
                       "yield_samples": 0, "boll": None, "resonance": None,
                       "errors": {}, "updated_at": None}
               for code, record in engine.STOCK_MAP.items()},
    "jobs": {"running": False, "done": 0, "total": 33, "phase": "未同步", "started": None,
             "finished": None, "error": None},
}


def _read_cache():
    if not CACHE_PATH.exists():
        return
    try:
        stored = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        with LOCK:
            for code, item in stored.get("stocks", {}).items():
                if code in STATE["stocks"]:
                    STATE["stocks"][code].update(item)
    except (ValueError, OSError) as e:
        print("缓存读取失败", e)


def _save_cache():
    with LOCK:
        data = {"stocks": STATE["stocks"]}
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)


def _merge_stock(code, fields):
    with LOCK:
        STATE["stocks"][code].update(fields)


def _refresh_quote():
    updates = engine.quotes_all()
    for code, q in updates.items():
        _merge_stock(code, q)
    return len(updates)


def _process_stock(code):
    patch = {"errors": {}, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    try:
        calc = engine.compute_boll_indicators(code)
        patch["boll"] = calc["boll"]
        patch["resonance"] = calc["resonance"]
        if calc["errors"]:
            patch["errors"]["boll"] = calc["errors"]
        # Keep chart data cached for /api/stock/{code}, not in the bulk table payload.
        with LOCK:
            CHART_CACHE[code] = calc["candles"]
    except Exception as e:
        patch["errors"]["boll"] = str(e)
        patch["boll"] = None
        patch["resonance"] = None
    try:
        divs = engine.dividends(code)
        with LOCK:
            DIVIDEND_CACHE[code] = divs
            raw_price = STATE["stocks"][code].get("price")
            qtime = str(STATE["stocks"][code].get("quote_time") or "")
        if not raw_price:
            raise RuntimeError("未取得最新原始股价，暂不计算股息率")
        # Tencent snapshot timestamp is normally YYYYMMDDhhmmss.
        asof = qtime[:8]
        if len(asof) == 8 and asof.isdigit():
            asof_date = f"{asof[:4]}-{asof[4:6]}-{asof[6:8]}"
            date.fromisoformat(asof_date)
        else:
            asof_date = date.today().isoformat()
        detail = engine.rly_yield(divs, asof_date, raw_price)
        yield_pct = detail["yield"]
        patch["yield"] = yield_pct
        patch["dividend_yield"] = yield_pct
        patch["rly_details"] = detail
        patch["yield_asof"] = asof_date
        patch["yield_basis"] = "最近已公告年度末期+中期分红（每10股金额÷10），按原始现价计算；中期待定时显式标记"
        if yield_pct is None:
            patch["errors"]["rly"] = detail["status"]
            patch["yield_percentile"] = None
            patch["yield_samples"] = 0
        else:
            try:
                patch["ttm_yield"] = engine.cash_yield_percent(divs, asof_date, raw_price)
            except Exception:
                patch["ttm_yield"] = None
            try:
                history = engine.raw_week_kline(code)
                dist = engine.yield_percentile(history, yield_pct, divs, asof_date)
                patch["yield_percentile"] = dist["percentile"]
                patch["yield_samples"] = dist["samples"]
                patch["yield_rank_details"] = dist
                if dist.get("warning"):
                    patch["errors"]["percentile"] = dist["warning"]
            except Exception as e:
                patch["yield_percentile"] = None
                patch["yield_samples"] = 0
                patch["errors"]["percentile"] = str(e)
    except Exception as e:
        patch["errors"]["dividend"] = str(e)
        patch["yield"] = None
        patch["rly_details"] = None
        patch["yield_percentile"] = None
    _merge_stock(code, patch)
    return code

CHART_CACHE = {}
DIVIDEND_CACHE = {}
REPORT_CACHE = {}


def _run_refresh():
    with LOCK:
        STATE["jobs"].update({"running": True, "done": 0, "total": len(engine.STOCKS),
                               "phase": "拉取腾讯报价", "started": datetime.now().isoformat(timespec="seconds"),
                               "finished": None, "error": None})
    try:
        try:
            _refresh_quote()
            _save_cache()
        except Exception as e:
            with LOCK:
                STATE["jobs"]["error"] = "报价获取失败：" + str(e)
        with LOCK:
            STATE["jobs"]["phase"] = "计算HFQ布林通道、宣告分红RLY及RLY历史分位（东财接口需要节流）"
        with ThreadPoolExecutor(max_workers=5) as executor:
            tasks = [executor.submit(_process_stock, code) for code, _, _ in engine.STOCKS]
            for future in as_completed(tasks):
                try:
                    future.result()
                except Exception as e:
                    print("单标的刷新异常", e)
                with LOCK:
                    STATE["jobs"]["done"] += 1
                _save_cache()
    finally:
        with LOCK:
            STATE["jobs"].update({"running": False, "phase": "刷新结束",
                                  "finished": datetime.now().isoformat(timespec="seconds")})
        _save_cache()


_read_cache()
app = FastAPI(title="高股息观察与财报分析", version="3.0.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/stocks")
def list_stocks():
    with LOCK:
        return {"stocks": [dict(STATE["stocks"][code]) for code, _, _ in engine.STOCKS],
                "jobs": dict(STATE["jobs"]), "sources": engine.SOURCES,
                "metadata": {"boll_adjustment": "hfq", "boll_rule": "上>=上轨；中上>中轨+1σ；中=中轨±1σ；中下<中轨-1σ；下<=下轨；共振=周与月都为中/中下", "version": "3.0.0", "dividend_type": "RLY 最近已公告年度末期+中期 (按报告期，元/10股÷10；中期未宣告待定沿用上期)",
                             "data_status": "缓存/实时均逐条标注；无网络时不会填充模拟值"}}


@app.post("/api/refresh")
def refresh():
    with LOCK:
        if STATE["jobs"]["running"]:
            return {"ok": True, "message": "刷新任务已在进行", "jobs": dict(STATE["jobs"])}
        STATE["jobs"]["running"] = True
    threading.Thread(target=_run_refresh, daemon=True).start()
    return {"ok": True, "message": "已开始后台刷新，请观察进度"}


@app.post("/api/quotes")
def refresh_quotes():
    try:
        count = _refresh_quote()
        _save_cache()
        return {"ok": True, "count": count}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/stock/{code}")
def stock_detail(code: str):
    if code not in engine.STOCK_MAP:
        raise HTTPException(status_code=404, detail="不在观察池中")
    with LOCK:
        result = dict(STATE["stocks"][code])
        result["chart"] = CHART_CACHE.get(code)
        result["dividends"] = DIVIDEND_CACHE.get(code)
    return result


@app.get("/api/stock/{code}/boll")
def stock_boll(code: str):
    if code not in engine.STOCK_MAP:
        raise HTTPException(status_code=404, detail="不在观察池中")
    try:
        result = engine.compute_boll_indicators(code)
        _merge_stock(code, {"boll": result["boll"], "resonance": result["resonance"]})
        with LOCK:
            CHART_CACHE[code] = result["candles"]
        _save_cache()
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/stock/{code}/financials")
def financials(code: str, force: bool = False):
    if code not in engine.STOCK_MAP:
        raise HTTPException(status_code=404, detail="不在观察池中")
    with LOCK:
        cached = REPORT_CACHE.get(code)
        if cached and not force and time.monotonic() - cached[0] < 3600:
            return cached[1]
    result = {"source": "新浪财经财报三表；巨潮资讯公告", "statement": None,
              "reports": [], "errors": [], "generated_summary": None, "fetched_at": datetime.now().isoformat(timespec="seconds")}
    try:
        pnl = engine.sina_statement(code, "lrb")
        if not pnl:
            raise RuntimeError("利润表无数据")
        result["statement"] = engine.statement_metrics(pnl)
        result["statement"]["periods"] = [r["period"] for r in pnl]
    except Exception as e:
        result["errors"].append("新浪利润表：" + str(e))
    try:
        result["reports"] = engine.cninfo_reports(code)
    except Exception as e:
        result["errors"].append("巨潮公告：" + str(e))
    if result["statement"] is None and not result["reports"]:
        raise HTTPException(status_code=502, detail="；".join(result["errors"]) or "财报来源无数据")
    with LOCK:
        REPORT_CACHE[code] = (time.monotonic(), result)
    return result


@app.get("/api/stock/{code}/dividends")
def dividend_detail(code: str):
    if code not in engine.STOCK_MAP:
        raise HTTPException(status_code=404, detail="不在观察池中")
    with LOCK:
        cached = DIVIDEND_CACHE.get(code)
    try:
        rows = cached or engine.dividends(code)
        if not cached:
            with LOCK:
                DIVIDEND_CACHE[code] = rows
        return {"dividends": rows[:40], "type": "RLY宣告分红方案（同时展示除息日）", "source": "东方财富分红送转",
                "rly_details": STATE["stocks"][code].get("rly_details")}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/export.csv")
def export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    columns = ["代码", "名称", "行业", "原始现价", "涨跌幅%", "RLY宣告股息率%",
               "RLY历史分位%", "有效周样本数", "RLY末期每股分红", "RLY中期每股分红", "RLY状态", "实派TTM参考%"]
    for name in ("日", "周", "月"):
        columns += [f"{name}BOLL位置", f"{name}HFQ收盘", f"{name}上轨", f"{name}中轨", f"{name}下轨", f"{name}K线日期"]
    columns += ["低位共振", "行情时间", "BOLL复权方式"]
    writer.writerow(columns)
    with LOCK:
        for code, _, _ in engine.STOCKS:
            r = STATE["stocks"][code]
            bs = r.get("boll") or {}
            row = [code, r["name"], r["industry"], r.get("price"), r.get("change_pct"),
                   r.get("yield"), r.get("yield_percentile"), r.get("yield_samples"),
                   (r.get("rly_details") or {}).get("year_end_cash"),
                   (r.get("rly_details") or {}).get("interim_cash"),
                   (r.get("rly_details") or {}).get("status"), r.get("ttm_yield")]
            for period in ("day", "week", "month"):
                v = bs.get(period) or {}
                row += [v.get("position"), v.get("close_hfq"), v.get("upper"),
                        v.get("mid"), v.get("lower"), v.get("date")]
            writer.writerow(row + [r.get("resonance"), r.get("quote_time"), "hfq"])

    return Response(output.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="dividend-watch.csv"'})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8765")))
