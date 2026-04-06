# -*- coding: utf-8 -*-
import requests
import json
import os
import pandas as pd
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

def psearch_result_all(access_token, user_id, seq):
    api_url = "/uapi/domestic-stock/v1/quotations/psearch-result"
    url = f"{BASE_URL}{api_url}"
    
    all_stocks = []
    tr_cont = ""  # 처음엔 빈 값으로 시작
    
    while True:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {access_token}",
            "appkey": os.getenv('KIS_VIRTUAL_APPKEY'),
            "appsecret": os.getenv('KIS_VIRTUAL_SECRETKEY'),
            "tr_id": "HHKST03900400",
            "custtype": "P",
            "tr_cont": tr_cont,  # ⚠️ 핵심: 연속 조회를 위한 파라미터
            "Accept": "text/plain"
        }

        params = {
            "user_id": user_id,
            "seq": seq
        }

        res = requests.get(url, headers=headers, params=params)
        
        if res.status_code == 200:
            print("response success")
            data = res.json()
            # 종목 리스트는 'output2'에 들어있음
            stocks = data.get('output2', [])
            if stocks:
                all_stocks.extend(stocks)
            
            # ⚠️ 공식 가이드: rt_cd가 '1'이면 다음 데이터가 더 있음
            # 응답 헤더의 'tr_cont' 값을 다음 요청에 사용
            tr_cont = res.headers.get('tr_cont', '')
            
            # 더 이상 조회할 데이터가 없거나 응답 코드가 '0'이면 종료
            if data.get('rt_cd') == '0' or not tr_cont:
                break
            
            print(f"[*] 연속 데이터 수신 중... (현재 {len(all_stocks)}종목)")
        else:
            print(f"!!! 에러: {res.status_code} | {res.text}")
            break
            
    return all_stocks

if __name__ == "__main__":
    token = get_access_token()
    my_id = os.getenv('KIS_VIRTUAL_ID').strip().upper()
    
    if token:
        print(f"[*] {my_id}님의 001번 조건식 종목 추출 시작...")
        stocks = psearch_result_all(token, my_id, "5")
        
        if stocks:
            print("\n" + "="*50)
            print(f" [성공] 검색된 종목 리스트 (총 {len(stocks)}개)")
            print("-" * 50)
            # 깔끔하게 보기 위해 DataFrame 변환 (Pandas 설치 필요)
            df = pd.DataFrame(stocks)
            # 필요한 열만 출력 (code: 종목코드, name: 종목명, price: 현재가)
            print(df[['code', 'name', 'price']])
            print("="*50)
        else:
            print("\n[!] 검색된 종목이 없습니다. HTS 조건식 설정을 확인하세요.")