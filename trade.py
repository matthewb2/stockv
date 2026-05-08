import json
import requests
import re
import os
from time import sleep
import time
from groq import Groq
from dotenv import load_dotenv

# 기존 프로젝트 모듈
from scripts.kis_tools import KISTools
from scripts.scanner import KISScanner, Color  # 분리된 모듈 임포트
from scripts.strategy import calculate_rsi
from scripts.notifier import DiscordNotifier

load_dotenv()

# ==============================
# 🔧 설정 및 파라미터
# ==============================
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
# 📈 분석 및 매수 함수
# ==============================

def process_stock_analysis(stock_code, kis):
    """포착된 종목을 KISTools를 사용하여 분석하고 매수 판단"""
    try:
        notifier = DiscordNotifier()
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        # 0. 중복 매수 방지 (KISTools 내부의 안전 호출 사용)
        holding_codes = kis.account.balance()
        if stock_code in holding_codes:
            print(f"{Color.YELLOW}⚠️ [{stock_code}] 이미 잔고에 보유 중인 종목입니다. 건너뜁니다.{Color.RESET}")
            return

        # 1. 시세 데이터 수집 (KISTools 내부에서 이미 _call_api_safe가 적용됨)
        data = kis.get_market_data(stock_code, timeframe="D")
        
        stock_name = data['stock_name']        
        print(f"{Color.CYAN}🔍 [Analysis] [{stock_name}({stock_code})] 분석 시작...{Color.RESET}")
        
        # 지표 계산
        rsi = calculate_rsi(data['closes'])
        recent_volumes = [int(v) for v in data['volumes'][-5:]]
        avg_volume = int(sum(data['volumes']) / len(data['volumes']))

        # 2. AI 판단 요청
        system_prompt = (
            "You are an expert stock trader. Analyze technical indicators and respond ONLY in JSON. "
            "Structure: {\"action\": \"BUY\" or \"HOLD\", \"reason\": \"...\"}"
        )
        user_input = (
            f"Stock: {stock_code}, Price: {data['price']:,.0f}\n"
            f"RSI: {rsi:.2f}, Volumes: {recent_volumes}, Avg: {avg_volume}"
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

        color = Color.BG_GREEN if action == "BUY" else Color.YELLOW
        print(f"{color}💡 [{stock_code}] AI 판단: {action}{Color.RESET} | 사유: {reason}")

        # 3. 매수 실행
        if action == "BUY":
            print(f"{Color.GREEN}✅ [{stock_code}] 매수 주문 실행 중...{Color.RESET}")
            # KISTools의 buy_ten_percent 내부에 _call_api_safe가 적용되어 있습니다.
            order_res = kis.buy_ten_percent(stock_code)
            
            if order_res:
                notifier.send("AUTO BUY", f"✅ **매수 완료: {stock_name}**\n- 사유: {reason}")
        else:
            print(f"{Color.YELLOW}⏳ [{stock_code}] 관망 결정{Color.RESET}")

    except Exception as e:
        print(f"{Color.RED}⚠️ [{stock_code}] 분석 중 오류: {e}{Color.RESET}")

def get_balance(token):
    url = f"https://openapivts.koreainvestment.com:29443/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "VTTC8434R", # 모의투자 잔고 tr_id
        "custtype": "P"
    }
    params = {
        "CANO": ACCOUNT_FRONT, "ACNT_PRDT_CD": ACCOUNT_BACK,
        "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
        "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""
    }
    res = requests.get(url, headers=headers, params=params)
    data = res.json()
    return data.get('output1', []) if data.get('rt_cd') == '0' else []

# ==============================
# 🏁 메인 루프
# ==============================
if __name__ == "__main__":
    print(f"{Color.BOLD}🚀 KISTools 통합형 자동매매 시스템 가동{Color.RESET}")
    
    # KISTools는 싱글톤이므로 한 번만 생성하면 내부 API 호출 시간을 공유합니다.
    main_kis = KISTools()
    sleep(2)
    
    # 환경변수 로드
    BASE_URL = "https://openapi.koreainvestment.com:9443"
    APP_KEY = os.getenv("KIS_APPKEY")
    APP_SECRET = os.getenv("KIS_SECRETKEY")
    USER_ID = os.getenv("KIS_ID")

    # 스캐너 객체 생성
    scanner = KISScanner(BASE_URL, APP_KEY, APP_SECRET, USER_ID)
    
    try:
        while True:
            # 1. 조건검색 종목 코드 리스트 가져오기
            target_codes = scanner.fetch_psearch_stocks()
            
            if target_codes:
                print(f"✨ {len(target_codes)}개 종목 포착. 순차 분석을 시작합니다.")
                for code in target_codes:
                    # 2. 각 종목 분석 함수 호출 (KISTools 인스턴스 전달)
                    process_stock_analysis(code, main_kis)
            
            print(f"\n{Color.CYAN}⏳ 한 사이클 완료. {POLLING_INTERVAL}초 대기...{Color.RESET}")
            sleep(POLLING_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n[운영 중단] 시스템을 종료합니다.")