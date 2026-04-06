import threading
import time
import sys

# 기존 monitor.py의 로직을 함수로 정의
def run_monitor():
    print("[시스템] Monitor 프로그램 시작...")
    try:
        # 여기에 monitor.py의 메인 루프/로직을 넣으세요
        while True:
            # 예시 로직
            # print("Monitoring...") 
            time.sleep(1) 
    except Exception as e:
        print(f"[에러] Monitor 발생: {e}")

# 기존 auto2_trade.py의 로직을 함수로 정의
def run_trade():
    print("[시스템] Auto2 Trade 프로그램 시작...")
    try:
        # 여기에 auto2_trade.py의 메인 루프/로직을 넣으세요
        while True:
            # 예시 로직
            # print("Trading...")
            time.sleep(1)
    except Exception as e:
        print(f"[에러] Trade 발생: {e}")

if __name__ == "__main__":
    # 1. 쓰레드 생성
    monitor_thread = threading.Thread(target=run_monitor, name="MonitorThread")
    trade_thread = threading.Thread(target=run_trade, name="TradeThread")

    # 2. 데몬 설정 (메인 프로그램 종료 시 함께 종료되길 원할 경우)
    # monitor_thread.daemon = True
    # trade_thread.daemon = True

    # 3. 쓰레드 시작
    monitor_thread.start()
    trade_thread.start()

    print("[시스템] 모든 쓰레드가 가동되었습니다.")

    try:
        # 메인 쓰레드가 유지되어야 서브 쓰레드들이 계속 작동함
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[시스템] 사용자에 의해 프로그램을 종료합니다.")
        sys.exit(0)