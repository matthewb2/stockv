# -*- coding: utf-8 -*-
"""
주식 3분봉 기반 HMM 국면 매매 시스템 - 구버전 진입점(사용하지 않음).

이 파일은 kospi_trade.py / kosdaq_trade.py 로 분리되었습니다.
하나의 프로세스에서 두 시장을 함께 처리하면 시장 국면·분봉·모의매매 상태가
뒤섞이므로, 시장별로 별도 프로세스를 사용합니다.

  python kospi_trade.py     # 코스피 전용
  python kosdaq_trade.py    # 코스닥 전용

공통 로직은 trade_core.py 에 있습니다.
"""
import sys

MESSAGE = """\
main.py 는 더 이상 사용하지 않습니다. 시장별로 분리된 프로그램을 실행하세요.

  python kospi_trade.py     # 코스피 전용
  python kosdaq_trade.py    # 코스닥 전용

두 프로그램은 각각 별도 프로세스로 동시에 실행할 수 있습니다.
공통 로직은 trade_core.py 에 있습니다.
"""

if __name__ == "__main__":
    print(MESSAGE)
    sys.exit(1)
