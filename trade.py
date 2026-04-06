import json
import requests
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
TARGET_PROFIT = 5.0
STOP_LOSS = -5.0
API_DELAY = 1.0       # API 유량 제한 방지 지연 (초)
POLLING_INTERVAL = 60 # 전체 루프 주기

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
# 🎨 콘솔 색상 정의
# ==============================
class Color:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    BOLD = "\033[1m"
    BG_GREEN = "\033[42m\033[30m"
    RESET = "\033[0m"

# ==============================
# 📈 분석 및 매수 함수 (순차 실행용)
# ==============================
def process_stock_analysis(stock_code, kis_shared):
    """포착된 종목을 하나씩 분석하고 매수 판단"""
    print(f"{Color.CYAN}🔍 [Analysis] {stock_code} 분석 시작...{Color.RESET}")
    
    try:
        kis = kis_shared
        notifier = DiscordNotifier()
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        # 1. 시세 데이터 수집 (API 호출 전 지연)
        sleep(API_DELAY) 
        data = kis.get_market_data(stock_code, timeframe="1")
                
        rsi = calculate_rsi(data['closes'])
        recent_volumes = [int(v) for v in data['volumes'][-5:]]
        avg_volume = int(sum(data['volumes']) / len(data['volumes']))

        # 2. AI 판단 요청
        system_prompt = (
            "You are an expert stock trader. Analyze the provided technical indicators and respond ONLY in JSON format. "
            "JSON structure: {\"action\": \"BUY\" or \"HOLD\", \"reason\": \"your analysis\"}"
        )
        
        user_input = (
            f"Analysis Request for Stock: {stock_code}\n"
            f"- Current Price: {data['price']:,.0f} KRW\n"
            f"- RSI (14): {rsi:.2f}\n"
            f"- Recent 5-period Volumes: {recent_volumes}\n"
            f"- Average Volume (30-period): {avg_volume}\n"
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

        # 결과 출력
        color = Color.BG_GREEN if action == "BUY" else Color.YELLOW
        print(f"{color}💡 [{stock_code}] AI 판단: {action}{Color.RESET} | 사유: {reason}")

        # 3. 매수 실행
        if action == "BUY":
            print(f"{Color.GREEN}{Color.BOLD}✅ [{stock_code}] 매수 주문 실행 중...{Color.RESET}")
            sleep(API_DELAY) # 매수 주문 전 지연
            kis.buy_ten_percent(stock_code)
            notifier.send("AUTO BUY", f"✅ **매수 완료: {stock_code}**\n- RSI: {rsi:.2f}\n- 사유: {reason}")
            sleep(1) # 주문 후 안정화
        else:
            print(f"{Color.YELLOW}⏳ [{stock_code}] 관망 결정{Color.RESET}")

    except Exception as e:
        print(f"{Color.RED}❌ [{stock_code}] 분석 중 에러: {e}{Color.RESET}")

# ==============================
# 🔍 조건검색 REST API
# ==============================
_SEARCH_TOKEN = None

def get_search_token():
    global _SEARCH_TOKEN
    if _SEARCH_TOKEN: return _SEARCH_TOKEN

    url = f"{BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    try:
        res = requests.post(url, json=body)
        _SEARCH_TOKEN = res.json().get("access_token")
        return _SEARCH_TOKEN
    except Exception as e:
        print(f"❌ 검색 토큰 발급 실패: {e}")
        return None

def fetch_psearch_and_run(kis_shared):
    token = get_search_token()
    if not token: return

    path = "/uapi/domestic-stock/v1/quotations/psearch-result"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHKST03900400",
        "custtype": "P"
    }
    params = {"user_id": USER_ID, "seq": "0"}

    print(f"\n{Color.MAGENTA}[*] 조건검색 업데이트 중...{Color.RESET}")
    try:
        res = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=10)
        if res.status_code != 200:
            print(f"❌ API 연결 실패: {res.status_code}")
            return

        data = res.json()
        stock_list = data.get('output2', [])
        
        if stock_list and isinstance(stock_list, list):
            print(f"✨ {len(stock_list)}개 종목 포착. 순차 분석을 시작합니다.")
            for stock in stock_list:
                code = stock.get('code')
                if code:
                    # 쓰레드 생성 대신 직접 함수 호출 (순차 실행)
                    process_stock_analysis(code, kis_shared)
        else:
            msg = data.get('msg1', '조건 일치 종목 없음')
            print(f"ℹ️ {msg}")

    except Exception as e:
        print(f"❌ 검색 API 로직 에러: {e}")

# ==============================
# 🏁 메인 루프
# ==============================
if __name__ == "__main__":
    print(f"{Color.BOLD}🚀 순차 실행 방식 자동매매 시스템 가동{Color.RESET}")
    
    main_kis = KISTools()
    sleep(2)
    
    try:
        while True:
            fetch_psearch_and_run(main_kis)
            
            print(f"\n{Color.CYAN}⏳ 한 사이클 완료. {POLLING_INTERVAL}초 대기...{Color.RESET}")
            sleep(POLLING_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n[운영 중단] 시스템을 종료합니다.")