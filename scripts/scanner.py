import requests
import os
from time import sleep

# 콘솔 색상 (메인과 동일하게 사용하거나 임포트)
class Color:
    MAGENTA = "\033[95m"
    RED = "\033[91m"
    RESET = "\033[0m"

class KISScanner:
    def __init__(self, base_url, app_key, app_secret, user_id):
        self.base_url = base_url
        self.app_key = app_key
        self.app_secret = app_secret
        self.user_id = user_id
        self.token = None

    def get_search_token(self):
        """조건검색 전용 토큰 발급"""
        if self.token:
            return self.token

        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        try:
            res = requests.post(url, json=body)
            self.token = res.json().get("access_token")
            return self.token
        except Exception as e:
            print(f"❌ 검색 토큰 발급 실패: {e}")
            return None

    def fetch_psearch_stocks(self):
        """조건검색 실행 후 종목 코드 리스트 반환"""
        token = self.get_search_token()
        if not token: return []

        path = "/uapi/domestic-stock/v1/quotations/psearch-result"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "HHKST03900400",
            "custtype": "P"
        }
        params = {"user_id": self.user_id, "seq": "0"}

        print(f"\n{Color.MAGENTA}[*] 조건검색 업데이트 중...{Color.RESET}")
        try:
            res = requests.get(f"{self.base_url}{path}", headers=headers, params=params, timeout=10)
            if res.status_code != 200:
                print(f"❌ API 연결 실패: {res.status_code}")
                return []

            data = res.json()
            rt_cd = data.get('rt_cd')
            msg1 = data.get('msg1', '')

            # 결과가 0건일 때의 예외 처리
            if rt_cd != '0' and "종목코드 오류" in msg1:
                print(f"ℹ️ 현재 조건에 일치하는 종목이 없습니다.")
                return []

            stock_list = data.get('output2', [])
            
            # 종목 코드만 추출하여 리스트로 반환
            codes = []
            if isinstance(stock_list, list):
                for s in stock_list:
                    code = s.get('code') or s.get('s_code')
                    if code:
                        codes.append(code.strip().replace('A', ''))
            return codes

        except Exception as e:
            print(f"❌ 검색 API 로직 에러: {e}")
            return []