#!/usr/bin/env python3
"""
Scheduled Telegram market alert — mirrors the options_levels_tracker.html
dashboard's exact logic (walls, max pain, vol/range forecast, COT, scoring),
but runs server-side via GitHub Actions instead of in a browser. No CORS
issues here since Python has no browser restrictions, and no local_proxy.py
needed for this script specifically.

Required environment variables (set as GitHub repo secrets):
  TWELVE_DATA_KEY     - Twelve Data API key
  FRED_KEY            - FRED API key (optional; macro section skipped if absent)
  TELEGRAM_BOT_TOKEN  - Telegram bot token from BotFather
  TELEGRAM_CHAT_ID    - your numeric Telegram chat ID
"""

import os
import math
import time
import json
from datetime import datetime, timezone

import requests

TWELVE_DATA_KEY = os.environ.get('TWELVE_DATA_KEY', '')
FRED_KEY = os.environ.get('FRED_KEY', '')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

# ============ Configs mirrored exactly from options_levels_tracker.html ============

INSTRUMENTS = [
    {
        'id': 'XAUUSD', 'label': 'XAU/USD', 'full': 'XAU/USD \u00b7 Gold Spot',
        'symbol': 'XAU/USD', 'decimals': 2, 'multiplier': 1, 'optsSymbol': 'GLD',
        'volProfile': 'Gold', 'atrMultiple': 1.5,
        'cot': {'report': 'legacy', 'datasetId': '6dca-aqww', 'marketLike': 'GOLD - COMMODITY EXCHANGE',
                'longField': 'noncomm_positions_long_all', 'shortField': 'noncomm_positions_short_all',
                'label': 'Non-Commercial (speculators)'},
    },
    {
        'id': 'EURUSD', 'label': 'EUR/USD', 'full': 'EUR/USD \u00b7 Euro Spot',
        'symbol': 'EUR/USD', 'decimals': 5, 'multiplier': 1, 'optsSymbol': 'FXE',
        'volProfile': 'EUR-USD', 'fixedRiskAbs': 0.00075, 'fixedRiskLabel': 'Fixed 7.5 pips',
        'cot': {'report': 'tff', 'datasetId': 'gpe5-46if', 'marketLike': 'EURO FX - CHICAGO MERCANTILE EXCHANGE',
                'longField': 'lev_money_positions_long', 'shortField': 'lev_money_positions_short',
                'label': 'Leveraged Money (speculators)'},
    },
    {
        'id': 'NAS100', 'label': 'NAS100', 'full': 'NAS100 \u00b7 Nasdaq 100',
        'symbol': 'QQQ', 'decimals': 1, 'multiplier': 41, 'optsSymbol': 'QQQ',
        'volProfile': 'NQ', 'fixedRiskPct': 0.45,
        'cot': {'report': 'tff', 'datasetId': 'gpe5-46if',
                'marketLike': 'NASDAQ-100 STOCK INDEX (MINI) - CHICAGO MERCANTILE EXCHANGE',
                'longField': 'lev_money_positions_long', 'shortField': 'lev_money_positions_short',
                'label': 'Leveraged Money (speculators)'},
    },
]

VOL_PROFILE_CORRECTIONS = {
    'Gold':    {'vol': 1.00, 'hlMed': 1.0064, 'hl75': 1.0096, 'ocMed': 1.0124, 'oc75': 0.9973},
    'EUR-USD': {'vol': 1.00, 'hlMed': 0.9806, 'hl75': 1.0077, 'ocMed': 0.9904, 'oc75': 1.0111},
    'NQ':      {'vol': 1.00, 'hlMed': 0.9911, 'hl75': 1.0127, 'ocMed': 0.9917, 'oc75': 1.0336},
}
VOL_FORECAST_LOOKBACK = 105

MACRO_SERIES = [
    {'id': 'DFF', 'label': 'Fed funds rate', 'bias': {'XAUUSD': -1, 'EURUSD': -1, 'NAS100': -1}},
    {'id': 'DGS10', 'label': '10Y Treasury yield', 'bias': {'XAUUSD': -1, 'EURUSD': -1, 'NAS100': -1}},
    {'id': 'DFII10', 'label': '10Y real yield (TIPS)', 'bias': {'XAUUSD': -1, 'EURUSD': -1, 'NAS100': -1}},
    {'id': 'T10YIE', 'label': '10Y breakeven inflation', 'bias': {'XAUUSD': 1, 'EURUSD': 0, 'NAS100': 0}},
    {'id': 'DTWEXBGS', 'label': 'Trade-weighted USD index', 'bias': {'XAUUSD': -1, 'EURUSD': -1, 'NAS100': -1}},
    {'id': 'VIXCLS', 'label': 'VIX', 'bias': {'XAUUSD': 1, 'EURUSD': -1, 'NAS100': -1}},
    {'id': 'UNRATE', 'label': 'Unemployment rate', 'bias': {'XAUUSD': 1, 'EURUSD': 1, 'NAS100': 0}},
]


# ============ Twelve Data ============

def td_get(params):
    params = dict(params)
    params['apikey'] = TWELVE_DATA_KEY
    r = requests.get('https://api.twelvedata.com/time_series', params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_spot_and_intraday(inst):
    data = td_get({'symbol': inst['symbol'], 'interval': '15min', 'outputsize': 2, 'timezone': 'UTC'})
    if 'values' not in data or not data['values']:
        raise RuntimeError('No intraday data for ' + inst['symbol'] + ': ' + json.dumps(data)[:200])
    mult = inst['multiplier']
    latest = data['values'][0]
    return float(latest['close']) * mult


def get_daily_bars(inst, outputsize=120):
    data = td_get({'symbol': inst['symbol'], 'interval': '1day', 'outputsize': outputsize, 'timezone': 'UTC'})
    if 'values' not in data or not data['values']:
        raise RuntimeError('No daily data for ' + inst['symbol'])
    mult = inst['multiplier']
    bars = []
    for v in reversed(data['values']):
        bars.append({
            'date': v['datetime'][:10],
            'open': float(v['open']) * mult, 'high': float(v['high']) * mult,
            'low': float(v['low']) * mult, 'close': float(v['close']) * mult,
        })
    return bars


# ============ Yahoo Finance options (crumb-aware, same as local_proxy.py) ============

_yahoo_session = requests.Session()
_yahoo_session.headers.update({'User-Agent': UA})
_crumb_cache = {'crumb': None}


def get_yahoo_crumb():
    if _crumb_cache['crumb']:
        return _crumb_cache['crumb']
    try:
        _yahoo_session.get('https://fc.yahoo.com', timeout=10)
    except Exception:
        pass
    r = _yahoo_session.get('https://query2.finance.yahoo.com/v1/test/getcrumb', timeout=10)
    r.raise_for_status()
    crumb = r.text.strip()
    _crumb_cache['crumb'] = crumb
    return crumb


def bs_gamma(S, K, T, r, sigma):
    if not (S > 0 and K > 0 and T > 0 and sigma and sigma > 0):
        return None
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    pdf = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
    return pdf / (S * sigma * math.sqrt(T))


def yahoo_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict) and v.get('raw') is not None:
        return v['raw']
    try:
        return float(v)
    except Exception:
        return None


def get_options_levels(inst, spot):
    symbol = inst['optsSymbol']
    crumb = get_yahoo_crumb()
    url = 'https://query1.finance.yahoo.com/v7/finance/options/' + symbol
    r = _yahoo_session.get(url, params={'crumb': crumb}, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data['optionChain']['result'][0]
    underlying_px = result['quote']['regularMarketPrice']
    exp_dates = result.get('expirationDates', [])
    if not exp_dates:
        raise RuntimeError('No expirations for ' + symbol)
    now_sec = int(time.time())
    future = sorted([d for d in exp_dates if d >= now_sec])
    nearest_exp = future[0] if future else sorted(exp_dates)[-1]

    opts_block = None
    if result.get('options') and result['options'][0].get('expirationDate') == nearest_exp:
        opts_block = result['options'][0]
    if opts_block is None:
        r2 = _yahoo_session.get(url, params={'crumb': crumb, 'date': nearest_exp}, timeout=15)
        r2.raise_for_status()
        result2 = r2.json()['optionChain']['result'][0]
        opts_block = result2['options'][0]

    dte = max(0.5, (nearest_exp - now_sec) / 86400)
    T = dte / 365
    chain = []
    for c in opts_block.get('calls', []):
        strike = yahoo_num(c.get('strike'))
        iv = yahoo_num(c.get('impliedVolatility'))
        oi = yahoo_num(c.get('openInterest')) or 0
        gamma = bs_gamma(underlying_px, strike, T, 0.05, iv)
        chain.append({'strike': strike, 'type': 'call', 'oi': oi, 'gamma': gamma, 'iv': iv})
    for p in opts_block.get('puts', []):
        strike = yahoo_num(p.get('strike'))
        iv = yahoo_num(p.get('impliedVolatility'))
        oi = yahoo_num(p.get('openInterest')) or 0
        gamma = bs_gamma(underlying_px, strike, T, 0.05, iv)
        chain.append({'strike': strike, 'type': 'put', 'oi': oi, 'gamma': gamma, 'iv': iv})

    total_oi = sum(c['oi'] for c in chain)
    if total_oi == 0:
        raise RuntimeError('Zero total OI for ' + symbol + ' — stale/unavailable data')

    calls = [c for c in chain if c['type'] == 'call' and c['oi'] > 0]
    puts = [c for c in chain if c['type'] == 'put' and c['oi'] > 0]
    call_wall = max(calls, key=lambda c: c['oi']) if calls else None
    put_wall = max(puts, key=lambda c: c['oi']) if puts else None
    total_call_oi = sum(c['oi'] for c in calls)
    total_put_oi = sum(c['oi'] for c in puts)
    pc_ratio = (total_put_oi / total_call_oi) if total_call_oi > 0 else None

    strikes = sorted(set(c['strike'] for c in chain))
    min_pain, max_pain_strike = float('inf'), None
    for S in strikes:
        pain = sum((S - c['strike']) * c['oi'] for c in calls if S > c['strike'])
        pain += sum((c['strike'] - S) * c['oi'] for c in puts if S < c['strike'])
        if pain < min_pain:
            min_pain, max_pain_strike = pain, S

    ratio = spot / underlying_px if underlying_px else 1

    atm = min(chain, key=lambda c: abs(c['strike'] - underlying_px)) if chain else None
    atm_iv = atm['iv'] if atm else None
    move = underlying_px * atm_iv * math.sqrt(1 / 365) if atm_iv else None

    return {
        'callWall': (call_wall['strike'] * ratio) if call_wall else None,
        'putWall': (put_wall['strike'] * ratio) if put_wall else None,
        'maxPain': (max_pain_strike * ratio) if max_pain_strike else None,
        'pcRatio': pc_ratio,
        'expHigh': (underlying_px + move) * ratio if move else None,
        'expLow': (underlying_px - move) * ratio if move else None,
        'expiration': datetime.fromtimestamp(nearest_exp, tz=timezone.utc).strftime('%Y-%m-%d'),
    }


# ============ Vol/Range Forecast (percentile bands, same math + calibrated factors) ============

def percentile_linear(arr, p):
    s = sorted(arr)
    idx = (p / 100) * (len(s) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def compute_atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        cur, prev = bars[i], bars[i - 1]
        tr = max(cur['high'] - cur['low'], abs(cur['high'] - prev['close']), abs(cur['low'] - prev['close']))
        trs.append(tr)
    return sum(trs[-period:]) / period


def compute_vol_forecast(bars, profile):
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    today_bar = None
    if bars and bars[-1]['date'] == today_str:
        today_bar = bars.pop()

    lookback = min(VOL_FORECAST_LOOKBACK, len(bars) - 1)
    if lookback < 10:
        return None

    for_returns = bars[-(lookback + 1):]
    log_rets = [math.log(for_returns[i]['close'] / for_returns[i - 1]['close']) for i in range(1, len(for_returns))]
    for_hloc = bars[-lookback:]
    hl_pct = [(b['high'] - b['low']) / b['open'] * 100 for b in for_hloc]
    oc_pct = [abs(b['close'] - b['open']) / b['open'] * 100 for b in for_hloc]

    mean_ret = sum(log_rets) / len(log_rets)
    variance = sum((r - mean_ret) ** 2 for r in log_rets) / len(log_rets)
    raw_vol = math.sqrt(variance) * math.sqrt(252) * 100
    raw_hl_med = percentile_linear(hl_pct, 50)
    raw_hl_75 = percentile_linear(hl_pct, 75)
    raw_oc_med = percentile_linear(oc_pct, 50)
    raw_oc_75 = percentile_linear(oc_pct, 75)

    corr = VOL_PROFILE_CORRECTIONS[profile]
    oc_med = raw_oc_med * corr['ocMed']
    oc_75 = raw_oc_75 * corr['oc75']
    hl_med = raw_hl_med * corr['hlMed']
    hl_75 = raw_hl_75 * corr['hl75']

    if today_bar:
        today_open = today_bar['open']
    else:
        today_open = bars[-1]['close']

    return {
        'closeMedPlus': today_open * (1 + oc_med / 100),
        'closeMedMinus': today_open * (1 - oc_med / 100),
        'close75pPlus': today_open * (1 + oc_75 / 100),
        'close75pMinus': today_open * (1 - oc_75 / 100),
        'todayOpen': today_open,
    }


# ============ FRED macro ============

def get_fred_series(series_id):
    if not FRED_KEY:
        return None
    r = requests.get('https://api.stlouisfed.org/fred/series/observations', params={
        'series_id': series_id, 'api_key': FRED_KEY, 'file_type': 'json',
        'sort_order': 'desc', 'limit': 90,
    }, timeout=20)
    r.raise_for_status()
    data = r.json()
    obs = [o for o in data.get('observations', []) if o['value'] != '.']
    return list(reversed(obs))  # chronological


def calc_trend(values):
    if len(values) < 2:
        return 0
    recent = values[-1]
    lookback = values[max(0, len(values) - 10)]
    diff = recent - lookback
    base = abs(lookback) if abs(lookback) > 1e-9 else 1
    if abs(diff) / base < 0.002:
        return 0
    return 1 if diff > 0 else -1


def compute_macro_aggregate(inst_id):
    bull = bear = neutral = total = 0
    for s in MACRO_SERIES:
        obs = get_fred_series(s['id'])
        if not obs:
            continue
        values = [float(o['value']) for o in obs]
        trend = calc_trend(values)
        bias = s['bias'].get(inst_id, 0)
        effect = trend * bias
        total += 1
        if effect > 0:
            bull += 1
        elif effect < 0:
            bear += 1
        else:
            neutral += 1
    return {'bull': bull, 'bear': bear, 'total': total}


# ============ CFTC COT ============

def get_cot(inst):
    cot_cfg = inst['cot']
    where = "market_and_exchange_names like '%{}%'".format(cot_cfg['marketLike'])
    url = 'https://publicreporting.cftc.gov/resource/{}.json'.format(cot_cfg['datasetId'])
    r = requests.get(url, params={
        '$where': where, '$order': 'report_date_as_yyyy_mm_dd DESC', '$limit': 8,
    }, timeout=20)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None
    parsed = []
    for row in rows:
        try:
            parsed.append({
                'date': row['report_date_as_yyyy_mm_dd'][:10],
                'long': float(row[cot_cfg['longField']]),
                'short': float(row[cot_cfg['shortField']]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    if not parsed:
        return None
    latest = parsed[0]
    net = latest['long'] - latest['short']
    net_history = [p['long'] - p['short'] for p in parsed]
    extreme = None
    if len(net_history) >= 4:
        if net == max(net_history):
            extreme = 'high'
        elif net == min(net_history):
            extreme = 'low'
    return {'net': net, 'date': latest['date'], 'extreme': extreme, 'label': cot_cfg['label']}


# ============ Trade idea + scoring (mirrors computeTradeIdea / computeTotalScore) ============

def compute_stop_buffer(inst, spot, daily_bars):
    if inst.get('fixedRiskAbs') is not None:
        return inst['fixedRiskAbs'], inst.get('fixedRiskLabel', 'Fixed risk')
    if inst.get('fixedRiskPct') is not None:
        return spot * (inst['fixedRiskPct'] / 100), 'Fixed {}% risk'.format(inst['fixedRiskPct'])
    atr = compute_atr(daily_bars, 14)
    atr_mult = inst.get('atrMultiple', 2)
    if atr is not None:
        return atr * atr_mult, '{}x ATR({:.2f})'.format(atr_mult, atr)
    return spot * 0.006, 'range-based fallback'


def compute_lean(spot, opts):
    # Simplified 2-of-2 check (flip not computed server-side; uses maxPain + pcRatio)
    if opts.get('maxPain') is None and opts.get('pcRatio') is None:
        return 0
    bull, total = 0, 0
    if opts.get('maxPain') is not None:
        total += 1
        if spot > opts['maxPain']:
            bull += 1
    if opts.get('pcRatio') is not None:
        total += 1
        if opts['pcRatio'] < 1:
            bull += 1
    if total == 0:
        return 0
    return bull if bull >= total - bull else -(total - bull)


def compute_total_score(net_positioning, cot, macro):
    cot_score = 0
    if cot:
        cot_score = 1 if cot['net'] > 0 else (-1 if cot['net'] < 0 else 0)
        if cot['extreme']:
            cot_score *= 2
    net_macro = (macro['bull'] - macro['bear']) if macro['total'] > 0 else 0
    total = net_positioning + cot_score + net_macro
    direction = 'LONG' if total > 0 else ('SHORT' if total < 0 else 'NEUTRAL')
    return {'netPositioning': net_positioning, 'cotScore': cot_score, 'netMacro': net_macro,
            'macroTotal': macro['total'], 'total': total, 'direction': direction}


def fmt(v, decimals):
    if v is None:
        return '\u2014'
    return '{:,.{}f}'.format(v, decimals)


# ============ Per-instrument pipeline ============

def process_instrument(inst):
    out = {'inst': inst, 'error': None}
    try:
        spot = get_spot_and_intraday(inst)
        out['spot'] = spot
        daily_bars = get_daily_bars(inst)
        out['dailyBars'] = daily_bars

        opts = get_options_levels(inst, spot)
        out['opts'] = opts

        vf = compute_vol_forecast(list(daily_bars), inst['volProfile'])
        out['volForecast'] = vf

        buffer_, buffer_source = compute_stop_buffer(inst, spot, daily_bars)
        out['bufferSource'] = buffer_source

        if opts.get('callWall') is not None and opts.get('putWall') is not None and opts.get('maxPain') is not None:
            dist_call = abs(opts['callWall'] - spot)
            dist_put = abs(spot - opts['putWall'])
            if dist_call <= dist_put:
                direction, entry, stop = 'SELL', opts['callWall'], opts['callWall'] + buffer_
                tp2 = opts['putWall']
            else:
                direction, entry, stop = 'BUY', opts['putWall'], opts['putWall'] - buffer_
                tp2 = opts['callWall']
            out['idea'] = {'direction': direction, 'entry': entry, 'stop': stop,
                            'tp1': opts['maxPain'], 'tp2': tp2}
        else:
            out['idea'] = None

        cot = get_cot(inst)
        out['cot'] = cot

        macro = compute_macro_aggregate(inst['id'])
        out['macro'] = macro

        net_positioning = compute_lean(spot, opts)
        out['score'] = compute_total_score(net_positioning, cot, macro)

    except Exception as e:
        out['error'] = str(e)
    return out


def format_message(results):
    lines = ['<b>Market Update</b> \u2014 ' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'), '']
    for r in results:
        inst = r['inst']
        lines.append('<b>{}</b> \u2014 {}'.format(inst['full'], fmt(r.get('spot'), inst['decimals'])))
        if r['error']:
            lines.append('  \u26a0 ' + r['error'])
            lines.append('')
            continue
        score = r.get('score')
        if score:
            lines.append('{} lean (net {:+d}) \u2014 Positioning {:+d} \u00b7 COT {:+d} \u00b7 Macro {:+d}/{}'.format(
                score['direction'], score['total'], score['netPositioning'], score['cotScore'],
                score['netMacro'], score['macroTotal']))
        idea = r.get('idea')
        if idea:
            lines.append('{}: entry {}, stop {} ({})'.format(
                idea['direction'], fmt(idea['entry'], inst['decimals']), fmt(idea['stop'], inst['decimals']), r['bufferSource']))
            lines.append('TP1 (max pain) {} / TP2 ({}) {}'.format(
                fmt(idea['tp1'], inst['decimals']),
                'put wall' if idea['direction'] == 'SELL' else 'call wall',
                fmt(idea['tp2'], inst['decimals'])))
        vf = r.get('volForecast')
        if vf:
            lines.append('Vol range: Med [{}, {}] \u00b7 75p [{}, {}]'.format(
                fmt(vf['closeMedMinus'], inst['decimals']), fmt(vf['closeMedPlus'], inst['decimals']),
                fmt(vf['close75pMinus'], inst['decimals']), fmt(vf['close75pPlus'], inst['decimals'])))
        lines.append('')
    lines.append('<i>Heuristic dashboard output, not financial advice.</i>')
    return '\n'.join(lines)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError('TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set')
    url = 'https://api.telegram.org/bot{}/sendMessage'.format(TELEGRAM_BOT_TOKEN)
    r = requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get('ok'):
        raise RuntimeError('Telegram error: ' + str(data))
    return data


def main():
    if not TWELVE_DATA_KEY:
        raise SystemExit('TWELVE_DATA_KEY is not set')
    results = [process_instrument(inst) for inst in INSTRUMENTS]
    message = format_message(results)
    print(message)
    send_telegram(message)
    print('\nSent to Telegram successfully.')


if __name__ == '__main__':
    main()
