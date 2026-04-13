import json
import requests
import os
from time import sleep
from dotenv import load_dotenv
from scripts.kis_tools import KISTools
from scripts.notifier import DiscordNotifier

load_dotenv()

# ==============================
# 🔧 설정 및 유틸리티
# ==============================
APP_KEY = os.getenv("KIS_VIRTUAL_APPKEY")
APP_SECRET = os.getenv("KIS_VIRTUAL_SECRETKEY")
ACCOUNT_FRONT = os.getenv("KIS_VIRTUAL_ACCOUNT")[:8]
ACCOUNT_BACK = os.getenv("KIS_VIRTUAL_ACCOUNT")[-2:]
BASE_URL = "https://openapivts.koreainvestment.com:29443"

TARGET_PROFIT = 10.0
STOP_LOSS = -6.0
CHECK_INTERVAL = 15
API_DELAY = 5

class Color:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"    # 수익 (한국 기준 빨간색)
    BLUE = "\033[94m"   # 손실 (한국 기준 파란색)
    CYAN = "\033[96m"
    BG_GREEN = "\033[42m\033[30m"
    RESET = "\033[0m"

# ... (중략: get_access_token, get_balance 함수는 동일) ...

def get_access_token():
    url = f"{BASE_URL}/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    res = requests.post(url, data=json.dumps(payload))
    return res.json().get('access_token')

def get_balance(token):
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "VTTC8434R",
        "custtype": "P"
    }
    params = {
        "CANO": ACCOUNT_FRONT,
        "ACNT_PRDT_CD": ACCOUNT_BACK,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    res = requests.get(url, headers=headers, params=params, timeout=10)
    data = res.json()
    return data.get('output1', []) if data.get('rt_cd') == '0' else []

# ==============================
# 🏁 메인 실행 로직
# ==============================
if __name__ == "__main__":
    print(f"{Color.CYAN}🚀 직접 호출 기반 순차 자동매매 시스템 가동{Color.RESET}")
    
    try:
        access_token = get_access_token()
        kis = KISTools()
        notifier = DiscordNotifier()
        sleep(API_DELAY)
        if not access_token:
            print("❌ 토큰 발급 실패. 프로그램을 종료합니다.")
            exit()

        while True:
            print(f"\n{Color.YELLOW}🔍 계좌 잔고 스캔 시작...{Color.RESET}")
            
            try:
                # 1. 잔고 조회 단계
                balance_list = get_balance(access_token)
                sleep(API_DELAY)
                
                if not balance_list:
                    print("현재 보유 중인 종목이 없습니다.")
                else:
                    for item in balance_list:
                        # 📍 [핵심 수정] try-except를 for 루프 내부로 이동
                        try:
                            stock_code = item['pdno']
                            stock_name = item['prdt_name']
                            qty = int(item['hldg_qty'])
                            
                            if qty <= 0: continue
                            buy_price = float(item['pchs_avg_pric'])
                            
                            # 데이터 조회 (에러 발생 가능 지점)
                            market_data = kis.get_market_data(stock_code)
                            curr_price = float(market_data['price'])
                            
                            # 수익률 계산 및 출력
                            profit_rate = ((curr_price - buy_price) / buy_price) * 100 - 0.3
                            status_color = Color.GREEN if profit_rate > 0 else Color.RED 
                            
                            print(f"📊 [{stock_name}({stock_code})] 매수가: {buy_price:,.0f} | 현재가: {curr_price:,.0f} | 수익률: {status_color}{profit_rate:.2f}%{Color.RESET}")

                            # 매도 판단
                            if profit_rate >= TARGET_PROFIT or profit_rate <= STOP_LOSS:
                                reason = "익절" if profit_rate >= TARGET_PROFIT else "손절"
                                sell_color = Color.GREEN if reason == "익절" else Color.RED
                                
                                print(f"{sell_color}🚀 {reason} 조건 도달! 전량 매도 실행{Color.RESET}")
                                kis.order(stock_code, qty=qty, side="sell")
                                notifier.send("AUTO SELL", f"🧪 **자동 매도 ({reason})**\n종목: {stock_name}\n수익률: {profit_rate:.2f}%")
                            
                            # 📍 종목 간 TPS 방어용 지연 (매 루프마다 실행)
                            sleep(API_DELAY)

                        except Exception as e:
                            # 📍 개별 종목 에러 처리: 여기서 잡히면 다음 'item'으로 넘어감
                            print(f"{Color.RED}⚠️ [{item.get('prdt_name', 'Unknown')}] 처리 중 오류: {e}{Color.RESET}")
                            print(f"{Color.YELLOW}🔄 다음 종목으로 건너뜁니다...{Color.RESET}")
                            sleep(1) # 잠시 휴식 후 다음 종목 진행
                            continue 

            except Exception as e:
                # 📍 잔고 조회(get_balance) 자체가 실패했을 때의 전체 에러 처리
                print(f"{Color.RED}⚠️ 계좌 스캔 중 치명적 오류: {e}{Color.RESET}")
            
            print(f"\n{Color.CYAN}💤 한 사이클 완료. {CHECK_INTERVAL}초 대기...{Color.RESET}")
            sleep(CHECK_INTERVAL)
            
    except KeyboardInterrupt:
        print(f"\n{Color.RED}[운영 중단] 시스템을 종료합니다.{Color.RESET}")