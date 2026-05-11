import requests
import time


class KISNative:
    def __init__(self, app_key, app_secret, account_prefix):
        self.app_key = app_key
        self.app_secret = app_secret
        self.account_prefix = account_prefix
        self.account_suffix = "01"

        self.base_url = "https://openapivts.koreainvestment.com:29443"
        
        self.access_token = None
        self.token_expire = 0

    def get_balance(self):
        """
        계좌 잔고 조회

        return:
        {
            "cash": int,          # 예수금
            "orderable": int,     # 주문가능금액
            "stocks": list[dict], # 보유종목
        }
        """

        token = self._get_token()

        url = (
            f"{self.base_url}"
            "/uapi/domestic-stock/v1/trading/inquire-balance"
        )

        headers = {
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,

            # 모의투자
            "tr_id": "VTTC8434R",
        }

        params = {
            "CANO": self.account_prefix,
            "ACNT_PRDT_CD": self.account_suffix,

            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",

            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        res = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=5,
        )

        res.raise_for_status()

        data = res.json()

        if data["rt_cd"] != "0":
            raise RuntimeError(data["msg1"])

        holdings = []

        for item in data["output1"]:
            qty = int(item["hldg_qty"])

            if qty == 0:
                continue

            holdings.append(
                {
                    "code": item["pdno"],
                    "name": item["prdt_name"],
                    "qty": qty,
                    "buy_price": int(float(item["pchs_avg_pric"])),
                    "current_price": int(item["prpr"]),
                    "eval_amount": int(item["evlu_amt"]),
                    "profit_amount": int(item["evlu_pfls_amt"]),
                    "profit_rate": float(item["evlu_pfls_rt"]),
                }
            )

        summary = data["output2"][0]

        return {
            "cash": int(summary["dnca_tot_amt"]),
            "orderable": int(summary["nxdy_excc_amt"]),
            "stocks": holdings,
        }
    def _get_token(self):
        now = time.time()

        if self.access_token and now < self.token_expire:
            return self.access_token

        url = f"{self.base_url}/oauth2/tokenP"

        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }

        res = requests.post(url, json=body)
        res.raise_for_status()

        data = res.json()

        self.access_token = data["access_token"]

        # 대충 23시간 캐시
        self.token_expire = now + 82800

        return self.access_token

    def get_name(self, code):
        """
        종목코드로 종목명 조회

        code: 종목코드 (예: 005930)

        return:
            str
        """

        token = self._get_token()

        url = (
            f"{self.base_url}"
            "/uapi/domestic-stock/v1/quotations/search-info"
        )

        headers = {
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "CTPF1604R",
        }

        params = {
            "PDNO": code,
            "PRDT_TYPE_CD": "300",
        }

        res = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=5,
        )

        res.raise_for_status()

        data = res.json()

        if data["rt_cd"] != "0":
            raise RuntimeError(data["msg1"])

        return data["output"]["prdt_name"]
    
    def get_price(self, code):
        """
        현재가 조회

        code: 종목코드 (예: 005930)

        return:
            {
                "code": str,
                "name": str,
                "price": int,
                "open": int,
                "high": int,
                "low": int,
                "volume": int,
            }
        """

        token = self._get_token()

        url = (
            f"{self.base_url}"
            "/uapi/domestic-stock/v1/quotations/inquire-price"
        )

        headers = {
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,

            # 모의/실전 동일
            "tr_id": "FHKST01010100",
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        }

        res = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=5,
        )

        res.raise_for_status()

        data = res.json()["output"]

        return {
            "code": code,
            "price": int(data["stck_prpr"]),
            "open": int(data["stck_oprc"]),
            "high": int(data["stck_hgpr"]),
            "low": int(data["stck_lwpr"]),
            "volume": int(data["acml_vol"]),
        }
    
    def get_3m_chart(self, code):
        """
        code: 종목코드 (예: 005930)

        return:
            list[dict]
        """

        token = self._get_token()

        url = (
            f"{self.base_url}"
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        )

        headers = {
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,

            # 실전계좌 TR
            "tr_id": "FHKST03010200",
        }

        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,

            # 현재시각부터 역순
            "FID_INPUT_HOUR_1": "",

            # 3분봉
            "FID_PERIOD_DIV_CODE": "3",

            "FID_PW_DATA_INCU_YN": "Y",
        }

        res = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=5,
        )

        res.raise_for_status()

        data = res.json()

        rows = []

        for item in data["output2"]:
            rows.append(
                {
                    "time": item["stck_cntg_hour"],
                    "open": int(item["stck_oprc"]),
                    "high": int(item["stck_hgpr"]),
                    "low": int(item["stck_lwpr"]),
                    "close": int(item["stck_prpr"]),
                    "volume": int(item["cntg_vol"]),
                }
            )

        return rows