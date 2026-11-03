"""A-stock-data (Simon Lin) documented Tencent / Eastmoney / Sina API adapters.
BOLL uses the *native HFQ* day/week/month candles returned by Tencent, never raw/qfq substitutes.
No live-market fallback to screenshot values. Data availability and as-of are explicit.
"""
from __future__ import annotations

import math
import os
import re
import statistics
import threading
import time
from datetime import date, timedelta
from typing import Any

import requests

SOURCES = {
    "repository": "https://github.com/simonlin1212/a-stock-data",
    "documentation": "https://raw.githubusercontent.com/simonlin1212/a-stock-data/main/SKILL.md",
    "quote": "https://qt.gtimg.cn",
    "hfq": "https://web.ifzq.gtimg.cn",
    "dividend": "https://datacenter-web.eastmoney.com",
    "financial": "https://quotes.sina.cn",
    "announcements": "https://www.cninfo.com.cn",
}
HEADERS = {"User-Agent": "Mozilla/5.0 Dividend-Watch/1.0", "Referer": "https://gu.qq.com/"}
SESSION = requests.Session()
HOSTS = ["https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg", "https://ifzq.gtimg.cn"]
_host_fail_until: dict[str, float] = {}
_host_lock = threading.Lock()
_em_lock = threading.Lock()
_em_last = 0.0

# All 33 tickers from the user's 2026-11-02 screenshots; codes checked against known A-share listings.
STOCKS = [
    ("000651", "格力电器", "家用电器"), ("000858", "五粮液", "食品饮料"),
    ("600750", "华润江中", "医药生物"), ("000423", "东阿阿胶", "医药生物"),
    ("600096", "云天化", "基础化工"), ("600023", "浙能电力", "公用事业"),
    ("601166", "兴业银行", "银行"), ("600011", "华能国际", "公用事业"),
    ("600642", "申能股份", "公用事业"), ("601318", "中国平安", "保险"),
    ("000807", "云铝股份", "有色金属"), ("000333", "美的集团", "家用电器"),
    ("601825", "沪农商行", "银行"), ("601919", "中远海控", "交通运输"),
    ("000538", "云南白药", "医药生物"), ("601838", "成都银行", "银行"),
    ("600036", "招商银行", "银行"), ("601169", "北京银行", "银行"),
    ("600941", "中国移动", "通信"), ("601006", "大秦铁路", "交通运输"),
    ("600795", "国电电力", "公用事业"), ("000429", "粤高速A", "交通运输"),
    ("600690", "海尔智家", "家用电器"), ("600887", "伊利股份", "食品饮料"),
    ("601998", "中信银行", "银行"), ("600863", "华能蒙电", "公用事业"),
    ("601088", "中国神华", "煤炭"), ("002415", "海康威视", "计算机设备"),
    ("600028", "中国石化", "石油石化"), ("600938", "中国海油", "石油石化"),
    ("601857", "中国石油", "石油石化"), ("601225", "陕西煤业", "煤炭"),
    ("000933", "神火股份", "有色金属"),
]
STOCK_MAP = {code: {"code": code, "name": name, "industry": industry} for code, name, industry in STOCKS}


def market_symbol(code: str) -> str:
    if code not in STOCK_MAP:
        raise ValueError("标的代码不在33只观察池中")
    return ("sh" if code.startswith("6") else "sz") + code


def _get_json(url, *, params=None, headers=None, timeout=12):
    r = SESSION.get(url, params=params, headers=headers or HEADERS, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("上游响应非JSON对象")
    return data


def quotes_all():
    """Follows a-stock-data §1.1 fields: Tencent GBK tilde-separated quote."""
    symbols = [market_symbol(code) for code, _, _ in STOCKS]
    response = SESSION.get("https://qt.gtimg.cn/q=" + ",".join(symbols),
                           headers=HEADERS, timeout=12)
    response.raise_for_status()
    result = {}
    content = response.content.decode("gbk", errors="replace")
    for line in content.split(";"):
        if '="' not in line:
            continue
        raw_symbol = line.split("=", 1)[0].split("_")[-1].strip()
        code = raw_symbol[-6:]
        vals = line.split('"', 2)[1].split("~")
        if code not in STOCK_MAP or len(vals) < 53:
            continue
        try:
            price = float(vals[3]); change = float(vals[32]); last_close = float(vals[4]);
        except (ValueError, TypeError):
            continue
        if price <= 0:
            continue
        quote_stamp = vals[30] if len(vals) > 30 else None
        result[code] = {"price": price, "change_pct": change,
                        "last_close": last_close, "quote_time": quote_stamp,
                        "quote_source": "腾讯财经实时快照（可能延迟）"}
    if not result:
        raise RuntimeError("腾讯财经未返回有效的观察池行情")
    return result


def hfq_kline(code: str, period: str, count: int = 240) -> list[dict]:
    """a-stock-data §1.2 native Tencent adjusted Kline.
    Require a native hfq period key. If the upstream only provides unadjusted
    candles, mark BOLL unavailable instead of making an unverifiable assumption.
    """
    if period not in ("day", "week", "month") or not 25 <= count <= 640:
        raise ValueError("周期只能day/week/month，K线数量25～640")
    symbol = market_symbol(code)
    param = f"{symbol},{period},,,{count},hfq"
    errors = []
    for host in HOSTS:
        with _host_lock:
            if _host_fail_until.get(host, 0) > time.monotonic():
                continue
        try:
            payload = _get_json(host + "/appstock/app/fqkline/get",
                                params={"param": param}, timeout=18)
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get(symbol), dict):
                raise RuntimeError("缺少股票K线节点")
            node = data[symbol]
            hfq_key = "hfq" + period
            if hfq_key in node:
                rows = node[hfq_key]
                if not rows and node.get(period):
                    raise RuntimeError("后复权数组为空，拒绝使用未复权价替代")
                factor = "hfq"
            else:
                # Unadjusted data is not proof that no corporate action ever occurred.
                # Fail closed rather than silently mixing incompatible price bases.
                raise RuntimeError("响应缺少后复权字段；禁止用未复权价代替HFQ")
            if not isinstance(rows, list) or not rows:
                raise RuntimeError("后复权K线为空")
            candles = []
            for row in rows:
                d = str(row[0])[:10]
                close = float(row[2]); high = float(row[3]); low = float(row[4])
                if not all(math.isfinite(v) and v > 0 for v in (close, high, low)):
                    raise RuntimeError("后复权K线存在非法数值")
                candles.append({"date": d, "close": close, "high": high, "low": low})
            candles.sort(key=lambda x: x["date"])
            if len(set(x["date"] for x in candles)) != len(candles):
                raise RuntimeError("K线交易日期重复")
            if len(candles) < 20:
                raise RuntimeError("不足20根K线")
            return [{**x, "adjust": "hfq", "factor_source": factor} for x in candles]
        except (requests.RequestException, ValueError, TypeError, RuntimeError, IndexError) as exc:
            errors.append(f"{host}: {exc}")
            with _host_lock:
                _host_fail_until[host] = time.monotonic() + 45
    raise RuntimeError("；".join(errors) or "腾讯K线接口处于冷却状态，请稍后刷新")


# BOLL 五档对应图示定义：
# 上：触上轨；中上：(中轨+1σ, 上轨)；中：[中轨-1σ, 中轨+1σ]；
# 中下：(下轨, 中轨-1σ)；下：触下轨。
# 这里直接用标准 BOLL (20 SMA ± 2σ) 的上下轨与中轨，
# 中间两条「半轨」只用于五档展示，不是另一个指标。
BOLL_COLORS = {
    "上": "#eaa0b8",  # 紫红
    "中上": "#eac27a",  # 琥珀
    "中": "#d3dbe9",  # 灰蓝
    "中下": "#87dfaa", # 翠绿
    "下": "#4fbd91",  # 深绿
}


def classify_boll(close: float, middle: float, upper: float, lower: float) -> str:
    """固定五档边界。无法判定的值不能被伪装为'中'。"""
    nums = (close, middle, upper, lower)
    if not all(isinstance(x, (float, int)) and math.isfinite(x) for x in nums):
        raise ValueError("BOLL 输入不合法")
    if not lower <= middle <= upper:
        raise ValueError("BOLL 上中下轨顺序错误")
    if abs(upper - lower) < 1e-10:
        return "中"  # 完全无波动时不视为触上/下轨
    if close >= upper:
        return "上"
    if close <= lower:
        return "下"
    half_upper = (middle + upper) / 2
    half_lower = (middle + lower) / 2
    if close > half_upper:
        return "中上"
    if close < half_lower:
        return "中下"
    return "中"


def boll_series(candles: list[dict], window: int = 20, std_factor: float = 2.0):
    """全量原生周期 HFQ 蜡烛生成 BOLL 轨迹；价格不做额外折算。

    每根数据用该周期截止时的最近 20 根收盘价计算。总体标准差 ddof=0。
    前19根仅作为预热窗，其线值留空；图表和右侧数值从同一结果读取。
    """
    if window != 20 or std_factor != 2.0:
        raise ValueError("本产品固定 BOLL(20,2)")
    if not isinstance(candles, list):
        raise ValueError("K线不是数组")
    for i, c in enumerate(candles):
        if (c.get("adjust") != "hfq" or not isinstance(c.get("close"), (float, int))
                or not math.isfinite(c["close"]) or c["close"] <= 0):
            raise ValueError(f"第{i+1}根K线缺少有效后复权价格，不允许混用原始价格")
    enriched = []
    history = []
    for candle in candles:
        current = float(candle["close"])
        history.append(current)
        item = {"date": candle["date"], "close": current,
                "upper": None, "mid": None, "lower": None,
                "position": None, "adjust": "hfq"}
        if len(history) >= window:
            vals = history[-window:]
            mid = statistics.mean(vals)
            sd = statistics.pstdev(vals)
            upper, lower = mid + std_factor * sd, mid - std_factor * sd
            item.update({"upper": round(upper, 6), "mid": round(mid, 6),
                         "lower": round(lower, 6),
                         "std": round(sd, 6),
                         "position": classify_boll(current, mid, upper, lower)})
        enriched.append(item)
    return enriched


def boll_last(candles: list[dict], window=20, std_factor=2):
    if len(candles) < window:
        return None
    points = boll_series(candles, window, std_factor)
    item = points[-1]
    return {"date": item["date"], "close_hfq": item["close"],
            "mid": round(item["mid"], 4), "upper": round(item["upper"], 4),
            "lower": round(item["lower"], 4),
            "std": round(item["std"], 4),
            "position": item["position"], "adjust": "hfq", "n": window,
            "basis": "原生周期后复权收盘价，BOLL(20,2)，总体标准差", "factor_source": candles[-1].get("factor_source")}


def resonance(day, week, month):
    # 只看周+月，严格依照用户提供的原图；日线不参与，下轨也不在共振规则内。
    if not week or not month:
        return None
    return week.get("position") in ("中", "中下") and month.get("position") in ("中", "中下")


def compute_boll_indicators(code):
    details = {}
    errors = {}
    for period in ("day", "week", "month"):
        try:
            candles = hfq_kline(code, period, count=240 if period != "month" else 120)
            plot = boll_series(candles)
            details[period] = {"boll": boll_last(candles), "candles": plot[-100:]}
        except Exception as e:
            details[period] = {"boll": None, "candles": []}
            errors[period] = str(e)
    vals = {period: details[period]["boll"] for period in details}
    return {"boll": vals, "resonance": resonance(vals["day"], vals["week"], vals["month"]),
            "candles": {p: details[p]["candles"] for p in details}, "errors": errors,
            "adjust": "hfq", "source": "腾讯财经原生后复权 K 线 / a-stock-data §1.2",
            "boll_rule": "BOLL(20,2)；上/中上/中/中下/下；月与周同时中或中下=共振"}


def _em_get(url, *, params):
    """Serialize Eastern Money calls to avoid IP bans; no sharing of user credentials."""
    global _em_last
    with _em_lock:
        delay = 1.1 - (time.monotonic() - _em_last)
        if delay > 0:
            time.sleep(delay)
        try:
            return _get_json(url, params=params,
                             headers={"User-Agent": HEADERS["User-Agent"], "Referer": "https://data.eastmoney.com/"},
                             timeout=18)
        finally:
            _em_last = time.monotonic()


def _iso_day(v):
    """Strictly resolve a vendor date. Do not substitute today's date for missing disclosure dates."""
    val = str(v or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
        return None
    try:
        return date.fromisoformat(val).isoformat()
    except ValueError:
        return None


def _cash_plan_amount(plan: str):
    """Cash amount (CNY/10 shares) from the public '10派x元' text, if present."""
    match = re.search(r"(?:每\s*)?10\s*股?[^，,；;]{0,15}?派(?:发|现|现金)?\s*(?:人民币)?\s*(\d+(?:\.\d+)?)\s*元?", plan or "")
    if not match:
        match = re.search(r"(?:10\s*)?派(?:发|现|现金)?\s*(?:人民币)?\s*(\d+(?:\.\d+)?)\s*元?", plan or "")
    return float(match.group(1)) if match else None


def parse_dividend_rows(raw_rows):
    """Normalize Eastmoney rows, preserving audit fields.

    PRETAX_BONUS_RMB is CNY PER TEN SHARES. This differs from the incorrect
    upstream a-stock-data example that names it as amount-per-share. An
    additional '10派X' string cross-check rejects conflicting source values.
    The report period and first plan date are required for the declared RLY.
    """
    output = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        period = _iso_day(row.get('REPORT_DATE') or row.get('END_DATE') or row.get('REPORT_PERIOD'))
        initial_ann = _iso_day(row.get('PLAN_NOTICE_DATE') or row.get('NOTICE_DATE') or row.get('ANNOUNCEMENT_DATE'))
        if not period or not initial_ann:
            continue
        status = str(row.get('ASSIGN_PROGRESS') or '').strip()
        text = str(row.get('IMPL_PLAN_PROFILE') or row.get('PLAN_PROFILE') or row.get('PLAN_EXPLAIN') or '')
        value = row.get('PRETAX_BONUS_RMB')
        per_ten = None
        if value is not None and str(value).strip() != '':
            try:
                per_ten = float(value)
                if not math.isfinite(per_ten) or per_ten < 0:
                    continue
            except (ValueError, TypeError):
                continue
        in_text = _cash_plan_amount(text)
        # Missing numeric data may be extracted from an unequivocal public scheme.
        if per_ten is None and in_text is not None:
            per_ten = in_text
        if per_ten is None:
            per_ten = 0.0
        # A mismatch is more dangerous than missing data: refuse the dividend.
        if in_text is not None and abs(per_ten - in_text) > max(.0001, in_text * .0001):
            continue
        exclusion = any(word in status + text for word in ('已取消', '终止', '撤回', '不予实施', '失效'))
        if exclusion:
            continue
        no_distribution = any(word in status + text for word in ('不分配', '不派发', '不进行利润分配', '不派现'))
        if not per_ten and not no_distribution:
            # No cash amount does not prove that management cancelled dividends.
            continue
        ex_date = _iso_day(row.get('EX_DIVIDEND_DATE'))
        pay_per_share = round(per_ten / 10.0, 8)
        try:
            bonus_per_ten = float(row.get('BONUS_RATIO') or row.get('BONUS_IT_RATIO') or 0)
            transfer_per_ten = float(row.get('TRANSFER_RATIO') or row.get('IT_RATIO') or 0)
        except (ValueError, TypeError):
            bonus_per_ten, transfer_per_ten = 0.0, 0.0
        output.append({
            'report_date': period, 'announcement_date': initial_ann,
            'notice_date_source': 'PLAN_NOTICE_DATE' if row.get('PLAN_NOTICE_DATE') else 'NOTICE_DATE',
            'date': ex_date, 'ex_date': ex_date,
            'cash_per_ten': round(per_ten, 8),
            'cash_per_share': pay_per_share, 'status': status,
            'plan': text, 'no_distribution': no_distribution,
            'category': '年度末期' if period.endswith('12-31') else '中期',
            'bonus_per_ten': bonus_per_ten,
            'transfer_per_ten': transfer_per_ten,
            'source': '东方财富 RPT_SHAREBONUS_DET',
            'verification_url': f"https://data.eastmoney.com/yjfp/detail/{str(row.get('SECURITY_CODE') or '').zfill(6)}.html"
        })
    return output


def dividends(code, count=180):
    """Read disclosed cash plans, including not-yet-ex-dividend plans.

    Sort by REPORT_DATE rather than EX_DIVIDEND_DATE so that approved plans
    without an ex-rights date aren't pushed out of the first page.
    """
    if code not in STOCK_MAP:
        raise ValueError('未知代码')
    payload = _em_get('https://datacenter-web.eastmoney.com/api/data/v1/get', params={
        'reportName': 'RPT_SHAREBONUS_DET', 'columns': 'ALL',
        'pageNumber': 1, 'pageSize': count,
        'sortColumns': 'REPORT_DATE', 'sortTypes': '-1',
        'filter': f'(SECURITY_CODE="{code}")',
    })
    result = payload.get('result')
    if not isinstance(result, dict) or result.get('data') is None:
        raise RuntimeError('东财没有返回分红方案（可能限流或字段改变）')
    rows = parse_dividend_rows(result['data'])
    for r in rows:
        r['verification_url'] = f'https://data.eastmoney.com/yjfp/detail/{code}.html'
    if not rows:
        raise RuntimeError('没有取得含报告期、公告日和可核实派息金额的分红记录')
    return sorted(rows, key=lambda r: (r['announcement_date'], r['report_date']), reverse=True)


def _dedupe_plans(records, asof):
    """Keep one *latest status* for each report period; do not count plan+implementation twice.

    If a vendor returns multiple materially different positive amounts under
    the SAME period, the tool refuses to guess which is a revision/extra payment.
    """
    asof_date = date.fromisoformat(str(asof))
    grouped = {}
    for r in records:
        ann = _iso_day(r.get('announcement_date'))
        rep = _iso_day(r.get('report_date'))
        if not ann or not rep or date.fromisoformat(ann) > asof_date:
            continue
        grouped.setdefault(rep, []).append(r)
    result = []
    for rep, values in grouped.items():
        amounts = {round(float(v.get('cash_per_share', 0)), 6) for v in values}
        if len(amounts) > 1:
            raise ValueError(f'{rep} 多个不同金额的分红方案，无法确认是否更正；请查原始公告')
        selected = max(values, key=lambda r: (r.get('announcement_date') or '',
                         1 if r.get('ex_date') else 0))
        result.append(selected)
    return sorted(result, key=lambda r: (r['report_date'], r['announcement_date']))


def _rebase_dividend_per_share(r, events, asof):
    """Convert declared cash to the share basis as of an observation date.

    If a later bonus/transfer corporate action changed number of shares,
    historic per-share cash is divided by cumulative share multiplication.
    Adj. is only applicable if source provides corporate-action fields; the
    yield trace explicitly exposes resulting factor. Does not mix HFQ prices.
    """
    after_date = r.get('ex_date') or r.get('announcement_date')
    factor = 1.0
    if after_date:
        for e in events:
            ex = e.get('ex_date')
            if not ex or not after_date < ex <= asof:
                continue
            mult = 1 + (float(e.get('bonus_per_ten') or 0) + float(e.get('transfer_per_ten') or 0)) / 10
            if mult > 0:
                factor *= mult
    return float(r['cash_per_share']) / factor, factor


def rly_yield(records, asof_date, raw_price):
    """RLY = latest declared year-end + latest declared midyear batch, divided by unadjusted price.

    Annual: latest report ending 12-31, interim: latest available year of
    declared non-annual plans (sum unique report periods in the year). If
    current-year midterm declaration is not available, carry forward the
    previously declared interim plan and label '待定沿用上期'. Do not confuse
    declaration date with ex-rights date. Zero needs explicit '不分配' evidence.
    """
    if raw_price is None or not math.isfinite(float(raw_price)) or float(raw_price) <= 0:
        return {'yield': None, 'status':'现价不可用', 'components':[], 'cash_per_share':None}
    asof = _iso_day(asof_date)
    if not asof:
        return {'yield': None, 'status':'行情日期不可用', 'components':[], 'cash_per_share':None}
    try:
        normalized = _dedupe_plans(records, asof)
    except ValueError as e:
        return {'yield': None, 'status':str(e), 'components':[], 'cash_per_share':None}
    annual = [r for r in normalized if r['report_date'].endswith('12-31')]
    if not annual:
        return {'yield': None, 'status':'缺少年报分红公告，暂不计算RLY', 'components':[], 'cash_per_share':None}
    year_end = annual[-1]
    # Avoid treating a 0-share, zero-dividend pre-disclosure record as final.
    interims = [r for r in normalized if not r['report_date'].endswith('12-31')]
    current_year = date.fromisoformat(asof).year
    effective = [r for r in interims if r['report_date'][:4] == str(current_year)]
    flagged_no_interim = any(r['no_distribution'] for r in effective)
    if any(r['cash_per_share'] > 0 for r in effective):
        interims_used = [r for r in effective if r['cash_per_share'] > 0]
        mid_status = '本年中期已公告'
    elif flagged_no_interim:
        interims_used = []
        mid_status = '本年中期明确不分配'
    else:
        earlier = [r for r in interims if r['report_date'][:4] < str(current_year) and r['cash_per_share'] > 0]
        if earlier:
            yy = max(int(r['report_date'][:4]) for r in earlier)
            interims_used = [r for r in earlier if int(r['report_date'][:4]) == yy]
            mid_status = f'本年中期待定；暂沿用{yy}年已公告中期分红（非已确认预测）'
        else:
            interims_used = []
            mid_status = '无可沿用的中期已宣告记录'
    # Annual/interim both use current capital base if source discloses split actions.
    selected = [year_end] + interims_used
    summary = []
    total = 0.0
    for row in selected:
        rebased, factor = _rebase_dividend_per_share(row, normalized, asof)
        total += rebased
        summary.append({
            'category': row['category'], 'report_date':row['report_date'],
            'announcement_date':row['announcement_date'], 'ex_date':row.get('ex_date'),
            'cash_per_ten':row['cash_per_ten'], 'cash_per_share':row['cash_per_share'],
            'share_adjustment_factor':round(factor, 7),
            'adjusted_cash_per_share':round(rebased, 7),
            'status':row['status'], 'plan':row['plan'],
            'verification_url':row.get('verification_url')
        })
    return {'yield':round(100 * total / float(raw_price), 4),
            'cash_per_share':round(total, 7), 'status':mid_status,
            'components':summary, 'annual_report_period': year_end['report_date'],
            'mid_year_status':mid_status, 'price_basis':'原始现价（不复权）',
            'source':'东财分红送配；按报告期/预案公告日（非除息日）',
            'year_end_cash':round(summary[0]['adjusted_cash_per_share'], 7),
            'interim_cash':round(sum(x['adjusted_cash_per_share'] for x in summary[1:]), 7)}


def cash_yield_percent(dividend_records, asof_date: str, raw_price: float):
    """Separate, optional *implemented TTM* cross-check; never use as RLY."""
    if not raw_price or raw_price <= 0:
        return None
    asof = date.fromisoformat(asof_date)
    start = asof - timedelta(days=365)
    entries = _dedupe_plans(dividend_records, asof_date)
    total = sum(_rebase_dividend_per_share(e, entries, asof_date)[0] for e in entries
                if e.get('ex_date') and start < date.fromisoformat(e['ex_date']) <= asof)
    return round(total / raw_price * 100, 4) if total else None


def raw_week_kline(code: str, count=560):
    """Historical UNADJUSTED prices are used solely for RLY rank denominator.
    HFQ is mandatory and exclusively used for BOLL.
    """
    symbol = market_symbol(code)
    param = f'{symbol},week,,,{min(count, 640)},'
    errors = []
    for host in HOSTS:
        try:
            data = _get_json(host + '/appstock/app/fqkline/get', params={'param': param}, timeout=18)
            node = (data.get('data') or {}).get(symbol) or {}
            rows = node.get('week')
            if not isinstance(rows, list) or not rows:
                raise RuntimeError('未复权周线不可用')
            return [{'date':str(row[0])[:10], 'close':float(row[2])} for row in rows]
        except (requests.RequestException, RuntimeError, ValueError, TypeError, IndexError) as e:
            errors.append(str(e))
    raise RuntimeError('未复权周线失败：'+';'.join(errors))


def yield_percentile(history, current_yield, records, asof_date=None):
    """10Y weekly historical RLY values (each week as-of, no future ann dates).
    Retrospective vendor corrections/bonuses can make this approximate; as-of
    calculations must never use future cash plan announcement dates.
    """
    if current_yield is None:
        return {'percentile':None, 'samples':0}
    asof = date.fromisoformat(asof_date or date.today().isoformat())
    first = asof - timedelta(days=3653)
    values = []
    for h in history:
        try:
            week = date.fromisoformat(h['date'])
            if not first <= week <= asof:
                continue
            result = rly_yield(records, str(week), float(h['close']))
            if result['yield'] is not None:
                values.append((week, result['yield']))
        except (ValueError, TypeError):
            continue
    if len(values) < 100:
        return {'percentile':None, 'samples':len(values), 'warning':'同口径历史RLY有效周样本不足100，分位不显示'}
    p = sum(v <= float(current_yield) for _, v in values) / len(values) * 100
    return {'percentile':round(p, 1), 'samples':len(values),
            'min':round(min(v for _,v in values),3), 'max':round(max(v for _,v in values),3),
            'history_start':min(d for d,_ in values).isoformat(),
            'history_end':max(d for d,_ in values).isoformat(),
            'method':'历史每周RLY；仅使用截至当周已公告方案；旧公告更正与权益变化可能造成回溯误差'}


def sina_statement(code: str, kind: str, periods=8):
    """a-stock-data §6.4 - type fzb/lrb/llb, preserve raw values."""
    if kind not in ("lrb", "fzb", "llb") or code not in STOCK_MAP:
        raise ValueError("invalid report request")
    payload = _get_json("https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
                        params={"paperCode": market_symbol(code), "source": kind,
                                "type": 0, "page": 1, "num": periods}, timeout=18)
    data = ((payload.get("result") or {}).get("data") or {}).get("report_list") or {}
    if not isinstance(data, dict):
        raise RuntimeError("新浪财报结构改变")
    out = []
    for period in sorted(data, reverse=True)[:periods]:
        record = {"period": f"{period[:4]}-{period[4:6]}-{period[6:8]}", "fields": {}}
        for item in (data[period] or {}).get("data", []) or []:
            title = item.get("item_title")
            if title and item.get("item_value") is not None:
                record["fields"][title] = {"value": item.get("item_value"),
                                            "yoy": item.get("item_tongbi")}
        out.append(record)
    return out


def statement_metrics(statements):
    latest = statements[0] if statements else None
    if not latest:
        return {"period": None, "metrics": []}
    specs = [
        ("营业收入", ["营业总收入", "营业收入"]),
        ("归母净利润", ["归属于母公司股东的净利润", "归属于母公司所有者的净利润", "归属母公司股东的净利润"]),
        ("净利润", ["净利润"]),
        ("营业利润", ["营业利润"]),
        ("每股收益", ["基本每股收益", "每股收益"]),
    ]
    used = set(); output = []
    for label, aliases in specs:
        for a in aliases:
            if a in latest["fields"] and a not in used:
                row = latest["fields"][a]
                output.append({"label": label, "field": a, "value": row["value"], "yoy": row.get("yoy")})
                used.add(a)
                break
    return {"period": latest["period"], "metrics": output, "raw_fields_count": len(latest["fields"])}


def cninfo_reports(code, page_size=60):
    """a-stock-data §7.1 dynamic official orgId mapping; link to official CNINFO detail."""
    if code not in STOCK_MAP:
        raise ValueError("invalid code")
    url = "https://www.cninfo.com.cn/new/data/szse_stock.json"
    stockmap = _get_json(url, timeout=18).get("stockList") or []
    org = next((row.get("orgId") for row in stockmap if str(row.get("code")) == code), None)
    if not org:
        raise RuntimeError("巨潮股票组织ID未取得，拒绝猜测并检索其他股票")
    body = {"stock": f"{code},{org}", "tabName": "fulltext", "pageSize": str(page_size),
            "pageNum": "1", "column": "", "category": "", "plate": "", "seDate": "",
            "searchkey": "", "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true"}
    response = SESSION.post("https://www.cninfo.com.cn/new/hisAnnouncement/query", data=body,
                            headers={"User-Agent": HEADERS["User-Agent"],
                                     "Content-Type": "application/x-www-form-urlencoded",
                                     "Origin": "https://www.cninfo.com.cn",
                                     "Referer": "https://www.cninfo.com.cn/new/disclosure"}, timeout=18)
    response.raise_for_status()
    j = response.json()
    anns = j.get("announcements")
    if anns is None:
        raise RuntimeError("巨潮公告接口没有返回公告列表")
    results = []
    for a in anns:
        title = re.sub(r"<[^>]+>", "", str(a.get("announcementTitle") or ""))
        if not any(k in title for k in ("年度报告", "季度报告", "半年度报告", "一季度报告", "三季度报告", "年报")):
            continue
        if any(k in title for k in ("摘要", "提示性公告", "取消", "审计报告")):
            continue
        ts = a.get("announcementTime")
        d = date.fromtimestamp(int(ts) / 1000).isoformat() if ts else None
        anno = str(a.get("announcementId") or "")
        if not anno:
            continue
        results.append({"title": title, "date": d,
                        "url": "https://www.cninfo.com.cn/new/disclosure/detail?annoId=" + anno,
                        "source": "巨潮资讯公告原文"})
    return results
