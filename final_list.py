# -*- coding: utf-8 -*-
import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

IS_VTS = True 
BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_VTS else "https://openapi.koreainvestment.com:9443"

def get_access_token():
    url = f"{BASE_URL}/oauth2/tokenP"
    headers = {"Content-Type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": os.getenv('KIS_VIRTUAL_APPKEY'),
        "appsecret": os.getenv('KIS_VIRTUAL_SECRETKEY')
    }
    res = requests.post(url, headers=headers, data=json.dumps(body))
    return res.json().get("access_token")

def fetch_real_data(token, user_id, seq):
    api_url = "/uapi/domestic-stock/v1/quotations/psearch-result"
    url = f"{BASE_URL}{api_url}"
    
    # ⚠️ 핵심: 첫 번째 호출에서 데이터가 없으면 'D'를 넣어서 다시 호출합니다.
    # 한국투자증권 API의 '연속 조회' 규격을 강제 적용합니다.
    for tr_cont in ["", "D"]: 
        headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {token}",
            "appkey": os.getenv('KIS_VIRTUAL_APPKEY'),
            "appsecret": os.getenv('KIS_VIRTUAL_SECRETKEY'),
            "tr_id": "HHKST03900400",
            "custtype": "P",
            "tr_cont": tr_cont
        }
        params = {"user_id": user_id, "seq": str(seq)}
        
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        
        stocks = data.get('output2', [])
        if stocks:
            return stocks  # 종목을 찾으면 즉시 반환
            
    return []

if __name__ == "__main__":
    token = get_access_token()
    user_id = os.getenv('KIS_VIRTUAL_ID').strip().upper()
    
    print(f"[*] {user_id}님의 조건식 데이터 강제 추출 시작...")
    
    for i in range(5): # HTS에 2개 있다고 하셨으니 0, 1번 위주로 검사
        stocks = fetch_real_data(token, user_id, i)
        
        if stocks:
            print("\n" + "="*50)
            print(f" 🎉 [성공] {i}번 조건식에서 {len(stocks)}개 종목 발견!")
            print("-" * 50)
            for s in stocks:
                print(f" 코드: {s['code']} | 종목명: {s['name']} | 현재가: {s['price']}")
            print("="*50)
            break
        else:
            print(f" [!] {i}번 조건식: 응답은 정상이나 종목 데이터가 비어있음.")

    print("\n[*] 검사 완료.")