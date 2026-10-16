# -*- coding: utf-8 -*-
"""
ETF 국면 예측 LightGBM 전략 — 로컬 paper 모의매매.

해외 참조 지수와 국내 ETF 일봉(최근 1년)을 합쳐 "향후 5거래일 내 당일
고가 돌파 확률"을 LightGBM 으로 예측하고, 확률에 따라 롱/인버스 ETF 를
교체한다.

  - 확률 > 0.6 : sell_ticker_up 매도 → buy_ticker_up 매수 (상승 전환)
  - 확률 < 0.4 : buy_ticker_up 매도  → buy_ticker_down 매수 (하락 전환)
  - 그 사이    : 관망

- 시세 : 실전 계좌(KISNative) 현재가만 조회한다. 실주문/한투 모의계좌는 쓰지 않는다.
- 주문 : KisPaperTrader 기반 로컬 모의매매
         balance_etf.json / orders_etf.json 을 쓴다 (직접 편집 금지).
- 데이터 : yfinance 일봉 (참조 지수 + 학습 타겟 ETF)

사용법:
    python etf_lstm.py
    python etf_lstm.py --dry-run              # 주문 없이 판정만 출력
    python etf_lstm.py --initial-krw 10000000
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

import lightgbm as lgb
import numpy as np
import pandas as pd
import yfinance as yf
from sklearn.metrics import (accuracy_score, average_precision_score,
                             roc_auc_score)
from sklearn.preprocessing import MinMaxScaler

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
UP_THRESHOLD = 0.6          # 이 확률 초과면 상승(롱)으로 교체
DOWN_THRESHOLD = 0.4        # 이 확률 미만이면 하락(인버스)으로 교체

DEFAULT_BALANCE_FILE = "balance_etf.json"
DEFAULT_ORDERS_FILE = "orders_etf.json"

# ETF 세트 (참조 지수 / 학습 타겟 / 롱 / 인버스)
STRATEGIES = [
    # S&P
    dict(ref_ticker="^GSPC", target_ticker_yf="379800.KS",
         buy_ticker_up="379800", sell_ticker_up="252670", lookback=15),
    # 나스닥
    dict(ref_ticker="^NDX", target_ticker_yf="379810.KS",
         buy_ticker_up="379810", sell_ticker_up="252670", lookback=15),
    # 반도체
    dict(ref_ticker="^SOX", target_ticker_yf="091160.KS",
         buy_ticker_up="091160", sell_ticker_up="114800", lookback=15),
    # 원유 WTI
    dict(ref_ticker="CL=F", target_ticker_yf="261220.KS",
         buy_ticker_up="261220", sell_ticker_up="271050", lookback=15),
    # 달러
    dict(ref_ticker="KRW=X", target_ticker_yf="261240.KS",
         buy_ticker_up="261240", sell_ticker_up="261270", lookback=15),
    # 금
    dict(ref_ticker="GC=F", target_ticker_yf="132030.KS",
         buy_ticker_up="132030", sell_ticker_up="280940", lookback=15),
]


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
# 🤖 LightGBM 전략
# ==============================
def run_lgb_strategy(
    native,
    paper,
    ref_ticker="^SOX",
    target_ticker_yf="091160.KS",
    buy_ticker_up="091160",
    buy_ticker_down="251340",
    sell_ticker_up="114800",
    lookback=15,
    dry_run=False,
):
    # 1. 최근 1년 데이터 수집 설정 (365일)
    end_date = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    print(f"\n{Color.BOLD}{'=' * 64}{Color.RESET}")
    print(f"데이터 수집 기간 (최근 1년): {start_date} ~ {end_date}")
    print(f"참조 지수 티커: {ref_ticker} | 학습 타겟 티커: {target_ticker_yf}")

    # 2. OHLCV 데이터 다운로드
    df_target = yf.download(target_ticker_yf, start=start_date, end=end_date)
    df_ref = yf.download(ref_ticker, start=start_date, end=end_date)

    # MultiIndex 컬럼 구조 단순화
    if isinstance(df_target.columns, pd.MultiIndex):
        df_target.columns = df_target.columns.get_level_values(0)
    if isinstance(df_ref.columns, pd.MultiIndex):
        df_ref.columns = df_ref.columns.get_level_values(0)

    # 데이터 병합 (동일한 컬럼 구조로 유연하게 대응)
    combined = pd.DataFrame({
        "Target_Close": df_target["Close"],
        "Target_High": df_target["High"],
        "Target_Low": df_target["Low"],
        "Target_Volume": df_target["Volume"],
        "Ref_Close": df_ref["Close"],
    }).dropna()

    # 3. 피처 엔지니어링 및 타겟 설정
    combined["Ref_Return"] = combined["Ref_Close"].pct_change()
    combined["Target_Return"] = combined["Target_Close"].pct_change()

    # 타겟 설정: 향후 5거래일 최고가(High)가 당일 'High'보다 높았는가? (1: 돌파, 0: 미돌파)
    future_highest = combined["Target_High"].shift(-1).rolling(window=5).max()
    combined["Target"] = (future_highest > combined["Target_High"]).astype(int)

    # 결측치 제거
    combined = combined.dropna()

    if len(combined) < 100:
        print("경고: 최근 1년 데이터 포인트가 부족할 수 있습니다.")

    # 4. 학습/테스트 구간 분리 (시계열이므로 셔플 없이 앞 80% 를 훈련 구간으로)
    feature_columns = [
        "Target_Close", "Target_High", "Target_Low", "Target_Volume",
        "Ref_Close", "Ref_Return", "Target_Return",
    ]
    raw_features = combined[feature_columns].values
    split_idx = int(len(combined) * 0.8)

    # 스케일러는 훈련 구간 통계로만 fit 한다. 전체 데이터로 fit 하면 테스트
    # 구간의 최솟값/최댓값이 훈련에 새어 들어가 정확도가 과대평가된다.
    scaler = MinMaxScaler()
    scaler.fit(raw_features[:split_idx])
    X_data = scaler.transform(raw_features)

    y_data = combined["Target"].values  # 예측 대상은 고가 돌파 여부

    # 5. 시계열 슬라이딩 윈도우 데이터셋 생성 (LightGBM용 Flatten 적용)
    def create_lgb_dataset(X, y, lookback):
        Xs, ys = [], []
        for i in range(len(X) - lookback):
            # lookback 기간 동안의 2D 데이터를 1D 배열로 평탄화하여 횡방향으로 확장
            window_flat = X[i:(i + lookback)].flatten()
            Xs.append(window_flat)
            ys.append(y[i + lookback])
        return np.array(Xs), np.array(ys)

    X_lgb, y_lgb = create_lgb_dataset(X_data, y_data, lookback)

    # 훈련/테스트 데이터 분리 (셔플 없이 80% 훈련, 20% 테스트)
    # 윈도우 분할 지점을 행 분할 지점(split_idx)과 같은 시점으로 맞춘다.
    train_size = split_idx - lookback
    if train_size <= 0 or train_size >= len(X_lgb):
        print(f"{Color.YELLOW}⚠️ [{target_ticker_yf}] 데이터가 부족해 학습을 "
              f"건너뜁니다 (rows={len(combined)}, lookback={lookback}){Color.RESET}")
        return

    X_train_all, y_train_all = X_lgb[:train_size], y_lgb[:train_size]
    X_test, y_test = X_lgb[train_size:], y_lgb[train_size:]

    # 조기 종료용 검증 구간은 훈련 구간 끝에서 떼어낸다. 테스트 구간으로
    # 조기 종료를 하면 그 구간이 학습에 노출되어 평가가 다시 낙관적으로 된다.
    val_size = max(1, int(len(X_train_all) * 0.2))
    X_train, y_train = X_train_all[:-val_size], y_train_all[:-val_size]
    X_val, y_val = X_train_all[-val_size:], y_train_all[-val_size:]

    print(f"훈련 {X_train.shape} / 검증 {X_val.shape} / 테스트 {X_test.shape}")

    # 6. LightGBM 모델 구축 및 학습 (조기 종료 적용)
    # 타겟이 불균형(돌파 1이 대부분)하므로 class_weight 로 소수 클래스에 가중치를 준다.
    model = lgb.LGBMClassifier(
        n_estimators=100,
        learning_rate=0.05,
        random_state=42,
        verbose=-1,
        class_weight="balanced",
    )

    model.fit(
        X_train, y_train,
        eval_X=X_val,
        eval_y=y_val,
        callbacks=[lgb.early_stopping(stopping_rounds=10, verbose=False)],
    )

    # 7. 평가 및 예측
    preds = model.predict(X_test)
    probs = model.predict_proba(X_test)[:, 1]
    accuracy = accuracy_score(y_test, preds)

    # 타겟이 불균형하면 다수 클래스만 찍어도 정확도가 높게 보인다.
    # 정확도/기준선과 함께 AUC·AP 를 봐야 모델이 실제로 학습했는지 알 수 있다.
    #   - 기준선 AUC 0.5, 기준선 AP = 양성 비율(무작위 예측)
    pos_rate = float(np.mean(y_test)) if len(y_test) else float("nan")
    base_acc = max(pos_rate, 1.0 - pos_rate) if len(y_test) else float("nan")
    pred_pos_rate = float(np.mean(preds)) if len(preds) else float("nan")
    if len(y_test) and 0 < pos_rate < 1:
        auc = roc_auc_score(y_test, probs)
        ap = average_precision_score(y_test, probs)
    else:
        auc = ap = float("nan")

    print(f"\n테스트 데이터 정확도 (Accuracy): {accuracy * 100:.2f}% "
          f"/ 다수 클래스 기준선: {base_acc * 100:.2f}% "
          f"(양성 비율 {pos_rate * 100:.1f}%)")
    print(f"ROC-AUC: {auc:.3f} (기준선 0.500) / "
          f"Average Precision: {ap:.3f} (기준선 {pos_rate:.3f}) / "
          f"예측 양성 비율 {pred_pos_rate * 100:.1f}%")

    # 최신 데이터를 활용한 향후 5일 고가 돌파 예측 (lookback 기간 추출 후 Flatten)
    latest_data = X_data[-lookback:].flatten().reshape(1, -1)
    prediction_prob = model.predict_proba(latest_data)[0][1]

    print(f"\n[{target_ticker_yf} 예측 결과]")
    print(f"향후 5일 내 당일 고가 돌파 확률: {prediction_prob * 100:.2f}%")

    # 8. 예측 확률에 따라 롱/인버스 ETF 교체 (로컬 모의매매)
    if prediction_prob > UP_THRESHOLD:
        print(f"{Color.RED}[주문] 상승 예측 → {sell_ticker_up} 매도 / "
              f"{buy_ticker_up} 매수{Color.RESET}")
        rotate_position(native, paper, sell_ticker_up, buy_ticker_up,
                        "상승", dry_run)
    elif prediction_prob < DOWN_THRESHOLD:
        print(f"{Color.CYAN}[주문] 하락 예측 → {buy_ticker_up} 매도 / "
              f"{buy_ticker_down} 매수{Color.RESET}")
        rotate_position(native, paper, buy_ticker_up, buy_ticker_down,
                        "하락", dry_run)
    else:
        print(f"{Color.YELLOW}⏸️ 확률 {prediction_prob * 100:.2f}% — "
              f"관망 구간 ({DOWN_THRESHOLD} ~ {UP_THRESHOLD}){Color.RESET}")


# ==============================
# 🚀 실행
# ==============================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="ETF LightGBM 전략 — 실전 시세 + 로컬 paper 모의매매")
    parser.add_argument("--dry-run", action="store_true",
                        help="판정만 출력하고 주문을 넣지 않는다")

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

    print(f"{Color.BOLD}■ ETF LightGBM 전략 (실전 시세 + 로컬 paper 모의매매)"
          f"{Color.RESET}")
    print(f"   잔고 파일 : {args.balance_file}")
    print(f"   주문 파일 : {args.orders_file}")
    print(f"   매수 비율 : 예수금의 {BUY_RATE * 100:.0f}%")
    if args.dry_run:
        print(f"   {Color.CYAN}[dry-run] 주문을 넣지 않습니다.{Color.RESET}")
    paper.get_status()

    for params in STRATEGIES:
        try:
            run_lgb_strategy(native, paper, dry_run=args.dry_run, **params)
        except Exception as e:
            print(f"{Color.RED}⚠️ [{params['target_ticker_yf']}] 전략 실행 중 "
                  f"에러: {e}{Color.RESET}")

    print(f"\n{Color.BOLD}📋 최종 자산 현황{Color.RESET}")
    paper.get_status()
    if not args.dry_run:
        print(f"   체결 내역: {args.orders_file}")


if __name__ == "__main__":
    main()
