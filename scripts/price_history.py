# -*- coding: utf-8 -*-
"""
yfinance 기반 최근 N거래일 일봉 조회.

KIS 일봉 대신 yfinance 를 쓴다. 종목코드만 알면 되고 별도 인증이 없다.
한국 주식은 .KS(코스피) / .KQ(코스닥) 접미사를 붙여야 한다.

yfinance 는 선택적 의존성이므로 import 를 늦춘다.
(미설치 환경에서도 나머지 모듈 import 가 죽지 않게)
"""
OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")

SUFFIX_KOSPI = ".KS"
SUFFIX_KOSDAQ = ".KQ"


def to_yfinance_symbol(code, market="KOSPI"):
    """종목코드 → yfinance 티커

    market 가 "KOSDAQ" 이면 .KQ, 그 외에는 .KS 를 붙인다.
    """
    suffix = SUFFIX_KOSDAQ if str(market).upper() == "KOSDAQ" else SUFFIX_KOSPI
    return f"{code}{suffix}"


def _import_yfinance():
    try:
        import yfinance
    except ImportError as e:
        raise RuntimeError(
            f"yfinance 패키지가 필요합니다. (pip install yfinance) - {e}")
    return yfinance


def fetch_daily_prices(code, days=5, market="KOSPI", period=None):
    """최근 days 거래일 일봉을 과거→최신 순서로 반환

    Returns:
        list[dict]: {"date","open","high","low","close","volume"}
                    실패 시 빈 리스트
    """
    yf = _import_yfinance()
    symbol = to_yfinance_symbol(code, market)

    # period 를 넉넉히 잡고 잘라낸다 (5거래일을 얻으려면 휴장일만큼 여유 필요)
    fetch_period = period or f"{max(int(days) * 3, 10)}d"

    try:
        df = yf.Ticker(symbol).history(period=fetch_period, auto_adjust=False)
    except Exception as e:
        print(f"❌ yfinance 조회 실패 ({symbol}): {e}")
        return []

    if df is None or df.empty:
        print(f"⚠️ {symbol} 일봉 데이터가 없습니다.")
        return []

    # yfinance 는 컬럼명을 Open/Close/Volume 처럼 대문자로 준다.
    # 그대로 row.get("close") 하면 전부 None 이 되어 아무 봉도 안 담긴다.
    df.columns = [str(c).lower() for c in df.columns]
    df = df.tail(int(days))

    rows = []
    for idx, row in df.iterrows():
        close = row.get("close")
        if close is None or close != close:      # None / NaN
            continue
        rows.append({
            "date": idx.strftime("%Y-%m-%d"),
            "open": _num(row.get("open")),
            "high": _num(row.get("high")),
            "low": _num(row.get("low")),
            "close": _num(close),
            "volume": int(row.get("volume") or 0),
        })

    return rows


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def summarize_prices(rows):
    """일봉을 LLM 프롬프트용 한 줄 요약으로 만든다

    예: "10-01 71,200→72,100(+1.3%) 거래량 1,234,567 | ..."
    """
    if not rows:
        return "(일봉 데이터 없음)"

    lines = []
    for i, r in enumerate(rows):
        chg = ""
        if i > 0:
            prev = rows[i - 1]["close"]
            if prev:
                chg = f"({(r['close'] - prev) / prev * 100:+.2f}%)"
        close = f"{r['close']:,.0f}" if r["close"] is not None else "-"
        vol = f"{r['volume']:,}" if r.get("volume") else "0"
        lines.append(f"- {r['date']} 종가 {close}{chg} 거래량 {vol}")

    return "\n".join(lines)