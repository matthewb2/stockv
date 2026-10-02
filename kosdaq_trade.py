# -*- coding: utf-8 -*-
"""
코스닥(KOSDAQ) 전용 3분봉 HMM 국면 매매 프로그램.

코스피는 kospi_trade.py 가 담당한다. 두 프로그램은 별개 프로세스로
동시에 실행할 수 있다.

조건검색식 1번(seq=0)은 HTS 서버에 코스닥 거래대금 상위 종목으로
저장되어 있으므로, 이 프로그램은 seq=0을 그대로 사용한다.
조건식 자체가 코스닥 전용이라 결과에 별도 시장 필터를 걸지 않는다.

거래 흐름
    1) 코스닥 시장 국면 판별   — 에코프로비엠(247540) 기준 RSI + 거래량 HMM
    2) 결과로 전략 선택        — 상승 → 액티브 / 횡보·하락 → 보수적
    3) 코스닥 거래대금 상위 종목 — 조건검색식 1번(seq=0)
    4) 종목별 상승/횡보/하락 판별
         액티브 전략 → VWMA 모델 / 보수적 전략 → MACD 모델
    5) 매수 또는 매도 실행      — 코스닥 로컬 모의매매

- 종목 탐색 : KIS 조건검색(psearch), 실전 계좌
- 시세/차트 : KISNative(실전 계좌) 현재가 + 3분봉, 로컬 누적 저장
- 국면 분석 : market_regime 모듈
             시장 국면 = 대표종목 + RSI/거래량
             종목 국면 = 전략에 따라 VWMA 또는 MACD
- 주문      : KisPaperTrader 기반 로컬 모의매매 (한투 모의계좌 미사용)

  # 조건검색식 1번으로 가동 (기본 100종목)
  python kosdaq_trade.py

  # 첫 실행(봉이 아직 덜 쌓인 상태) 동작 확인
  python kosdaq_trade.py --max-cycles 1

  # 코스피 프로그램과 함께 실행 (별도 터미널)
  python kospi_trade.py
"""
import trade_core

MARKET = "KOSDAQ"
SEQ = 0            # HTS 조건검색 1번 = 코스닥 거래대금 상위
MAX_CODES = 100    # 사이클당 분석 종목 수


def main(argv=None):
    trade_core.main(argv, market=MARKET, seq=SEQ, max_codes=MAX_CODES)


if __name__ == "__main__":
    main()
