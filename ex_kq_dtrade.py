# -*- coding: utf-8 -*-
"""
시장 방향성(수급/지수) 로지스틱 회귀 전략 — 로컬 paper 모의매매.

market_data.csv 의 외국인/기관 순매수, DJI 수익률, FER 피처로 다음날 KOSPI
방향을 예측하고, 예측 방향에 따라 롱/인버스 ETF 를 교체한다.

  - 상승 : KODEX 코스피인버스(114800) 매도 → KODEX 레버리지(122630) 매수
  - 하락 : KODEX 레버리지(122630) 매도  → KODEX 코스피인버스(114800) 매수

- 시세 : 실전 계좌(KISNative) 현재가만 조회한다. 실주문/한투 모의계좌는 쓰지 않는다.
- 주문 : KisPaperTrader 기반 로컬 모의매매
         balance_ks_dtrade.json / orders_ks_dtrade.json 을 쓴다 (직접 편집 금지).
- 데이터 : market_data.csv (수급/지수 일별)

사용법:
    python ex_ks_dtrade.py
    python ex_ks_dtrade.py --dry-run              # 주문 없이 판정만 출력
    python ex_ks_dtrade.py --initial-krw 10000000
"""
import argparse
import datetime
import os
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Windows 콘솔(cp949)에서도 이모지/한글 출력이 가능하도록 UTF-8 로 재설정
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from kis_trader import KisPaperTrader  # noqa: E402
from scripts.console_color import Color  # noqa: E402
from trade_core import build_native, get_current_price  # noqa: E402

# ==============================
# 🔧 설정
# ==============================
BUY_RATE = 0.30             # 매수 비율: 예수금의 30% (기존 buy_percent 와 동일)

DEFAULT_DATA_FILE = "market_data.csv"
DEFAULT_BALANCE_FILE = "balance_kq_dtrade.json"
DEFAULT_ORDERS_FILE = "orders_kq_dtrade.json"

# 예측 방향별 교체 ETF (롱 레버리지 / 인버스)
LONG_TICKER = "233740"      # KODEX 레버리지
INVERSE_TICKER = "251340"   # KODEX 코스피인버스


# ==============================
# 💱 매매 실행 (로컬 모의매매)
# ==============================
def rotate_position(native, paper, exit_ticker, entry_ticker, label,
                    dry_run=False):
    """exit_ticker 전량 매도 → entry_ticker 를 예수금 BUY_RATE 만큼 매수

    - 매도 대금은 즉시 예수금에 반영되므로 매수 여력이 늘어난다.
    - dry_run 이면 현재가만 조회하고 주문은 넣지 않는다.

    return: (매도 성공 여부, 매수 주문 결과)
    """
    sold = False
    if exit_ticker:
        qty = paper.get_balance(exit_ticker)
        if qty > 0:
            price = get_current_price(native, exit_ticker)
            if dry_run:
                print(f"   {Color.CYAN}🔎 --dry-run: [{label}] {exit_ticker} "
                      f"{qty}주 매도 생략 (현재가 {price:,.0f}){Color.RESET}")
            else:
                order = paper.sell_market_order(exit_ticker, price)
                if order:
                    sold = True
                    print(f"   {Color.RED}🔻 [{label}] {exit_ticker} "
                          f"{order['qty']}주 @ {price:,.0f} 매도 "
                          f"(손익 {order['pnl_pct']:+.2f}%){Color.RESET}")
        else:
            print(f"   ℹ️ {exit_ticker} 보유 중인 종목이 아닙니다.")

    bought = None
    if entry_ticker:
        price = get_current_price(native, entry_ticker)
        budget = paper.krw * BUY_RATE
        if dry_run:
            print(f"   {Color.CYAN}🔎 --dry-run: [{label}] {entry_ticker} "
                  f"예수금 {budget:,.0f}원({BUY_RATE * 100:.0f}%) 매수 생략 "
                  f"(현재가 {price:,.0f}){Color.RESET}")
        else:
            bought = paper.buy_market_order(entry_ticker, budget, price)
            if bought:
                print(f"   {Color.GREEN}🔺 [{label}] {entry_ticker} "
                      f"{bought['qty']}주 @ {price:,.0f} 매수 "
                      f"({bought['total']:,.0f}원){Color.RESET}")
    return sold, bought


# ==============================
# 🤖 방향성 기반 자동매매 봇
# ==============================
class DirectionalTradingBot:

    def __init__(self, native, paper, target_code=LONG_TICKER,
                 data_file=DEFAULT_DATA_FILE):
        self.native = native
        self.paper = paper
        self.target_code = target_code  # 기본 대상: KODEX 레버리지
        self.data_file = data_file
        self.features = ["NASDAQ_Return", "Foreigner_Net", "Institutional_Net", "FER"]

    def load_and_preprocess_data(self, file_path=None):
        file_path = file_path or self.data_file
        try:
            df_raw = pd.read_csv(file_path, header=None, skiprows=1)
        except FileNotFoundError:
            print(f"{Color.RED}오류: '{file_path}' 파일을 찾을 수 없습니다.{Color.RESET}")
            return None

        df = pd.DataFrame()
        df["Date"] = pd.to_datetime(
            df_raw.iloc[:, 0], format="mixed", errors="coerce"
        )
        df["Foreigner_Net"] = pd.to_numeric(df_raw.iloc[:, 1], errors="coerce")
        df["Institutional_Net"] = pd.to_numeric(df_raw.iloc[:, 2], errors="coerce")
        df["NASDAQ_Return"] = pd.to_numeric(df_raw.iloc[:, 6], errors="coerce")
        df["KOSDAQ_Return"] = pd.to_numeric(
            df_raw.iloc[:, 9].replace("#DIV/0!", np.nan), errors="coerce"
        )
        df["FER"] = pd.to_numeric(
            df_raw.iloc[:, 11].replace("#DIV/0!", np.nan), errors="coerce"
        )

        return df

    def train_model(self, file_path=None):
        df = self.load_and_preprocess_data(file_path)
        if df is None:
            return None

        df_clean = df.dropna(subset=self.features + ["KOSDAQ_Return"])

        X = df_clean[self.features]
        y_ret = df_clean["KOSDAQ_Return"].shift(-1).dropna()
        X = X.iloc[: len(y_ret)]
        y_dir = (y_ret > 0).astype(int)

        clf = LogisticRegression(C=1.0, random_state=42)
        clf.fit(X, y_dir)

        return clf

    def execute_daily_trading(self, dry_run=False):
        print(
            f"{Color.CYAN}--- [즉시 실행 자동매매] 대상 종목 코드: {self.target_code}"
            f" ---{Color.RESET}"
        )

        df = self.load_and_preprocess_data()
        if df is None:
            return

        clf = self.train_model()
        if clf is None:
            return

        df_valid = df.dropna(subset=self.features)
        if df_valid.empty:
            print(f"{Color.RED}오류: 유효한 최신 시장 데이터가 없습니다.{Color.RESET}")
            return

        latest_row = df_valid.iloc[-1]
        latest_date = latest_row["Date"]
        new_data = pd.DataFrame([latest_row[self.features]], columns=self.features)

        print(
            f"📊 [최신 지표 참조일: "
            f"{str(latest_date.date()) if pd.notnull(latest_date) else '알 수 없음'}]"
        )

        pred_dir = clf.predict(new_data)[0]
        pred_proba = clf.predict_proba(new_data)[0][pred_dir] * 100

        direction = (f"{Color.RED}상승{Color.RESET}" if pred_dir == 1
                     else f"{Color.CYAN}하락{Color.RESET}")
        raw_direction_str = "상승" if pred_dir == 1 else "하락"

        current_date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{current_date_str} 예측 결과]")
        print(f"➡️ 예측 방향: {direction} (예측 확률: {pred_proba:.2f}%)")

        # 예측 방향에 따라 롱/인버스 ETF 교체 (로컬 모의매매)
        if raw_direction_str == "상승":
            print(f"{Color.RED}[주문] 상승 예측 → {INVERSE_TICKER} 매도 / "
                  f"{LONG_TICKER} 매수{Color.RESET}")
            rotate_position(self.native, self.paper, INVERSE_TICKER, LONG_TICKER,
                            "상승", dry_run)
        else:
            print(f"{Color.CYAN}[주문] 하락 예측 → {LONG_TICKER} 매도 / "
                  f"{INVERSE_TICKER} 매수{Color.RESET}")
            rotate_position(self.native, self.paper, LONG_TICKER, INVERSE_TICKER,
                            "하락", dry_run)


# ==============================
# 🚀 실행
# ==============================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="시장 방향성 로지스틱 회귀 전략 — 실전 시세 + 로컬 paper 모의매매")
    parser.add_argument("--dry-run", action="store_true",
                        help="판정만 출력하고 주문을 넣지 않는다")
    parser.add_argument("--data-file", default=DEFAULT_DATA_FILE,
                        help="수급/지수 CSV 경로")

    paper = parser.add_argument_group("로컬 모의매매 옵션")
    paper.add_argument("--initial-krw", type=float, default=10_000_000,
                       help="잔고 파일이 없을 때의 초기 원금")
    paper.add_argument("--balance-file", default=DEFAULT_BALANCE_FILE,
                       help="잔고 JSON 경로")
    paper.add_argument("--orders-file", default=DEFAULT_ORDERS_FILE,
                       help="체결 내역 JSON 경로")

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    native = build_native()
    paper = KisPaperTrader(initial_krw=args.initial_krw,
                           balance_file=args.balance_file,
                           orders_file=args.orders_file)

    print(f"{Color.BOLD}■ 시장 방향성 전략 (실전 시세 + 로컬 paper 모의매매)"
          f"{Color.RESET}")
    print(f"   데이터 파일 : {args.data_file}")
    print(f"   잔고 파일 : {args.balance_file}")
    print(f"   주문 파일 : {args.orders_file}")
    print(f"   매수 비율 : 예수금의 {BUY_RATE * 100:.0f}%")
    if args.dry_run:
        print(f"   {Color.CYAN}[dry-run] 주문을 넣지 않습니다.{Color.RESET}")
    paper.get_status()

    bot = DirectionalTradingBot(native, paper, target_code=LONG_TICKER,
                                data_file=args.data_file)
    try:
        bot.execute_daily_trading(dry_run=args.dry_run)
    except Exception as e:
        print(f"{Color.RED}[에러 발생] 매매 실행 중 예외 발생: {e}{Color.RESET}")

    print(f"\n{Color.BOLD}📋 최종 자산 현황{Color.RESET}")
    paper.get_status()
    if not args.dry_run:
        print(f"   체결 내역: {args.orders_file}")


if __name__ == "__main__":
    main()
