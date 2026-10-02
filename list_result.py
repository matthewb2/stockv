# -*- coding: utf-8 -*-
import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

# 환경 변수 로드
APP_KEY = os.getenv("KIS_APPKEY")
APP_SECRET = os.getenv("KIS_SECRETKEY")
USER_ID = os.getenv("KIS_ID")

# 실전 도메인
URL_BASE = "https://openapi.koreainvestment.com:9443"

def get_real_token():
    url = f"{URL_BASE}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    res = requests.post(url, json=body)
    return res.json().get("access_token")
    

def psearch_result(token, seq):
    """
    특정 조건식(seq)에 해당하는 종목 검색 결과를 가져옵니다.
    tr_id: HHKST03900400 (실전 투자용 조건검색 종목 조회)
    """
    path = "/uapi/domestic-stock/v1/quotations/psearch-result"
    
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHKST03900400",
        "custtype": "P"
    }
    
    params = {
        "user_id": USER_ID,
        "seq": seq
    }

    res = requests.get(f"{URL_BASE}{path}", headers=headers, params=params)
    return res.json()

def check_real_psearch():
    token = get_real_token()
    if not token:
        print("❌ 토큰 발급 실패")
        return

    path = "/uapi/domestic-stock/v1/quotations/psearch-title"
    
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHKST03900300",
        "custtype": "P"
    }
    params = {"user_id": USER_ID}

    print(f"[*] [실전서버] {USER_ID}님의 조건목록 확인 중...")
    res = requests.get(f"{URL_BASE}{path}", headers=headers, params=params)
    data = res.json()

     # 검색 결과 호출
    result_data = psearch_result(token, 0)
            
    if 'output2' in result_data and result_data['output2']:
        for stock in result_data['output2']:
            print(f"  - 종목코드: {stock['code']} | 종목명: {stock['name']}")
    else:
        print(f"  ⚠️ 검색된 종목이 없거나 오류: {result_data.get('msg1')}")    

if __name__ == "__main__":
    check_real_psearch()