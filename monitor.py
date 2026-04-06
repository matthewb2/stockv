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
# 계좌번호 10자리 (앞 8자리-뒤 2자리 분리)
ACCOUNT_FRONT = os.getenv("KIS_VIRTUAL_ACCOUNT")[:8]
ACCOUNT_BACK = os.getenv("KIS_VIRTUAL_ACCOUNT")[-2:]
BASE_URL = "https://openapivts.koreainvestment.com:29443"  # 모의투자 URL

TARGET_PROFIT = 5.0
STOP_LOSS = -5.0
CHECK_INTERVAL = 15
API_DELAY = 1.0

class Color:
    GREEN = "\033[92m"; YELLOW = "\033[93m"; RED = "\033[91m"
    CYAN = "\033[96m"; BG_GREEN = "\033[42m\033[30m"; RESET = "\033[0m"

# ==============================
# 📡 직접 구현한 KIS API 함수
# ==============================
def get_access_token():
    """OAuth2 토큰 발급"""
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
        "tr_id": "VTTC8434R", # 모의투자용
        "custtype": "P"
    }
    
    # 공식 문서 가이드에 따른 필수 파라미터 세팅
    params = {
        "CANO": ACCOUNT_FRONT,
        "ACNT_PRDT_CD": ACCOUNT_BACK,
        "AFHR_FLPR_YN": "N",    # 시간외단일가여부
        "OFL_YN": "",           # 공식 코드에 명시된 공란 필드 (에러 해결 포인트)
        "INQR_DVSN": "02",      # 조회구분 (02: 종목별)
        "UNPR_DVSN": "01",      # 단가구분 (01: 기본)
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",      # 처리구분 (01: 전일매매미포함)
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    
    res = requests.get(url, headers=headers, params=params)
    data = res.json()
    
    if data.get('rt_cd') == '0':
        return data.get('output1', [])
    else:
        print(f"❌ 잔고 조회 실패: {data.get('msg1')}")
        return []
        
# ==============================
# 🏁 메인 실행 로직
# ==============================
if __name__ == "__main__":
    print(f"{Color.CYAN}🚀 직접 호출 기반 순차 자동매매 시스템 가동{Color.RESET}")
    
    try:
        # 1. 초기화 (토큰 발급 및 기존 도구 세팅)
        access_token = get_access_token()
        kis = KISTools()  # 매수/매도/현재가 API용 (기존 모듈 활용)
        notifier = DiscordNotifier()
        
        if not access_token:
            print("❌ 토큰 발급 실패. 프로그램을 종료합니다.")
            exit()

        while True:
            print(f"\n{Color.YELLOW}🔍 계좌 잔고 스캔 시작...{Color.RESET}")
            
            try:
                # 2. 잔고 가져오기
                balance_list = get_balance(access_token)
                sleep(API_DELAY)
                
                if not balance_list:
                    print("현재 보유 중인 종목이 없습니다.")
                else:
                    for item in balance_list:
                        stock_code = item['pdno']         # 종목번호
                        stock_name = item['prdt_name']   # 종목명
                        qty = int(item['hldg_qty'])      # 보유수량
                        
                        if qty <= 0: continue

                        # 3. 핵심: 평균 매입가 (PCHS_AVG_PRIC)
                        buy_price = float(item['pchs_avg_pric'])
                        
                        # 4. 현재가 조회 (KISTools 활용)
                        market_data = kis.get_market_data(stock_code)
                        curr_price = float(market_data['price'])
                        
                        # 5. 수익률 계산
                        profit_rate = ((curr_price - buy_price) / buy_price) * 100 - 0.3
                        
                        color = Color.GREEN if profit_rate > 0 else Color.RED
                        print(f"📊 [{stock_name}({stock_code})] 매수가: {buy_price:,.0f} | 현재가: {curr_price:,.0f} | 수익률: {color}{profit_rate:.2f}%{Color.RESET}")

                        # 6. 매도 판단
                        if profit_rate >= TARGET_PROFIT or profit_rate <= STOP_LOSS:
                            reason = "익절" if profit_rate >= TARGET_PROFIT else "손절"
                            print(f"{Color.BG_GREEN}🚀 {reason} 조건 도달! 전량 매도 실행{Color.RESET}")
                            
                            kis.order(stock_code, qty=qty, side="sell")
                            notifier.send("AUTO SELL", f"🧪 **자동 매도 ({reason})**\n종목: {stock_name}\n수익률: {profit_rate:.2f}%")
                            sleep(API_DELAY)

                        sleep(API_DELAY) # 종목 간 지연

            except Exception as e:
                if "EGW00201" in str(e):
                    print(f"{Color.RED}⚠️ 초당 호출 제한! 10초 대기...{Color.RESET}")
                    sleep(10)
                else:
                    print(f"{Color.RED}⚠️ 에러: {e}{Color.RESET}")
                    sleep(5)
            
            print(f"\n{Color.CYAN}💤 한 사이클 완료. {CHECK_INTERVAL}초 대기...{Color.RESET}")
            sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print(f"\n{Color.RED}[운영 중단] 시스템을 종료합니다.{Color.RESET}")