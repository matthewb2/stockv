# -*- coding: utf-8 -*-
import sys
import os
import re
import json
from time import sleep  # time 모듈 전체가 아닌 sleep 함수만 직접 임포트
from groq import Groq
from dotenv import load_dotenv
from scripts.kis_tools import KISTools
from scripts.strategy import calculate_rsi
from scripts.notifier import DiscordNotifier

load_dotenv()

# ==============================
# 🔧 설정 및 유틸리티
# ==============================
TARGET_PROFIT = 3.0   # 익절 목표 (+3%)
STOP_LOSS = -2.0     # 손절선 (-2%)
CHECK_INTERVAL = 60   # 감시 주기 (60초)
API_DELAY = 0.5      # 유량 제한 방지 지연 (초당 3건 제한)

def extract_json(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match: raise ValueError("JSON 추출 실패")
    return match.group(0)

def safe_json_loads(text):
    try: return json.loads(extract_json(text))
    except: return {"action": "HOLD", "reason": "JSON 파싱 에러"}

# ==============================
# 📡 실시간 모니터링 & 자동 매도
# ==============================
def monitor_position(kis, stock_code, notifier):
    """보유 종목의 수익률을 감시하고 매도 조건 도달 시 처리"""
    print(f"📡 [{stock_code}] 실시간 감시 모드 진입...")
    
    while True:
        try:
            # 1. 잔고 조회 (API 호출 1)
            balance = kis.account.balance()
            sleep(API_DELAY) 
            
            holding = next((item for item in balance if item.symbol == stock_code), None)
            
            if not holding:
                print(f"ℹ️ [{stock_code}] 잔고가 없습니다. 모니터링을 종료합니다.")
                break

            # 2. 매입가 및 수량 추출 (getattr로 안전하게 호출)
            buy_price = float(getattr(holding, 'puch_avg_pric', 0) or getattr(holding, 'price', 0))
            qty = int(getattr(holding, 'qty', 0) or getattr(holding, 'hold_qty', 0))

            if buy_price <= 0:
                print(f"⚠️ 매입단가를 불러올 수 없습니다. 데이터 확인: {vars(holding)}")
                sleep(5)
                continue

            # 3. 현재가 조회 (API 호출 2)
            market_data = kis.get_market_data(stock_code)
            curr_price = float(market_data['price'])
            sleep(API_DELAY)

            # 4. 수익률 직접 계산
            # 세금 0.3% 포함
            profit_rate = ((curr_price - buy_price) / buy_price) * 100 - 0.3
            
            print(f"📊 [{stock_code}] 수익률: {profit_rate:.2f}% | 현재가: {curr_price:,.0f} | 매입가: {buy_price:,.0f} | 수량: {qty}")

            # 5. 매도 조건 판단
            if profit_rate >= TARGET_PROFIT or profit_rate <= STOP_LOSS:
                reason = "익절" if profit_rate >= TARGET_PROFIT else "손절"
                print(f"🚀 {reason} 조건 도달! 매도를 실행합니다.")
                
                # 주문 실행 (API 호출 3)
                order_res = kis.order(stock_code, qty=qty, side="sell")
                sleep(API_DELAY)
                
                msg = (f"🔔 **자동 매도 체결 ({reason})**\n"
                       f"종목: {stock_code}\n"
                       f"수익률: {profit_rate:.2f}%\n"
                       f"매도가: {curr_price:,.0f}\n"
                       f"수량: {qty}주")
                
                notifier.send("SELL SIGNAL", msg)
                print(f"✅ 매도 완료: {msg}")
                break

        except Exception as e:
            print(f"⚠️ 감시 중 에러 발생: {e}")
            sleep(5)

        sleep(CHECK_INTERVAL)

# ==============================
# 🤖 에이전트 실행 로직 (매수 결정)
# ==============================
def run_agent(stock_code):
    print(f"🚀 Groq 에이전트 가동: {stock_code}")
    
    kis = KISTools()
    notifier = DiscordNotifier()
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    try:
        # 1. 데이터 수집
        data = kis.get_market_data(stock_code, "D")
        sleep(API_DELAY)
        
        rsi = calculate_rsi(data['closes'])
        vol_change = (data['volumes'][-1] / data['volumes'][-2] - 1) if len(data['volumes']) > 1 else 0

        # 2. Groq 판단
        system_prompt = "You are a trading agent. Respond ONLY in JSON. Example: {\"action\": \"BUY\", \"reason\": \"...\"}"
        user_input = f"Stock: {stock_code}, Price: {data['price']}, RSI: {rsi:.2f}, Vol: {vol_change:.2%}"

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_input}],
            temperature=0
        )

        decision = safe_json_loads(response.choices[0].message.content)
        action = decision.get("action", "HOLD").upper()
        reason = decision.get("reason", "")

        print(f"💡 Groq 판단: [{action}] - {reason}")

        # 3. 액션 수행
        if action == "BUY":
            # 시장가 1주 매수 주문
            order_res = kis.order(stock_code, qty=1, side="buy")
            sleep(API_DELAY)
            
            notifier.send("BUY SIGNAL", f"✅ 매수 완료 ({stock_code})\n이유: {reason}")
            # 매수 직후 감시 모드로 전환
            monitor_position(kis, stock_code, notifier)
        else:
            print(f"⏳ 관망 중... (기존 잔고 감시 시작)")
            monitor_position(kis, stock_code, notifier)

    except Exception as e:
        print(f"❌ 에러 발생: {e}")

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "005930"
    run_agent(target)