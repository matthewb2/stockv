import json
import requests
import threading
import re
import os
from time import sleep
from groq import Groq
from dotenv import load_dotenv

# 기존 프로젝트 모듈
from scripts.kis_tools import KISTools
from scripts.strategy import calculate_rsi
from scripts.notifier import DiscordNotifier

load_dotenv()

# ==============================
# 🔧 설정 및 유틸리티
# ==============================
USER_ID = os.getenv("KIS_ID")
APP_KEY = os.getenv("KIS_APPKEY")
APP_SECRET = os.getenv("KIS_SECRETKEY")
BASE_URL = "https://openapi.koreainvestment.com:9443"

# 매매 파라미터
TARGET_PROFIT = 5.0   # 익절 목표 (%)
STOP_LOSS = -5.0      # 손절선 (%)
CHECK_INTERVAL = 15   # 감시 주기
API_DELAY = 1.0       # API 유량 제한 방지 지연
POLLING_INTERVAL = 60

# 중복 실행 방지용 세트 및 락
active_monitors = set()
lock = threading.Lock()

def extract_json(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match: raise ValueError("JSON 추출 실패")
    return match.group(0)

def safe_json_loads(text):
    try: 
        return json.loads(extract_json(text))
    except: 
        return {"action": "HOLD", "reason": "JSON 파싱 에러"}

# ==============================
# 📡 실시간 모니터링 & 자동 매도
# ==============================
def monitor_position(kis, stock_code, notifier):
    """보유 종목 수익률 감시 및 조건 도달 시 매도"""
    print(f"📡 [{stock_code}] 실시간 모니터링 시작...")
    
    while True:
        try:
            # 공유된 kis 객체의 계좌 세션 사용 (추가 토큰 발급 없음)
            balance = kis.account.balance()
            sleep(API_DELAY)
            
            holding = next((item for item in balance if item.symbol == stock_code), None)
            
            if not holding:
                print(f"ℹ️ [{stock_code}] 잔고 없음 (매도 완료)")
                break

            buy_price = float(getattr(holding, 'puch_avg_pric', 0) or getattr(holding, 'price', 0))
            qty = int(getattr(holding, 'qty', 0) or getattr(holding, 'hold_qty', 0))

            if buy_price <= 0:
                sleep(5)
                continue

            market_data = kis.get_market_data(stock_code)
            curr_price = float(market_data['price'])
            sleep(API_DELAY)

            # 수익률 계산 (수수료/세금 약 0.3% 반영)
            profit_rate = ((curr_price - buy_price) / buy_price) * 100 - 0.3
            print(f"📊 [{stock_code}] 수익률: {profit_rate:.2f}% | 현재가: {curr_price:,.0f}")

            if profit_rate >= TARGET_PROFIT or profit_rate <= STOP_LOSS:
                reason = "익절" if profit_rate >= TARGET_PROFIT else "손절"
                print(f"🚀 {reason} 조건 도달! 매도 실행")
                
                kis.order(stock_code, qty=qty, side="sell")
                notifier.send("AUTO SELL", f"🧪 **자동 매도 ({reason})**\n종목: {stock_code}\n수익률: {profit_rate:.2f}%")
                break

        except Exception as e:
            print(f"⚠️ [{stock_code}] 감시 중 에러: {e}")
            sleep(5)

        sleep(CHECK_INTERVAL)

# ==============================
# 🧵 분석 및 매수 스레드
# ==============================
# ==============================
# 🧵 분석 및 매수 스레드 (프롬프트 수정 버전)
# ==============================
def run_agent_thread(stock_code, kis_shared):
    """포착된 종목 분석 및 매수 판단"""
    with lock:
        if stock_code in active_monitors:
            return
        active_monitors.add(stock_code)

    print(f"🚀 [Thread] {stock_code} 에이전트 분석 시작")
    
    try:
        kis = kis_shared
        notifier = DiscordNotifier()
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        # 1. 시세 데이터 수집 (최근 3분봉 30개 데이터)
        # timeframe="3"은 KISTools에서 수정하신 분봉 간격입니다.
        data = kis.get_market_data(stock_code, timeframe="3")
        sleep(API_DELAY)
        
        rsi = calculate_rsi(data['closes'])
        
        # 최근 5개 봉의 거래량 리스트 (가독성을 위해 정수형 변환)
        recent_volumes = [int(v) for v in data['volumes'][-5:]]
        avg_volume = int(sum(data['volumes']) / len(data['volumes']))

        # 2. AI 판단 요청 (RSI + 거래량 데이터 포함)
        system_prompt = (
            "You are an expert stock trader. Analyze the provided technical indicators and respond ONLY in JSON format. "
            "JSON structure: {\"action\": \"BUY\" or \"HOLD\", \"reason\": \"your analysis\"}"
        )
        
        user_input = (
            f"Analysis Request for Stock: {stock_code}\n"
            f"- Current Price: {data['price']:,.0f} KRW\n"
            f"- RSI (14): {rsi:.2f}\n"
            f"- Recent 5-period Volumes: {recent_volumes}\n"
            f"- Average Volume (30-period): {avg_volume}\n\n"
            "Technical Guidance:\n"
            "1. RSI below 30 indicates oversold, above 70 indicates overbought.\n"
            "2. A sudden spike in volume compared to the average often confirms a price trend.\n"
            "Decision: Should I BUY or HOLD?"
        )

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input}
            ],
            temperature=0
        )

        decision = safe_json_loads(response.choices[0].message.content)
        action = decision.get("action", "HOLD").upper()
        reason = decision.get("reason", "No reason provided")

        print(f"💡 [{stock_code}] AI 판단: {action} | 사유: {reason}")

        # 3. 매수 실행 및 모니터링 전환
        if action == "BUY":
            print(f"✅ [{stock_code}] 매수 주문 실행")
            kis.buy_ten_percent(stock_code)
            notifier.send("AUTO BUY", f"✅ **매수 완료: {stock_code}**\n- RSI: {rsi:.2f}\n- 사유: {reason}")
            
            sleep(2)
            monitor_position(kis, stock_code, notifier)
        else:
            print(f"⏳ [{stock_code}] 관망 결정 (분석 종료)")

    except Exception as e:
        print(f"❌ [{stock_code}] 스레드 내부 에러: {e}")
    finally:
        with lock:
            if stock_code in active_monitors:
                active_monitors.remove(stock_code)
# ==============================
# 🔍 조건검색 REST API 폴링
# ==============================
_SEARCH_TOKEN = None

def get_search_token():
    """조건검색(실전) 전용 토큰을 가져오거나 새로 발급합니다."""
    global _SEARCH_TOKEN
    if _SEARCH_TOKEN:
        return _SEARCH_TOKEN

    print("[*] 검색용 실전 토큰 발급 중...")
    url = f"{BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": os.getenv("KIS_APPKEY"),      # 실전 AppKey
        "appsecret": os.getenv("KIS_SECRETKEY") # 실전 SecretKey
    }
    try:
        res = requests.post(url, json=body)
        res.raise_for_status()
        _SEARCH_TOKEN = res.json().get("access_token")
        return _SEARCH_TOKEN
    except Exception as e:
        print(f"❌ 검색 토큰 발급 실패: {e}")
        return None

# ==============================
# 🔍 조건검색 REST API 폴링 (최종 수정)
# ==============================
# ==============================
# 🔍 조건검색 REST API 폴링 (방어 코드 강화)
# ==============================
def fetch_psearch_and_run(kis_shared):
    """실전 토큰으로 검색하고, 포착 시 모의 객체(kis_shared)를 넘겨줌"""
    # 1. 검색 전용 실전 토큰 확보
    token = get_search_token()
    if not token: 
        print("⚠️ 검색용 토큰이 없어 폴링을 건너뜁니다.")
        return

    path = "/uapi/domestic-stock/v1/quotations/psearch-result"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": os.getenv("KIS_APPKEY"),
        "appsecret": os.getenv("KIS_SECRETKEY"),
        "tr_id": "HHKST03900400",
        "custtype": "P"
    }
    params = {"user_id": USER_ID, "seq": "0"}

    print(f"\n[*] [{USER_ID}] 조건검색 업데이트 중...")
    try:
        res = requests.get(f"{BASE_URL}{path}", headers=headers, params=params)
        
        # HTTP 에러 체크 (401, 403, 500 등)
        if res.status_code != 200:
            print(f"❌ API 연결 실패: {res.status_code} - {res.text}")
            return

        data = res.json()
        
        # 2. 토큰 만료 시 초기화 (재발급 유도)
        if data.get("msg_cd") == "EGW00123":
            print("🔄 토큰이 만료되었습니다. 다음 루프에서 재발급합니다.")
            global _SEARCH_TOKEN
            _SEARCH_TOKEN = None
            return

        # 3. 데이터 유무 확인 (안전한 필드 접근)
        # 'output2'가 존재하고 리스트 형태인지, 비어있지는 않은지 검사
        stock_list = data.get('output2', [])
        
        if stock_list and isinstance(stock_list, list):
            found_count = 0
            for stock in stock_list:
                # stock 객체 내에 code가 없는 비정상 데이터 방어
                code = stock.get('code')
                name = stock.get('name', '알 수 없는 종목')
                
                if code and code not in active_monitors:
                    print(f"✨ [포착] {name}({code}) 분석 스레드 생성")
                    found_count += 1
                    t = threading.Thread(target=run_agent_thread, args=(code, kis_shared), daemon=True)
                    t.start()
            
            if found_count == 0:
                print("ℹ️ 새로운 종목이 없습니다. (기존 종목 감시 중)")
        else:
            # 검색 결과가 없거나 API 메시지가 있는 경우
            msg = data.get('msg1', '조건 일치 종목 없음')
            print(f"ℹ️ {msg}")

    except Exception as e:
        print(f"❌ 검색 API 로직 에러: {e}")
        
# ==============================
# 🏁 메인 루프
# ==============================
if __name__ == "__main__":
    print("🚀 REST API 기반 자동매매 시스템 가동")
    
    # 1. KISTools 객체 생성 (프로그램 시작 시 단 1회, 토큰 발급 포함)
    main_kis = KISTools()
    
    try:
        while True:
            # 2. 생성된 객체를 함수들에 공유하여 실행
            fetch_psearch_and_run(main_kis)
            
            print(f"⏳ {POLLING_INTERVAL}초 후 재검색...")
            sleep(POLLING_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n[운영 중단] 시스템을 종료합니다.")