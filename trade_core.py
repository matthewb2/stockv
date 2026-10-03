# -*- coding: utf-8 -*-
"""
주식 3분봉 HMM 국면 매매 공통 모듈.

kospi_trade.py / kosdaq_trade.py 가 이 모듈을 공유한다.
두 프로그램은 각각 별도 프로세스로 실행되며, 시장 한 곳만 담당한다.

거래 흐름 (시장별 프로그램 1개가 담당)
    1) 시장 국면 판별        — 대표종목 기준 RSI + 거래량 3-State HMM
    2) 결과로 전략 선택      — 상승 → 액티브 / 횡보·하락 → 보수적
    3) 거래대금 상위 종목 조회 — 조건검색식(seq)
    4) 종목별 상승/횡보/하락 판별
         액티브 전략 → VWMA 모델
         보수적 전략 → MACD 모델
    5) 매수 또는 매도 실행    — 로컬 모의매매

- 종목 탐색 : KIS 조건검색(psearch), 실전 계좌
- 시세/차트 : KISNative(실전 계좌) 현재가 + 3분봉, 로컬 누적 저장
- 국면 분석 : market_regime 모듈
             시장 국면은 대표종목 + RSI/거래량
             종목 국면은 전략에 따라 VWMA 또는 MACD
- 시장 구분 : 정적 시장 목록 파일(kospi.txt / kosdaq.txt)
- 주문      : KisPaperTrader 기반 로컬 모의매매 (한투 모의계좌 미사용)

kospi_trade.py  → 코스피만 담당 (조건식 seq=1)
kosdaq_trade.py → 코스닥만 담당 (조건식 seq=0)
"""
import argparse
import os
import sys
from dataclasses import dataclass, field
from time import sleep
from typing import Any

import pandas as pd
from dotenv import load_dotenv

# Windows 콘솔(cp949)에서도 이모지/한글 출력이 가능하도록 UTF-8로 재설정
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 국면 분석은 market_regime 모듈이 담당한다.
# 기존 trade_core.HMM_3STATE_* / REGIME_* 경로를 깨지 않도록 재노출한다.
# (신규 코드에서는 market_regime 에서 직접 import 할 것)
from market_regime import (  # noqa: F401
    FEATURE_BUILDERS,
    HMM_3STATE_MIN_BARS,
    HMM_3STATE_MIN_PER_STATE,
    HMM_3STATE_MIN_PER_STATE_PER_MODEL,
    HMM_3STATE_MIN_ROWS,
    HMM_3STATE_MIN_SEPARATION_PER_MODEL,
    HMM_3STATE_MIN_STATE_RATIO,
    HMM_3STATE_MIN_STATE_SEPARATION,
    HMM_3STATE_RSI_PERIOD,
    HMM_3STATE_VOLUME_MA,
    HMM_MIN_CONFIDENCE,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    MARKET_REPRESENTATIVE,
    MODEL_MACD,
    MODEL_RSI_VOLUME,
    MODEL_VWMA,
    REGIME_DOWNTREND,
    REGIME_INDICATORS,
    REGIME_LOOKBACK,
    REGIME_MAGNITUDE_GATE_K,
    REGIME_SIDEWAYS,
    REGIME_SORT_AXIS,
    REGIME_TEXT,
    REGIME_UPTREND,
    VWMA_SHORT_WINDOW,
    VWMA_SLOPE_WINDOW,
    VWMA_WINDOW,
    analyze_market_regime,
    analyze_stock_regime,
    analyze_stock_regime_macd,
    analyze_stock_regime_vwma,
    build_features_vwma,
    calculate_macd,
    calculate_macd_indicator,
    calculate_rsi,
    calculate_vwma,
    calculate_vwma_indicator,
    calculate_vwma_slope,
    calculate_vwma_spread,
    calculate_volume_ratio,
    classify_3state,
    classify_by_threshold,
    is_risk_on,
    market_representative,
    min_training_bars,
    prepare_3m_frame,
)
from scripts.chart_fetch import fetch_ticker_3m_data  # noqa: F401
from scripts.chart_store import ChartDB, current_trade_date, open_store
from scripts.console_color import Color
from scripts.kis_native import KISNative
from scripts.notifier import build_notifier
from scripts.scanner import KISScanner
from trade_execution import MIN_BUY_AMOUNT, execute_trade_logic  # noqa: F401

load_dotenv()

# ==============================
# 🔧 설정 및 파라미터
# ==============================
POLLING_INTERVAL = 180        # 스캔 주기 (초, 3분봉 기준)
DEFAULT_SEQ = 0              # 조건검색식 번호 (0 = 1번 조건식)

# 매매 파라미터
AMOUNT_RATE = 0.10           # 종목당 매수 자금 비율 (예수금의 10%)
STOP_LOSS_PCT = -1.2         # 손절 기준 (%)
TAKE_PROFIT_PCT = 1.5        # 익절 기준 (%)
# MIN_BUY_AMOUNT 은 trade_execution 에서 쓰인다 (매수 실행 로직과 함께 이동)

# 전략은 시장 국면 판정 결과로 선택한다. (2종)
#   시장 상승             → 액티브 전략  : 개별종목을 VWMA 모델로 판별, 매수 허용
#   시장 횡보 또는 하락    → 보수적 전략  : 개별종목을 MACD 모델로 판별, 매도 전용
STRATEGY_ACTIVE = "active"
STRATEGY_CONSERVATIVE = "conservative"

STRATEGY_LABEL = {
    STRATEGY_ACTIVE: "액티브 (상승장 · VWMA 모델 · 신규 매수 허용)",
    STRATEGY_CONSERVATIVE: "보수적 (횡보·하락장 · MACD 모델 · 매도 전용)",
}

# 전략별로 사용할 개별종목 국면 판별 모델
#   액티브  → VWMA : 단기/장기 VWMA 스프레드의 부호 + 지속성
#   보수적  → MACD : MACD 라인의 부호 + 지속성
STRATEGY_STOCK_MODEL = {
    STRATEGY_ACTIVE: MODEL_VWMA,
    STRATEGY_CONSERVATIVE: MODEL_MACD,
}

# 전략별 매수 허용 여부. 매도 트리거는 전략과 무관하게 항상 동작한다.
STRATEGY_ALLOW_BUY = {
    STRATEGY_ACTIVE: True,
    STRATEGY_CONSERVATIVE: False,
}

MARKET_LABEL = {
    "KOSPI": "코스피",
    "KOSDAQ": "코스닥",
}
MAX_CODES = 3               # 사이클당 분석 종목 수 (거래대금 상위)

# KRX 정보데이터 시세 파일(cp949). 각 줄의 첫 필드가 종목코드다.
MARKET_FILES = {
    "KOSPI": "kospi.txt",
    "KOSDAQ": "kosdaq.txt",
}

# 시장별 프로그램 기본 설정
#   seq           : 사용할 HTS 조건검색식 번호
#   max_codes     : 사이클당 분석 종목 수
#   filter_by_list: 정적 시장 목록(kospi.txt/kosdaq.txt)으로 한 번 더 거르는지
#
# 조건검색 1번(seq=0) : 코스닥 거래대금 상위
# 조건검색 2번(seq=1) : 코스피 거래대금 상위
# 두 조건식 모두 시장이 구분되므로 목록 재필터링을 하지 않는다.
# (kospi_trade.py / kosdaq_trade.py 의 SEQ 상수가 우선하며, 여기서는 기본값이다)
#
# 정적 목록은 코스피 이전분을 반영한 최신본이다. 조건식 결과가 목록에 없는
# 코드가 있어도 필터링하지 않고, 0개일 때만 경고를 낸다.
MARKET_SETTINGS = {
    "KOSDAQ": {"seq": DEFAULT_SEQ, "max_codes": 100, "filter_by_list": False},
    "KOSPI": {"seq": DEFAULT_SEQ + 1, "max_codes": MAX_CODES, "filter_by_list": False},
}


def market_settings(market):
    """시장별 프로그램 설정 반환"""
    return MARKET_SETTINGS.get(market, MARKET_SETTINGS["KOSPI"])


# ==============================
# 🏦 거래 컨텍스트
# ==============================
@dataclass
class TradingContext:
    native: KISNative
    store: ChartDB
    paper: Any
    notifier: Any
    market: str = "KOSPI"
    filter_by_list: bool = True
    amount_rate: float = AMOUNT_RATE
    stop_loss_pct: float = STOP_LOSS_PCT
    take_profit_pct: float = TAKE_PROFIT_PCT
    name_cache: dict = field(default_factory=dict)

    def stock_name(self, code):
        return self.name_cache.get(code, code)

    def holding_codes(self):
        return [c for c, qty in self.paper.balances.items() if qty > 0]


# ==============================
# 📈 주식 데이터 수집
# ==============================
def fetch_stock_3m_data(kis, ticker, store, min_bars=min_training_bars()):
    """3분봉 조회 후 로컬 저장소에 누적하고 누적 DataFrame 반환

    1회 호출은 30개로 제한되므로, 봉이 부족하면 과거 구간을 분할 조회한다.

    부족 여부는 원본 봉 수가 아니라 거래량 0 채움 봉을 제거한 유효 봉 수로
    판단한다. 원본 기준으로 판단하면 09:00 이전·20:00 이후의 채움 봉이
    학습 분으로 세어져 과거 조회 없이도 "충분"으로 잘못 판단한다.
    """
    df = fetch_ticker_3m_data(kis, ticker)

    if df.empty:
        return store.get_df(ticker)

    trade_date = current_trade_date()
    df = df.assign(date=trade_date)

    # 누적분이 부족하면 과거 구간을 추가로 페이징한다 (유효 봉 기준)
    if len(prepare_3m_frame(df)) < min_bars:
        rows, fetched = kis.get_3m_chart_history(ticker, min_bars=min_bars)
        if rows:
            print(f"📜 [{ticker}] 과거 구간 분할 조회로 {fetched}봉 확보")
            df = pd.DataFrame(rows).assign(date=trade_date)

    added = store.add(ticker, df.to_dict("records"))
    total = store.count(ticker)
    valid = len(prepare_3m_frame(store.get_df(ticker)))
    print(f"📥 [{ticker}] 3분봉 {added}개 신규 누적 "
          f"(보유 {total}개, 학습 가능 {valid}개)")

    return store.get_df(ticker)


# ==============================
# 🗺️ 시장 판별
# ==============================
_MARKET_LISTS = None
_MISSING_WARNED = set()


def load_market_lists(force=False):
    """정적 시장 목록 파일 로드 (최초 1회만 디스크 읽기)"""
    global _MARKET_LISTS
    if _MARKET_LISTS is not None and not force:
        return _MARKET_LISTS

    codes = {}
    base = os.path.dirname(os.path.abspath(__file__))
    for market, fname in MARKET_FILES.items():
        path = os.path.join(base, fname)
        found = set()
        if os.path.exists(path):
            # KRX 파일은 cp949 (한글 EUC-KR 확장) 로 저장되어 있다
            with open(path, encoding="cp949", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("^"):
                        continue  # 헤더 행
                    code = line.split(",")[0].strip()
                    if code:
                        found.add(code.upper())
        else:
            print(f"{Color.YELLOW}⚠️ 시장 목록 파일 없음: {path}{Color.RESET}")
        codes[market] = found

    _MARKET_LISTS = codes
    return _MARKET_LISTS


def get_stock_market(ticker, warn=True):
    """종목의 소속 시장 판별 (정적 파일 기반, 네트워크 없음)

    kospi.txt/kosdaq.txt 에 없으면 KOSPI 로 간주한다.
    (요청사항: 코스닥 종목이 아닌 종목은 코스피로 분류)
    """
    code = str(ticker).strip().upper()
    lists = load_market_lists()

    if code in lists.get("KOSDAQ", ()):
        return "KOSDAQ"

    if code in lists.get("KOSPI", ()):
        return "KOSPI"

    if warn and code not in _MISSING_WARNED:
        _MISSING_WARNED.add(code)
        print(f"{Color.YELLOW}⚠️ [{code}] 시장 목록에 없음. KOSPI로 간주합니다.{Color.RESET}")
    return "KOSPI"


# ==============================
# 🌐 시장 국면 판정
# ==============================
def get_market_state(ctx, market):
    """시장 국면 판별 (RSI 기반)

    지수 3분봉이 KIS 미지원이므로 시장 대표종목 분봉으로 판별한다.
    판별 로직은 market_regime 모듈이 담당한다.

    return: REGIME_UPTREND / REGIME_SIDEWAYS / REGIME_DOWNTREND / None
            None 은 판별 불가이며, 이 경우에도 신규 매수는 보류한다.
    """
    rep = market_representative(market)
    rep_name = ctx.stock_name(rep)

    df = fetch_stock_3m_data(ctx.native, rep, ctx.store,
                             min_bars=min_training_bars())
    regime = analyze_market_regime(rep, df)

    if regime is None:
        print(f"{Color.YELLOW}⏳ [{market}/{rep_name}] 국면 판별 불가{Color.RESET}")
        return None

    print(f"{Color.GREEN}✅ [{market}/{rep_name}] 시장 국면: "
          f"{REGIME_TEXT.get(regime, regime)}{Color.RESET}")
    return regime


def select_strategy(market_regime):
    """시장 국면 판정 결과로 매매 전략 선택

    시장이 상승이면 액티브 전략, 횡보·하락이면 보수적 전략을 고른다.

    return: STRATEGY_ACTIVE / STRATEGY_CONSERVATIVE
    """
    if is_risk_on(market_regime):
        return STRATEGY_ACTIVE
    return STRATEGY_CONSERVATIVE


def strategy_allows_buy(strategy):
    """해당 전략이 신규 매수를 허용하는지"""
    return STRATEGY_ALLOW_BUY.get(strategy, False)


def stock_model_for_strategy(strategy):
    """전략에 대응하는 개별종목 국면 판별 모델

    액티브(VWMA) / 보수적(MACD)
    """
    return STRATEGY_STOCK_MODEL.get(strategy, MODEL_RSI_VOLUME)


# ==============================
# 💱 매매 실행
# ==============================
def get_current_price(native, ticker):
    """실전 계좌 현재가 조회"""
    return float(native.get_price(ticker)['price'])


# ==============================
# 🔍 종목 분석
# ==============================
def analyze_ticker(ticker, strategy, ctx):
    """개별종목의 상승/횡보/하락을 판별해 매매 실행

    시장 국면은 이미 전략 선택에 반영되어 있으므로 여기서는 종목 국면만 본다.
    전략   : STRATEGY_ACTIVE      → VWMA 모델로 판별, 신규 매수 허용
             STRATEGY_CONSERVATIVE → MACD 모델로 판별, 신규 매수 보류
    종목국면: Downtrend → 매도 / Uptrend → 매수 / Sideways·None → 관망

    return: 주문이 실행되었으면 True
    """
    df = fetch_stock_3m_data(ctx.native, ticker, ctx.store,
                             min_bars=min_training_bars())
    if df.empty:
        return False

    name = ctx.stock_name(ticker)
    model = stock_model_for_strategy(strategy)
    print(f"📊 [{name}] 누적 {len(df)}봉 / 판별모델 {model}")
    curr_price = get_current_price(ctx.native, ticker)

    regime = analyze_stock_regime(ticker, df, model=model)
    if regime is None:
        print(f"⏳ [{name}] 국면 판별 불가로 관망합니다. (분봉 {len(df)}개)")
        return False

    execute_trade_logic(ticker, curr_price, regime, ctx,
                        allow_buy=strategy_allows_buy(strategy))
    return True


# ==============================
def run_cycle(scanner, ctx, seq, max_codes):
    """1회 스캔: 시장 국면 판별 → 전략 선택 → 거래대금 상위 N개 → 종목별 분석

    대상 시장이 아닌 종목은 이 프로그램에서 제외한다.
    (코스피 프로그램과 코스닥 프로그램이 같은 조건검색 결과를 나눠 처리)
    """
    market = ctx.market
    label = MARKET_LABEL.get(market, market)

    # 1) 시장 국면 판별
    rep = market_representative(market)
    print(f"\n🔍 [{label}] 시장 국면 판별 중... (대표종목: {ctx.stock_name(rep)})")

    market_regime = get_market_state(ctx, market)

    # 2) 결과에 따라 전략 선택
    strategy = select_strategy(market_regime)
    print(f"   적용 전략: {STRATEGY_LABEL[strategy]} [{strategy}]")

    # 3) 거래대금 상위 종목을 조건검색으로 조회
    all_items = scanner.fetch_top_by_amount(seq, top_n=max_codes)
    if not all_items:
        print(f"{Color.YELLOW}⚠️ 조건검색 결과가 없어 이번 사이클을 종료합니다.{Color.RESET}")
        return

    print(f"✨ 조건검색 결과 {len(all_items)}개 수령.")

    for item in all_items:
        code = item['code']
        if item.get('name'):
            ctx.name_cache[code] = item['name']

    if ctx.filter_by_list:
        # 조건검색식이 시장을 구분하지 않는 경우: 정적 목록으로 자기 시장만 남긴다
        items = [it for it in all_items if get_stock_market(it['code']) == market]

        if not items:
            print(f"{Color.YELLOW}⏳ 거래대금 상위 안에 {label} 종목이 없어 "
                  f"이번 사이클을 건너뜁니다.{Color.RESET}")
            return

        others = len(all_items) - len(items)
        print(f"🎯 [{label}] 대상 {len(items)}종목 (타 시장 {others}종목 제외)")
    else:
        # 조건검색식 자체가 이 시장 전용으로 저장된 경우: 목록 재필터링을 하지 않는다
        items = all_items
        known = sum(1 for it in items if it['code'] in load_market_lists().get(market, ()))
        print(f"🎯 [{label}] 대상 {len(items)}종목 "
              f"(조건검색식 seq={seq}이 {label} 전용 → 목록 재필터링 생략)")
        if known < len(items):
            print(f"ℹ️ [{label}] 정적 목록에 없는 코드 {len(items) - known}종목 "
                  f"(목록 미완성일 수 있으나 필터링하지 않음)")
        if items and known == 0:
            print(f"{Color.RED}🚨 [{label}] seq={seq} 결과 중 {label} 종목으로 확인되는 코드가 0개입니다. "
                  f"HTS 조건식 설정(대상 시장/정렬/상한)을 확인하세요.{Color.RESET}")

    # 4) 종목별 상승/횡보/하락 판별 후 매매 실행
    for idx, item in enumerate(items, start=1):
        code = item['code']
        name = ctx.stock_name(code)
        holding = code in ctx.holding_codes()
        print(f"\n---------------------------------------- ({idx}/{len(items)})")
        print(f"🔎 분석 중: {name}({code}) "
              f"거래대금 {item.get('trade_amt', 0) / 1e8:.1f}억"
              f"{' [보유 중]' if holding else ''}")

        try:
            analyze_ticker(code, strategy, ctx)
        except Exception as e:
            print(f"{Color.RED}⚠️ [{name}({code})] 분석 중 에러: {e}{Color.RESET}")


def run_loop(scanner, ctx, seq, interval, max_cycles=None, max_codes=MAX_CODES):
    label = MARKET_LABEL.get(ctx.market, ctx.market)
    print(f"{Color.BOLD}🚀 3분봉 3-State HMM 국면 매매 [{label}] 전용 가동 "
          f"(조건식 seq={seq}, 주문=로컬 모의매매){Color.RESET}")
    ctx.paper.get_status()

    print(f"\n📊 분봉 누적 현황 (3-State HMM {HMM_3STATE_MIN_BARS}개 필요):")
    summary = ctx.store.summary()
    for ticker, count, updated in summary:
        print(f"   - {ticker}: {count}봉 (최근 갱신 {updated})")

    if not summary:
        print(f"   - (없음) 분봉 {HMM_3STATE_MIN_BARS}개 이상 모이면 국면 분석이 시작됩니다.")

    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        cycle += 1
        print(f"\n{Color.CYAN}──────────── [{label}] 스캔 {cycle} ────────────{Color.RESET}")

        try:
            run_cycle(scanner, ctx, seq, max_codes)
        except Exception as e:
            print(f"{Color.RED}❌ 메인 루프 에러: {e}{Color.RESET}")

        if max_cycles is not None and cycle >= max_cycles:
            print(f"\n{Color.CYAN}⏹️ {max_cycles}회 실행 후 종료합니다.{Color.RESET}")
            break

        print(f"\n{Color.CYAN}⏳ 다음 스캔까지 {interval}초 대기...{Color.RESET}")
        sleep(interval)


# ==============================
# 🧰 의존성 팩토리
# ==============================
def build_native() -> KISNative:
    """실전 계좌 시세 조회 클라이언트 (한투 모의계좌 미사용)"""
    app_key = os.getenv("KIS_APPKEY")
    app_secret = os.getenv("KIS_SECRETKEY")
    account = os.getenv("KIS_ACCOUNT")

    if not app_key or not app_secret or not account:
        raise SystemExit("❌ KIS_APPKEY / KIS_SECRETKEY / KIS_ACCOUNT 환경변수가 필요합니다.")

    return KISNative(app_key, app_secret, account[:8], virtual=False)


def build_scanner(native, user_id) -> KISScanner:
    # 조건검색 토큰(tokenP)은 1분당 1회 발급 제한이 있으므로 실전 토큰을 공유한다.
    return KISScanner(native.base_url, native.app_key, native.app_secret, user_id,
                      virtual=False, token_provider=native.get_access_token)


def parse_args(argv=None, market="KOSPI", seq=None, max_codes=None):
    label = MARKET_LABEL.get(market, market)
    settings = market_settings(market)
    default_seq = settings["seq"] if seq is None else seq
    default_codes = settings["max_codes"] if max_codes is None else max_codes
    parser = argparse.ArgumentParser(
        description=f"3분봉 HMM 국면 매매 시스템 [{label}] (실전 시세 + 로컬 모의매매)")
    parser.add_argument("--seq", default=default_seq,
                        help=f"조건검색식 번호 (기본: {default_seq}, "
                             f"HTS {default_seq + 1}번 조건식)")
    parser.add_argument("--interval", type=int, default=POLLING_INTERVAL, help="스캔 주기(초)")
    parser.add_argument("--max-cycles", type=int, default=None, help="스캔 횟수 제한 (테스트 종료용)")
    parser.add_argument("--max-codes", type=int, default=default_codes,
                        help=f"스캔당 분석 종목 수 (기본: {default_codes})")
    parser.add_argument("--amount-rate", type=float, default=AMOUNT_RATE, help="종목당 매수 자금 비율")
    parser.add_argument("--stop-loss", type=float, default=STOP_LOSS_PCT, help="손절 기준(%%)")
    parser.add_argument("--take-profit", type=float, default=TAKE_PROFIT_PCT, help="익절 기준(%%)")

    paper = parser.add_argument_group("로컬 모의매매 옵션")
    paper.add_argument("--initial-krw", type=float, default=10_000_000, help="모의매매 초기 원금")
    # 시장별로 기본 파일명을 분리한다 (두 프로세스가 같은 파일을 쓰면 안 된다)
    paper.add_argument("--balance-file", default=f"{market.lower()}_balance.json",
                       help="잔고 파일 경로")
    paper.add_argument("--orders-file", default=f"{market.lower()}_orders.json",
                       help="체결 내역 파일 경로")

    store = parser.add_argument_group("분봉 누적 저장소 옵션")
    store.add_argument("--store-file", default=f"{market.lower()}_chart.db",
                       help="3분봉 누적 DB 경로 (.json 지정 시 자동 이관)")

    return parser.parse_args(argv)


def main(argv=None, market="KOSPI", seq=None, max_codes=None):
    """지정 시장만 담당하는 프로그램의 진입점

    seq / max_codes 를 주면 해당 시장 파일의 기본값을 덮어쓴다.
    (각 진입점의 SEQ / MAX_CODES 상수가 여기서 쓰인다)
    """
    args = parse_args(argv, market=market, seq=seq, max_codes=max_codes)

    from kis_trader import KisPaperTrader

    label = MARKET_LABEL.get(market, market)
    settings = market_settings(market)
    native = build_native()

    ctx = TradingContext(
        native=native,
        store=open_store(args.store_file),
        paper=KisPaperTrader(initial_krw=args.initial_krw,
                             balance_file=args.balance_file,
                             orders_file=args.orders_file),
        notifier=build_notifier(),
        market=market,
        filter_by_list=settings["filter_by_list"],
        amount_rate=args.amount_rate,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
    )

    print(f"{Color.BOLD}■ 대상 시장: {label} ({market}){Color.RESET}")
    print(f"   조건검색식 : seq={args.seq} (HTS {args.seq + 1}번 조건식, {label} 전용)")
    print(f"   분석 종목 : 최대 {args.max_codes}종목")
    print(f"   잔고 파일 : {args.balance_file}")
    print(f"   주문 파일 : {args.orders_file}")
    print(f"   분봉 파일 : {args.store_file}")

    try:
        run_loop(build_scanner(native, os.getenv("KIS_ID")), ctx,
                 args.seq, args.interval,
                 max_cycles=args.max_cycles, max_codes=args.max_codes)
    except KeyboardInterrupt:
        print("\n[운영 중단] 시스템을 종료합니다.")
    finally:
        ctx.paper.get_status()
        # SQLite 는 파일 락을 잡으므로 종료 전에 연결을 닫아 준다
        ctx.store.close()
