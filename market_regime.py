# -*- coding: utf-8 -*-
"""
3분봉 기반 국면 판별 모듈 (RSI + 거래량).

KOSPI/KOSDAQ 공통으로 쓰는 순수 분석 로직만 담는다.
매매 실행·주문·조건검색 등 거래 런타임에 의존하지 않으므로
단독 임포트해 백테스트·진단 스크립트에서 그대로 재사용할 수 있다.

모델
    시장 국면 (analyze_market_regime)
        Feature1 = RSI(14) - 50          : 매수/매도 압력 (높을수록 강세)
        Feature2 = log(거래량/거래량MA20) : 거래량 급증·둔화

    개별 종목 국면 (analyze_stock_regime, model 인자로 분기)
        MODEL_VWMA      : VWMA 3-State HMM (가격추세 + 다이버전스)
        MODEL_MACD      : MACD 라인의 부호 + 지속성 + 크기 게이트
        MODEL_RSI_VOLUME: RSI + 거래량 3-State HMM

    시장 국면 (analyze_market_regime) 은 항상 MODEL_RSI_VOLUME HMM 을 쓴다.

    VWMA 는 HMM 을 사용한다. 단일 VWMA 스프레드는 단봉 지표라 3-군집이
    안 잡히지만, 가격 변화량과 VWMA 변화량의 차이(다이버전스)를 두 축으로
    쓰면 3상태가 분리되고 수렴한다(실측 27/27 종목 수렴).
    참고: MACD 는 지표 부호 규칙 경로를 유지한다.

공개 함수
    analyze_stock_regime  : 개별종목의 상승/횡보/하락 판별 (model 선택)
    analyze_market_regime : 시장 국면 판별 (RSI+거래량 HMM, 대표종목 적용)
    classify_3state       : 3-State HMM 국면 분류 (VWMA / 시장 판별용)
    classify_by_threshold : 지표 부호 규칙 국면 분류 (MACD 용)
    prepare_3m_frame      : 거래량 0 채움 봉 제거
    calculate_rsi         : Wilder RSI
    calculate_volume_ratio: 거래량 상대비
    calculate_vwma        : 거래량 가중 이동평균
    calculate_vwma_spread : 단기/장기 VWMA 스프레드
    calculate_macd        : MACD / 시그널 / 히스토그램
    market_representative : 시장별 대표종목

주의: KIS 3분봉에는 장 시간대 밖의 거래량 0 채움 봉이 섞이므로
      분석 전에 반드시 prepare_3m_frame 으로 정제해야 한다.
"""
import sys

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM

# Windows 콘솔(cp949)에서도 이모지/한글 출력이 가능하도록 UTF-8로 재설정
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ==============================
# 🔧 모델 파라미터
# ==============================
HMM_3STATE_RSI_PERIOD = 14         # RSI 계산 기간
HMM_3STATE_VOLUME_MA = 20          # 거래량 이동평균 기간
HMM_3STATE_MIN_BARS = 120          # 유효 3분봉 최소 봉 수 (학습 60봉 + 워밍업 20봉)
HMM_3STATE_MIN_ROWS = 60           # dropna 이후 학습 최소 행 수
HMM_3STATE_MIN_PER_STATE = 5       # 상태별 최소 봉 수 (미달 시 판정 보류)
HMM_3STATE_MIN_PER_STATE_PER_MODEL = {
    # VWMA 는 3-군집이 또렷이 분리되지만(분리도 0.8~3.5) 한 국면이 짧게 지나가
    # 표본 7개에 못 미칠 수 있다. 분리도가 충분하면 라벨은 확실하므로
    # 최소 봉 수를 낮춰 판정을 살린다.
    "vwma": 2,
}
HMM_3STATE_MIN_STATE_RATIO = 0.05  # 상태별 최소 비중 (전체 봉의 5% 미만이면 보류)

VWMA_WINDOW = 20                    # VWMA 기간 (기준선)
VWMA_SHORT_WINDOW = 5               # VWMA 단기 기간 (스프레드 계산)
VWMA_SLOPE_WINDOW = 10              # VWMA 기울기 계산 기간
MACD_FAST = 12                      # MACD 단기 EMA
MACD_SLOW = 26                      # MACD 장기 EMA
MACD_SIGNAL = 9                     # MACD 시그널 EMA
HMM_3STATE_MIN_STATE_SEPARATION = 0.15  # 상태 평균 간 최소 거리 (표준화 공간)
"""3-State HMM 은 데이터에 실제로 3개 군집이 있을 때만 의미가 있다.
상태 평균이 겹치면 정렬 기반 라벨링이 뒤집힌다(강세 봉을 약세로 오인).

분리도는 '수렴 여부'가 아니라 '라벨링 신뢰도'만 검증한다.
실측(kospi_chart.db, 27개 학습 가능 종목, 표준화 공간 최소 평균 간격):
    RSI+거래량 : 0.18 ~ 1.73
    VWMA      : 0.01 ~ 0.86 (중앙 0.17)
VWMA 는 분산이 크지만 HMM 은 100% 수렴하고 상태도 3개 모두 채워진다.
평균이 가까운 일부 구간에서만 라벨이 모호해지므로, 그때는 판정을 보류한다.
"""

# VWMA / MACD 는 지표 부호 규칙으로 판별한다 (HMM 은 3-군집을 만들지 못함)
REGIME_LOOKBACK = 3            # 부호가 연속 유지되어야 하는 봉 수
REGIME_MAGNITUDE_GATE_K = 0.5  # 판정에 필요한 최소 크기 (해당 지표 표준편차 배수)

# VWMA HMM 피처 설정
VWMA_HMM_WINDOW = 12   # 가격추세/다이버전스 계산에 쓰는 봉 간격
VWMA_HMM_FAST = 5      # VWMA 모멘텀 계산용 단기 VWMA 기간
"""VWMA 를 HMM 으로 판별할 때 쓰는 피처 조합.

단순 VWMA 스프레드는 단봉 지표라 3-군집이 되지 않는다(평균이 거의 겹친다).
그래서 '가격 변화량'과 'VWMA 변화량의 차이(다이버전스)' 를 쓴다.
  Price_Trend = close.diff(12)          → 추세 방향·크기
  Div_Score   = vwma.diff(12) - 위 값   → 거래량 가중 평균이 가격보다
                                          얼마나 앞서는지(다이버전스)
두 축이 서로 다른 정보를 담으므로 3상태가 실제로 분리되고 수렴한다.
실측: 27/27 종목 수렴, 상태 3개 모두 배정됨.
"""

# 모델별 분리도 하한. 시장(RSI+거래량) 모델은 실제로 3-군집이 잘 분리된다.
# VWMA 는 두 피처의 스케일이 종목마다 달라 동일 기준이 과도하다.
# 정답지 있는 합성 데이터 29종목 측정: 분리도와 위험오판(하락→상승)의
# 상관 r=+0.26 으로 사실상 무관하고, 분리도 0.02 미만 구간이 오히려
# 위험오판이 가장 낮았다(4.6%). 즉 이 게이트는 위험을 걸러내지 못하고
# 판정률만 깎는다(실전 0.05→77%, 0.02→80%).
# 그래도 HMM 이 퇴화한 해를 막는 안전 최저선으로 아주 작은 값만 유지한다.
HMM_3STATE_MIN_SEPARATION_PER_MODEL = {
    "rsi_volume": 0.15,
    "vwma": 0.02,
}

# 상태 정렬에 쓸 Feature 인덱스. 정답지 있는 국면 전환형 합성 데이터
# 기준 Feature1(가격추세) 이 유일하게 저위험이었다. Feature2(다이버전스) 로
# 정렬하면 하락을 상승으로 보는 비율이 최대 80% 까지 올라간다.
REGIME_SORT_AXIS = {
    "rsi_volume": 0,
    "vwma": 0,
}

HMM_MIN_CONFIDENCE = 0.45  # 다음봉 상태 확률 하한 (미달이면 횡보로 간주)

# ==============================
# 📊 국면 상수
# ==============================
# 주의: HMM 라벨은 절대가락이 아니라 '해당 종목 최근 분포 대비 상대 위치'
#       다. Downtrend 는 '지금 하락 중' 이 아니라 '상장 중 상대 약화'
#       (=모멘텀 둔화) 를 뜻하며, 가격이 오르는 중에도 나올 수 있다.
REGIME_UPTREND = "Uptrend"        # 상대 강세 → 매수
REGIME_SIDEWAYS = "Sideways"      # 중립 → 관망
REGIME_DOWNTREND = "Downtrend"    # 상대 약화 → 매도

# 개별 종목 판별에 사용할 모델
MODEL_VWMA = "vwma"               # 액티브 전략 — VWMA 3-State HMM
MODEL_MACD = "macd"               # 보수적 전략 — MACD 라인 부호
MODEL_RSI_VOLUME = "rsi_volume"   # 시장 판별과 동일 (RSI + 거래량)

# 국면 판정 결과의 한글 표기
REGIME_TEXT = {
    REGIME_UPTREND: "📈 상승",
    REGIME_SIDEWAYS: "➡️ 횡보",
    REGIME_DOWNTREND: "📉 하락",
}

# 시장 대표종목 (지수 3분봉이 KIS 미지원이므로 대표종목으로 시장 국면을 판단)
MARKET_REPRESENTATIVE = {
    "KOSPI": "005930",   # 삼성전자 (코스피 시총 1위)
    "KOSDAQ": "247540",  # 에코프로비엠 (코스닥 시총 1위)
}


def market_representative(market):
    """시장별 대표종목 코드 (지수 3분봉 KIS 미지원 대체)"""
    return MARKET_REPRESENTATIVE.get(market, MARKET_REPRESENTATIVE["KOSPI"])


def min_training_bars():
    """국면 분석에 필요한 최소 유효 봉 수

    fetch_stock_3m_data 가 과거 분할 조회 여부를 판단할 때 사용한다.
    """
    return HMM_3STATE_MIN_BARS


def is_risk_on(regime):
    """신규 매수를 허용해야 하는 국면인지

    시장 국면이 상승일 때만 True 다. 횡보·하락·판별 불가에서는 매도만 한다.
    """
    return regime == REGIME_UPTREND


# ==============================
# 🧹 봉 정제 / 피처 계산
# ==============================
def prepare_3m_frame(df, ticker=None):
    """모델 입력용 3분봉 정제

    KIS 3분봉에는 장 시작 전/장 마감 뒤 거래량이 0인 채움 봉이 섞여 있다.
    이 봉은 가격이 flat이고 이동평균 분모를 0으로 만들어 dropna 이후
    학습 표본이 대량으로 소실되므로, 거래량 0 봉은 제거한다.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=["close", "volume"])

    work = df.copy()
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work["volume"] = pd.to_numeric(work["volume"], errors="coerce")

    before = len(work)
    work = work.dropna(subset=["close", "volume"])
    work = work[(work["volume"] > 0) & (work["close"] > 0)]

    dropped = before - len(work)
    if ticker and dropped:
        print(f"🧹 [{ticker}] 거래량 0 등 불필요 봉 {dropped}개 제거 → 유효 {len(work)}봉")

    return work.reset_index(drop=True)


def calculate_rsi(close, period=HMM_3STATE_RSI_PERIOD):
    """Wilder 방식 RSI (0~100)

    하락이 전혀 없으면 100, 상승/하락이 모두 없으면 50으로 보정한다.
    """
    delta = close.diff()
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)

    avg_up = up.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_down = down.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rsi = 100 - (100 / (1 + avg_up / avg_down.replace(0, np.nan)))
    rsi = rsi.mask(avg_down == 0, 100.0)
    rsi = rsi.mask((avg_down == 0) & (avg_up == 0), 50.0)
    return rsi


def calculate_volume_ratio(volume, period=HMM_3STATE_VOLUME_MA):
    """현재 거래량 / 이동평균 거래량의 로그 비율 (거래량 급증·둔화)"""
    moving = volume.rolling(window=period).mean()
    ratio = volume / moving.replace(0, np.nan)
    return np.log1p(ratio)


def calculate_vwma(close, volume, period=VWMA_WINDOW):
    """거래량 가중 이동평균 (VWMA)

    거래량이 몰린 가격대를 단순 평균 종가로 본다.
    단순 이동평균보다 실제 체결 중심에 가깝게 반응한다.
    """
    weighted = close * volume
    numerator = weighted.rolling(window=period).sum()
    denominator = volume.rolling(window=period).sum()
    return numerator / denominator.replace(0, np.nan)


def calculate_vwma_spread(close, volume, short=VWMA_SHORT_WINDOW,
                          long=VWMA_WINDOW):
    """단기 VWMA 와 장기 VWMA 의 스프레드(%) — 추세의 진동 폭이 크다

    VWMA 단선(종가 vs VWMA)은 추세장에서도 단봉형이라 3-군집으로 나뉘지 않는다.
    단기·장기 VWMA 의 간격은 추세 전환마다 크게 부호가 바뀌므로
    상승/횡보/하락 3개 군집을 실제로 만든다.
    """
    fast = calculate_vwma(close, volume, short)
    slow = calculate_vwma(close, volume, long)
    return (fast - slow) / slow.replace(0, np.nan) * 100.0


def calculate_vwma_slope(vwma, window=VWMA_SLOPE_WINDOW):
    """VWMA 의 window 봉 변화율(%) — 추세 방향과 세기(진단용)"""
    past = vwma.shift(window)
    return (vwma - past) / past.replace(0, np.nan) * 100.0


def calculate_macd(close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    """MACD (시그널·히스토그램 포함)

    return: (macd_line, signal_line, histogram) 세 개의 Series
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


# ==============================
# 🧠 HMM 피처 빌더
# ==============================
# Feature1 이 상태 정렬 기준이므로 '높을수록 강세' 로 정의한다.
def build_features_rsi_volume(work):
    """RSI + 거래량 (시장 국면 판별 모델)"""
    work["d_RSI"] = calculate_rsi(work["close"])
    work["Feature1"] = work["d_RSI"] - 50.0
    work["Feature2"] = calculate_volume_ratio(work["volume"])
    return work


def build_features_vwma(work):
    """VWMA 가격추세 + 다이버전스 (액티브 전략 종목 판별 모델)

    단일 VWMA 스프레드는 단봉 지표라 3-군집이 되지 않아 HMM 라벨이 뒤집힌다.
    가격 변화량과 VWMA 변화량의 차이를 두 축으로 쓰면 3상태가 실제로 분리된다.

    라벨은 절대가락이 아니라 '해당 종목 최근 분포 대비 상대 위치' 다.
    Uptrend/Downtrend 는 각각 '상대 강세/상대 약화' 를 뜻하므로, 가격이
    오르는 중에도 Downtrend 가 나올 수 있다. 이는 모멘텀 둔화 신호로
    의도된 동작이며 추세 추종 지표가 아니다.
    """
    vwma = calculate_vwma(work["close"], work["volume"], VWMA_HMM_FAST)
    price_trend = work["close"].diff(VWMA_HMM_WINDOW)
    vwma_trend = vwma.diff(VWMA_HMM_WINDOW)

    work["d_VWMA"] = vwma
    work["d_PriceTrend"] = price_trend
    work["d_DivScore"] = vwma_trend - price_trend
    work["Feature1"] = price_trend
    work["Feature2"] = work["d_DivScore"]
    return work


FEATURE_BUILDERS = {
    MODEL_RSI_VOLUME: build_features_rsi_volume,
    MODEL_VWMA: build_features_vwma,
}


def _describe(work):
    """표시용 d_* 컬럼을 사람이 읽을 문자열로"""
    parts = []
    for column in work.columns:
        if not column.startswith("d_"):
            continue
        value = work[column].iloc[-1]
        parts.append(f"{column[2:]}={value:+.2f}")
    return ", ".join(parts)


def calculate_vwma_indicator(close, volume):
    """VWMA 추세 지표 — 단기/장기 VWMA 스프레드(%)

    상승장에서는 양수, 하락장에서는 음수다.
    """
    return calculate_vwma_spread(close, volume)


def calculate_macd_indicator(close, volume=None):
    """MACD 추세 지표 — MACD 라인 / 종가 * 100

    히스토그램이 아니라 MACD 라인을 쓴다. 강한 상승에서 시그널 라인이
    따라붙어 히스토그램이 음수가 되는 역효과가 생기지 않는다.
    """
    macd_line, _, _ = calculate_macd(close)
    return macd_line / close.replace(0, np.nan) * 100.0


# 임계값 규칙을 쓰는 모델별 지표 (MACD 만 사용 — VWMA 는 HMM 경로)
REGIME_INDICATORS = {
    MODEL_MACD: calculate_macd_indicator,
}


def classify_by_threshold(ticker, df, scope="종목", model=MODEL_MACD):
    """지표 부호 + 지속성 + 크기 게이트로 3국면 판별 (MACD 용)

      1) 최근 REGIME_LOOKBACK 봉의 부호가 모두 같아야 하고
      2) 그 크기가 해당 지표 표준편차의 REGIME_MAGNITUDE_GATE_K 배 이상이어야 한다.
        흔들림 수준의 값은 횡보로 처리해 불필요한 매매를 막는다.

    return: REGIME_UPTREND / REGIME_SIDEWAYS / REGIME_DOWNTREND / None
    """
    if df is None or df.empty:
        return None

    indicator_fn = REGIME_INDICATORS.get(model)
    if indicator_fn is None:
        print(f"⚠️ [{ticker}] 임계값 판별을 지원하지 않는 모델 '{model}'")
        return None

    work = prepare_3m_frame(df, ticker)
    if len(work) < HMM_3STATE_MIN_BARS:
        print(f"⏳ [{ticker}] 유효 3분봉 {len(work)}개로 "
              f"{HMM_3STATE_MIN_BARS}개 미만 → 국면 판별 보류")
        return None

    series = indicator_fn(work["close"], work["volume"]).dropna()

    if len(series) < HMM_3STATE_MIN_ROWS:
        print(f"⏳ [{ticker}] {model} 지표 계산 후 {len(series)}개로 "
              f"{HMM_3STATE_MIN_ROWS}개 미만 → 국면 판별 보류")
        return None

    recent = series.tail(REGIME_LOOKBACK)
    magnitude = float(recent.abs().mean())
    gate = REGIME_MAGNITUDE_GATE_K * float(series.std())

    if magnitude < gate:
        regime = REGIME_SIDEWAYS
        reason = f"크기 {magnitude:.3f} < 기준 {gate:.3f}"
    elif bool((recent > 0).all()):
        regime = REGIME_UPTREND
        reason = f"최근 {REGIME_LOOKBACK}봉 연속 상승"
    elif bool((recent < 0).all()):
        regime = REGIME_DOWNTREND
        reason = f"최근 {REGIME_LOOKBACK}봉 연속 하락"
    else:
        regime = REGIME_SIDEWAYS
        reason = "부호 혼재"

    print(f"-> [{ticker}] {scope} 국면: {REGIME_TEXT.get(regime, regime)} "
          f"[{model}] (지표={float(series.iloc[-1]):+.3f}%, {reason})")
    return regime


# ==============================
# 📈 국면 판별
# ==============================
def classify_3state(ticker, df, scope="종목", model=MODEL_RSI_VOLUME):
    """3-State HMM 국면 분류 공통 로직

    후행성 제거: 마지막 봉의 상태(hidden_states[-1])는 '지금까지 벌어진' 결과라
    매수 판단에 1봉 늦다. 전이확률을 한 번 곱해 '다음 봉 상태'를 추정한다.

    df: 3분봉 누적 DataFrame (거래량 0 채움 봉 포함 가능)
    model: MODEL_VWMA / MODEL_MACD / MODEL_RSI_VOLUME 중 하나
    return: REGIME_UPTREND / REGIME_SIDEWAYS / REGIME_DOWNTREND / None
    """
    if df is None or df.empty:
        return None

    builder = FEATURE_BUILDERS.get(model)
    if builder is None:
        print(f"⚠️ [{ticker}] 알 수 없는 국면 모델 '{model}' → 판별 보류")
        return None

    # 거래량 0 채움 봉 제거 후 봉 수 기준을 확인한다 (정제 전 기준으로 판정)
    work = prepare_3m_frame(df, ticker)

    if len(work) < HMM_3STATE_MIN_BARS:
        print(f"⏳ [{ticker}] 유효 3분봉 {len(work)}개로 "
              f"{HMM_3STATE_MIN_BARS}개 미만 → 국면 판별 보류")
        return None

    try:
        work = builder(work)
        work = work.dropna(subset=["Feature1", "Feature2"])
        if len(work) < HMM_3STATE_MIN_ROWS:
            print(f"⏳ [{ticker}] 피처 계산 후 학습 행 {len(work)}개로 "
                  f"{HMM_3STATE_MIN_ROWS}개 미만 → 국면 판별 보류")
            return None

        X = work[["Feature1", "Feature2"]].values

        stds = np.std(X, axis=0)
        if np.any(stds == 0.0):
            # 전부 평탄하면 추세가 없다는 뜻이므로 3-상태 HMM 을 돌릴
            # 필요가 없다. 보류(None) 가 아니라 횡보를 직접 준다.
            if np.all(stds == 0.0):
                print(f"➡️ [{ticker}] {model} 모든 피처가 평탄 → 횡보 처리")
                return REGIME_SIDEWAYS
            print(f"⏳ [{ticker}] {model} 피처 중 변동이 0인 컬럼이 있어 "
                  f"국면 판별을 보류합니다.")
            return None

        # 전체 변동이 있어도 최근 구간이 평탄하면(장 마감 후 거래정지 등)
        # 그 평탄 봉이 임의의 군집에 배정되어 국면을 거꾸로 읽는다.
        # 판정에 쓰는 것은 마지막 봉이므로 최근 구간 변동을 확인한다.
        recent = X[-HMM_3STATE_MIN_PER_STATE_PER_MODEL.get(
            model, HMM_3STATE_MIN_PER_STATE):]
        if np.all(np.std(recent, axis=0) == 0.0):
            print(f"➡️ [{ticker}] {model} 최근 {len(recent)}봉 피처가 평탄 "
                  f"→ 횡보 처리")
            return REGIME_SIDEWAYS

        # 피처 스케일이 1e-6 수준이면 3상태가 구분되지 않아 상태가 붕괴한다.
        # 표준화 후 HMM에 넣는다.
        X_scaled = (X - X.mean(axis=0)) / stds

        hmm_model = GaussianHMM(n_components=3, covariance_type="diag",
                                n_iter=2000, random_state=42)
        hmm_model.fit(X_scaled)

        # 상태 정렬 기준은 REGIME_SORT_AXIS 가 밝힌 정렬 축을 쓴다.
        # 정답지 있는 합성 데이터 검증 결과 Feature1(가격추세) 정렬만
        # 저위험이었다. 표준화 공간의 means_ 순서를 그대로 믿으면 안 된다.
        sort_axis = REGIME_SORT_AXIS.get(model, 0)
        sorted_states = np.argsort(hmm_model.means_[:, sort_axis])
        down_state = sorted_states[0]       # 정렬 축이 가장 낮은 약세
        sideways_state = sorted_states[1]   # 중립 (횡보)
        up_state = sorted_states[2]         # 정렬 축이 가장 높은 강세

        state_mapping = {
            up_state: REGIME_UPTREND,
            sideways_state: REGIME_SIDEWAYS,
            down_state: REGIME_DOWNTREND,
        }

        # 군집 분리도 검증: 상태 평균이 겹치면 '강세/약세' 라벨 자체가 성립하지
        # 않는다. 겹친 상태를 정렬로 뒤집으면 강한 상승 봉이 하락으로 오인된다.
        means = hmm_model.means_
        gaps = [float(np.linalg.norm(means[i] - means[j]))
                for i in range(3) for j in range(i + 1, 3)]
        min_gap = min(gaps)
        min_gap_required = HMM_3STATE_MIN_SEPARATION_PER_MODEL.get(
            model, HMM_3STATE_MIN_STATE_SEPARATION)
        if min_gap < min_gap_required:
            print(f"⚠️ [{ticker}] {model} 3-상태 분리도 부족 "
                  f"(최소 간격 {min_gap:.2f} < "
                  f"{min_gap_required}). 국면 판별을 보류합니다.")
            return None

        hidden_states = hmm_model.predict(X_scaled)

        # 후행성 제거: 현재 봉 상태가 아니라 전이확률로 추정한 다음 봉 상태를 쓴다.
        current_probs = hmm_model.predict_proba(X_scaled)
        next_step_probs = np.dot(current_probs, hmm_model.transmat_)
        predicted_state = int(np.argmax(next_step_probs[-1]))
        confidence = float(next_step_probs[-1][predicted_state])

        # 빈 상태 처리: HMM이 봉을 배정하지 않은 상태가 있으면 그 상태의
        # means_ 는 추정이 아니므로 배정을 신뢰할 수 없다.
        counts = np.bincount(hidden_states, minlength=3)
        empty_states = [s for s in range(3) if counts[s] == 0]

        if empty_states:
            names = [state_mapping[s] for s in empty_states]
            print(f"⚠️ [{ticker}] 봉이 없는 상태 감지: {', '.join(names)} "
                  f"(분포={counts.tolist()}). 국면 판별을 보류합니다.")
            return None

        # 모든 상태가 최소 봉 수를 갖는지 확인한다.
        min_per_state = HMM_3STATE_MIN_PER_STATE_PER_MODEL.get(
            model, HMM_3STATE_MIN_PER_STATE)
        # 판정에 실제로 쓰이는 predicted_state 가 학습된 상태일 때만 국면을
        # 신뢰한다. 쓰지 않는 소수 상태가 작아도 예측 상태가 충분하면
        # 그 국면은 정상 학습된 것이므로 보류하지 않는다.
        # (실전 6종목 확인: 예외 통과분은 모두 예측 상태가 120봉 이상)
        if counts[predicted_state] < min_per_state:
            names = [state_mapping[s] for s in range(3)]
            print(f"⚠️ [{ticker}] 예측 상태({names[predicted_state]})의 봉이 "
                  f"{min_per_state}개 미만 (분포={counts.tolist()}, "
                  f"학습 {len(work)}봉). 국면 판별을 보류합니다.")
            return None

        regime = state_mapping[predicted_state]

        # 확신이 낮으면 방향을 신뢰할 수 없으므로 횡보로 낮춘다.
        if confidence < HMM_MIN_CONFIDENCE:
            print(f"⚠️ [{ticker}] 다음봉 상태 확률 {confidence:.2f} < "
                  f"{HMM_MIN_CONFIDENCE} → 횡보 처리")
            regime = REGIME_SIDEWAYS

        # 분포는 국면 순서(약세/횡보/강세)로 보여야 읽을 수 있다
        spread = [int(counts[s]) for s in (down_state, sideways_state, up_state)]
        print(f"-> [{ticker}] {scope} 국면: {REGIME_TEXT.get(regime, regime)} "
              f"[{model}] ({_describe(work)}, 약세/횡보/강세={spread}, "
              f"분리도 {min_gap:.2f}, 확률 {confidence:.2f}, "
              f"학습 {len(work)}봉)")
        return regime

    except Exception as e:
        print(f"⚠️ [{ticker}] HMM 모델 학습/추정 중 에러 발생: {e}")
        return None


def analyze_stock_regime(ticker, df, model=MODEL_RSI_VOLUME):
    """개별종목의 상승/횡보/하락 국면 판별

    시장 국면이 결정한 전략에 따라 model 이 달라진다.
      추세장(시장 상승) → MODEL_VWMA  (3-State HMM)
      그 외(횡보·하락) → MODEL_MACD  (지표 부호 규칙)

    df: 3분봉 누적 DataFrame (fetch_stock_3m_data 결과)
    return: REGIME_UPTREND / REGIME_SIDEWAYS / REGIME_DOWNTREND / None
            None 은 봉 부족·판별 불가인 경우다.
    """
    if model in REGIME_INDICATORS:
        return classify_by_threshold(ticker, df, scope="종목", model=model)
    return classify_3state(ticker, df, scope="종목", model=model)


def analyze_stock_regime_vwma(ticker, df):
    """개별종목 국면 판별 — VWMA 모델 (시장 상승 시 사용, 3-State HMM)"""
    return classify_3state(ticker, df, scope="종목", model=MODEL_VWMA)


def analyze_stock_regime_macd(ticker, df):
    """개별종목 국면 판별 — MACD 모델 (시장 횡보·하락 시 사용)"""
    return classify_by_threshold(ticker, df, scope="종목", model=MODEL_MACD)


def analyze_market_regime(ticker, df):
    """시장 국면 판별 (RSI + 거래량 기반)

    지수 3분봉이 KIS 미지원이므로 시장 대표종목 분봉으로 시장 국면을 대신한다.
    시장 판별은 항상 MODEL_RSI_VOLUME 을 사용하며, 판별 결과로 전략을 고른다.

    return: REGIME_UPTREND / REGIME_SIDEWAYS / REGIME_DOWNTREND / None
    """
    return classify_3state(ticker, df, scope="시장", model=MODEL_RSI_VOLUME)
