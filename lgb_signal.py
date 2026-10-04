# ==============================
# lgb_signal.py
# LightGBM 상승확률 모델 (VWMA HMM / MACD 와 병용)
# ==============================
"""
3분봉 OHLCV 로 LightGBM 앙상블을 학습해 '향후 N봉 뒤 상승 확률'을 산출한다.
VWMA 3-State HMM, MACD 와 함께 쓰며 서로 다른 정보를 본다.

    HMM/MACD : 국면(추세 방향)을 판별
    LightGBM : 다음 구간 상승 확률 + 그에 맞는 임계값

원본 코드의 열 이름/데이터 경로를 이 코드베이스에 맞춰 고친 부분
------------------------------------------------------------------
1. 원본 `pyupbit.get_ohlcv(...)` → 로컬 ChartDB (`open_store().get_df()`)
   pyupbit 은 이 프로젝트 의존성이 아니며, 실전 경로와 동일하게 맞췄다.
   DB 컬럼은 이미 소문자(open/high/low/close/volume)라 변환이 불필요하다.
   (`vwma_backtest_01.py` 의 `.capitalize()` → `Close` 는 market_regime.py 의
   `work["close"]` 관례와 어긋나므로 쓰지 않는다.)

2. 원본 `compute_rsi()` → market_regime.calculate_rsi() 재사용
   중복 구현을 없애고 RSI 정의를 한 곳에 모은다.
   차이: 원본은 단순 이동평균(Cutler), 여기는 Wilder EWM 이다.
        기존 코드베이스가 Wilder 를 쓰므로 그쪽에 맞춘다.

3. 중간 계산 컬럼은 market_regime 관례대로 `d_` 접두어를 쓴다.
   최종 피처 이름은 원본 그대로 유지해 원본과 1:1 로 대응시킨다.
   (`LGB_FEATURES` 가 단일 진실 공급원)

4. 거래량 0 채움 봉 제거 — market_regime.prepare_3m_frame 재사용
   KIS 3분봉은 장 시간대 밖 봉이 대부분 거래량 0 이다. 005930 은 459봉 중
   176봉(38%)이 이에 해당한다. 이걸 그대로 쓰면:
     - VWMA 분모(rolling volume 합)가 오염된다
     - `Volume_Change = volume.pct_change()` 가 ±inf 로 폭발한다
   반드시 제거해야 한다.

5. 예측 대상을 '진짜 최신 봉'으로 고침 (원본의 실질 버그)
   원본은 라벨이 NaN 인 마지막 10봉을 dropna 로 버린 뒤 `iloc[-1]` 을 예측한다.
   즉 예측값이 실제 최신 봉보다 `horizon` 봉만큼 과거값이다.
   여기서는 라벨이 필요 없는 최신 봉의 피처를 따로 만들어 그 봉을 예측한다.

6. 임계값을 out-of-fold 검증 예측으로 선택 (원본의 과최적화)
   원본은 학습 데이터에 대한 in-sample 예측(`train_probs_mean`)으로
   임계값을 고른다. Train AUC 와 Train Prob_AUC 를 같이 최적화하므로
   확신에 찬 과최적값이 나온다. 여기서는 TimeSeriesSplit 검증 fold
   예측으로 고른다.

7. 원본의 `test_size = 40` 은 계산만 하고 쓰이지 않는 죽은 코드였다. 제거.

주의: 현재 로컬 DB 는 2026-10-02 단일 거래일이라 학습 표본이 수백 봉이다.
      out-of-fold balanced accuracy 는 참고 수치이며 통계적으로 유의하지 않다.
      성능 주장은 다중 거래일 데이터로 다시 재야 한다.
"""
import sys

import numpy as np
import pandas as pd

# Windows 콘솔(cp949)에서도 한글 출력이 가능하도록 UTF-8 로 재설정
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from market_regime import (
    REGIME_DOWNTREND,
    REGIME_SIDEWAYS,
    REGIME_UPTREND,
    calculate_rsi,
    prepare_3m_frame,
)

try:
    import lightgbm as lgb
    from sklearn.metrics import balanced_accuracy_score
    from sklearn.model_selection import TimeSeriesSplit
except ImportError as _exc:      # 의존성 미설치 시 안내 후 해당 기능만 비활성
    lgb = None
    balanced_accuracy_score = None
    TimeSeriesSplit = None
    _IMPORT_ERROR = _exc

# ==============================
# 🔧 모델 파라미터
# ==============================
LGB_RSI_PERIOD = 14            # RSI 계산 기간
LGB_ATR_PERIOD = 14            # ATR 계산 기간
LGB_BB_PERIOD = 20             # 볼린저 밴드 기간
LGB_VOLUME_MA_SHORT = 5        # 단기 거래량 이동평균
LGB_VOLUME_MA_LONG = 20        # 장기 거래량 이동평균
LGB_HORIZON = 10               # N봉 뒤 수익률 예측
LGB_THRESHOLD_RETURN = 0.0008  # |label| 이 이보다 작으면 표본 제외(중립 구간 배제)
"""원본은 0.002(0.2%)를 썼으나 3분봉에는 너무 크다.

로컬 DB 실측(2026-10-02, horizon 10봉) 10봉 뒤 |수익률|:
    005930  50%ile 0.181%  90%ile 0.272%  → 0.2% 는 표본의 17%만 남김
    000660  50%ile 0.108%  90%ile 0.271%  → 18%
    105560  50%ile 0.060%  90%ile 0.240%  → 11%
    247540  50%ile 0.175%  90%ile 0.438%  → 38%
0.08% 로 낮추면 표본이 48~88% 남아 학습이 가능하다.
(대형주는 호가 단위 때문에 수익률이 양자화된다. 005930 은 25% 이상이
 동일 |수익률| 이다.)
"""
LGB_MIN_BARS = 150             # 학습 최소 봉 수 (원본과 동일)
LGB_MIN_TRAIN_ROWS = 50        # dropna 이후 최소 학습 행 수 (원본과 동일)
LGB_N_SPLITS = 3               # TimeSeriesSplit fold 수
LGB_TEST_SIZE = 40             # 최종 홀드아웃 봉 수 (성적 리포트용)
LGB_THRESHOLD_GRID = np.arange(0.30, 0.70, 0.01)   # 임계값 탐색 범위
LGB_NEUTRAL_PROB = 0.5         # 판단 불가 시 반환할 확률

LGB_HORIZON_MINUTES = LGB_HORIZON * 3   # 3분봉 → 분 단위 (표시용)

# 최종 피처 컬럼 (원본 명칭 유지 — 단일 진실 공급원)
LGB_FEATURES = [
    "Open_Ratio",
    "High_Ratio",
    "Low_Ratio",
    "Return_1",
    "RSI",
    "BB_Width",
    "ATR",
    "Volume_MA5",
    "Volume_MA20",
    "Volume_Ratio",
    "Volume_Change",
]


# ==============================
# 📊 보조지표
# ==============================
def compute_atr(df, window=LGB_ATR_PERIOD):
    """평균 진폭 (ATR) — 고가-저가, 고가-전종가, 저가-전종가 중 최대값의 이동평균"""
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(window=window).mean()


def build_features_lgb(work):
    """LightGBM 피처를 work 에 추가하고 돌려준다.

    원본 컬럼은 소문자 open/high/low/close/volume 을 전제로 한다.
    중간 컬럼은 market_regime 관례대로 `d_` 접두어를 쓴다.
    """
    close = work["close"]
    volume = work["volume"]

    # 0 윸로 나누지 않도록 분모를 NaN 치환한 뒤 inf → NaN → dropna 로 정리한다
    safe_close = close.replace(0, np.nan)

    # ── candle 형태 ──
    work["Open_Ratio"] = (work["open"] - close) / safe_close
    work["High_Ratio"] = (work["high"] - close) / safe_close
    work["Low_Ratio"] = (work["low"] - close) / safe_close

    # ── 수익률 / 모멘텀 ──
    work["Return_1"] = close.pct_change(1)

    # ── RSI (Wilder, market_regime 재사용) ──
    work["RSI"] = calculate_rsi(close, period=LGB_RSI_PERIOD)

    # ── 볼린저 밴드 폭 ──
    bb_middle = close.rolling(window=LGB_BB_PERIOD).mean()
    bb_std = close.rolling(window=LGB_BB_PERIOD).std()
    # (BB_Middle + 2σ) - (BB_Middle - 2σ) = 4σ
    work["BB_Width"] = 4.0 * bb_std / bb_middle.replace(0, np.nan)

    # ── ATR (가격 정규화) ──
    work["ATR"] = compute_atr(work, window=LGB_ATR_PERIOD) / safe_close

    # ── 거래량 ──
    vol_ma5 = volume.rolling(window=LGB_VOLUME_MA_SHORT).mean()
    vol_ma20 = volume.rolling(window=LGB_VOLUME_MA_LONG).mean()
    work["Volume_MA5"] = vol_ma5
    work["Volume_MA20"] = vol_ma20
    work["Volume_Ratio"] = volume / vol_ma20.replace(0, np.nan)
    # 전 봉 거래량이 0 이면 pct_change 가 ±inf → 분모를 NaN 으로 바꾼다
    work["Volume_Change"] = volume.pct_change().replace(
            [np.inf, -np.inf], np.nan)

    return work


def _build_target(work, horizon=LGB_HORIZON,
                  threshold=LGB_THRESHOLD_RETURN):
    """N봉 뒤 수익률로 이진 라벨을 만든다. 중립 구간은 표본에서 제외.

    반환: (표본 DataFrame, 판별 가능 여부)
    라벨이 없는 마지막 `horizon` 봉은 이후 예측용으로 따로 남긴다.
    """
    d_future_close = work["close"].shift(-horizon)
    future_return = (d_future_close - work["close"]) / work["close"]

    # |수익률| 이 임계값 이하인 중립 구간은 학습에서 뺀다
    decided = future_return.abs() > threshold
    labeled = work.loc[decided].copy()
    labeled["Target"] = (future_return.loc[decided] > 0).astype(int)
    return labeled


def select_threshold(y_true, probs, grid=LGB_THRESHOLD_GRID):
    """balanced accuracy 가 최대인 임계값을 반환 (out-of-fold 예측 기준)"""
    best_threshold = 0.5
    best_score = -1.0
    for threshold in grid:
        preds = (probs >= threshold).astype(int)
        score = float(balanced_accuracy_score(y_true, preds))
        if score > best_score:
            best_threshold = float(threshold)
            best_score = score
    return best_threshold, best_score


def _insufficient(return_models=False):
    """학습 불가 시 반환값. 원본 관대함(확률 0.5)을 유지한다."""
    if return_models:
        return None, LGB_NEUTRAL_PROB, 0.0, []
    return None, LGB_NEUTRAL_PROB, 0.0


# ==============================
# 🎯 학습 / 예측
# ==============================
def train_and_predict_lgb(ticker, df, return_models=False):
    """LightGBM 을 학습해 최신 봉의 상승 확률과 최적 임계값을 반환한다.

    Args:
        ticker: 종목코드 (로그용)
        df: 로컬 DB 에서 읽은 3분봉 DataFrame (소문자 컬럼)
        return_models: True 면 (prob, threshold, score, models) 반환

    Returns:
        (pred_prob_rise, best_threshold, oof_balanced_accuracy)
        학습이 불가능하면 (None, 0.5, 0.0) — 확률만 중립으로 두고
        임계값·정확도는 '없음(0)' 으로 구분해 반환한다.

    주의: 호출마다 모델을 다시 학습한다. 사이클이 여러 번 돌면 비싸므로
          동일 종목에 대한 반복 호출은 캐시를 고려할 것.
    """
    if lgb is None:
        raise ImportError(
            "lightgbm / scikit-learn 이 필요합니다: "
            f"{_IMPORT_ERROR}\n  pip install lightgbm scikit-learn"
        )

    try:
        if df is None or len(df) < LGB_MIN_BARS:
            return _insufficient(return_models)

        # ── 정제 + 피처 ──
        # 거래량 0 채움 봉을 먼저 걷어야 ATR/Volume_Change 가 깨끗해진다
        work = prepare_3m_frame(df.copy(), ticker)
        if len(work) < LGB_MIN_BARS:
            return _insufficient(return_models)

        work = build_features_lgb(work)

        # LightGBM 은 정수형 feature 를 요구한다
        work[LGB_FEATURES] = work[LGB_FEATURES].astype(np.float32)

        # ── 학습 표본 / 최신 봉 분리 ──
        labeled = _build_target(work)
        labeled = labeled.replace([np.inf, -np.inf], np.nan).dropna(
            subset=LGB_FEATURES + ["Target"])
        if len(labeled) < LGB_MIN_TRAIN_ROWS + LGB_TEST_SIZE:
            return _insufficient(return_models)

        # 최신 봉 = 라벨이 필요 없는 마지막 행. 이것을 예측 대상으로 쓴다.
        latest = work.dropna(subset=LGB_FEATURES)
        latest = latest[~latest.index.isin(labeled.index)]
        if latest.empty:
            return _insufficient(return_models)
        latest_row = latest.iloc[[-1]][LGB_FEATURES].values.astype(np.float32)

        X = labeled[LGB_FEATURES].values
        y = labeled["Target"].values.astype(int)

        # ── 마지막 LGB_TEST_SIZE 봉은 홀드아웃으로 남김 ──
        split_at = len(X) - LGB_TEST_SIZE
        X_fit, y_fit = X[:split_at], y[:split_at]
        if len(X_fit) < LGB_MIN_TRAIN_ROWS:
            return _insufficient(return_models)

        # ── TimeSeriesSplit 로 fold 학습 + out-of-fold 임계값 선택 ──
        tscv = TimeSeriesSplit(n_splits=LGB_N_SPLITS)
        models = []
        oof_probs = np.full(len(y_fit), np.nan)
        for fold, (tr, va) in enumerate(tscv.split(X_fit)):
            model = lgb.LGBMClassifier(
                n_estimators=100,
                learning_rate=0.03,
                max_depth=3,
                subsample=0.8,
                colsample_bytree=0.8,
                random_state=42 + fold,
                class_weight="balanced",
                verbose=-1,
            )
            model.fit(X_fit[tr], y_fit[tr])
            models.append(model)
            oof_probs[va] = model.predict_proba(X_fit[va])[:, 1]

        if not models:
            return _insufficient(return_models)

        # 검증 fold 에 존재하는 구간에서만 임계값을 고른다 (과최적화 방지)
        valid = ~np.isnan(oof_probs)
        if valid.sum() < 10:
            return _insufficient(return_models)
        best_threshold, oof_score = select_threshold(
            y_fit[valid], oof_probs[valid])

        # ── 앙상블 예측 ──
        probs = [m.predict_proba(latest_row)[:, 1] for m in models]
        pred_prob_rise = float(np.mean(probs))

        if return_models:
            return pred_prob_rise, best_threshold, oof_score, models
        return pred_prob_rise, best_threshold, oof_score

    except Exception as exc:
        print(f"[{ticker}] LightGBM 학습 중 예외: {exc}")
        return _insufficient(return_models)


def analyze_stock_regime_lgb(ticker, df):
    """LightGBM 상승확률을 HMM 과 같은 REGIME_* 문자열로 변환한다.

    확률 >= 임계값            → Uptrend   (상대 강세와 무관한 절대 확률)
    확률 <= 1-임계값          → Downtrend
    그 사이                   → Sideways
    학습 불가(확률 None)      → None (판별 보류)

    market_regime 의 판별 함수와 동일한 (ticker, df) 인자 순서를 따른다.
    """
    prob, threshold, score = train_and_predict_lgb(ticker, df)
    if prob is None:
        print(f"⏳ [{ticker}] LightGBM 학습 표본 부족 → 국면 판별 보류")
        return None

    if prob >= threshold:
        regime = REGIME_UPTREND
    elif prob <= 1.0 - threshold:
        regime = REGIME_DOWNTREND
    else:
        regime = REGIME_SIDEWAYS

    print(f"-> [{ticker}] LightGBM 국면: {regime} "
          f"(상승확률 {prob:.3f}, 임계 {threshold:.2f}, "
          f"OOF 정확도 {score:.3f})")
    return regime


# CLI: python lgb_signal.py --ticker 005930
if __name__ == "__main__":
    import argparse

    from scripts.chart_store import open_store
    from scripts.console_color import Color

    parser = argparse.ArgumentParser(description="LightGBM 상승확률 모델")
    parser.add_argument("--ticker", default="005930")
    parser.add_argument("--store-file", default="kospi_chart.db")
    args = parser.parse_args()

    store = open_store(args.store_file)
    try:
        df = store.get_df(args.ticker)
    finally:
        store.close()

    if df is None or df.empty:
        print(f"{args.ticker}: 저장된 3분봉 데이터 없음")
        sys.exit(1)

    print(f"{Color.BOLD}LightGBM — {args.ticker}{Color.RESET}")
    print(f"  원본 봉 {len(df)}")

    p, th, acc = train_and_predict_lgb(args.ticker, df)
    if p is None:
        print(f"  학습 불가 (최소 {LGB_MIN_BARS}봉 / 표본 부족)")
        sys.exit(1)

    print(f"  상승확률(최신 봉, {LGB_HORIZON}봉≈{LGB_HORIZON_MINUTES}분 뒤) {p:.3f}")
    print(f"  최적 임계값 {th:.2f}  (out-of-fold balanced accuracy {acc:.3f})")
    print(f"  국면 판정 {analyze_stock_regime_lgb(args.ticker, df)}")
