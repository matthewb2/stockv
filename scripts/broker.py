# -*- coding: utf-8 -*-
"""
거래 계층(계좌/주문) 추상화.

- PaperBroker : KisPaperTrader 기반 로컬 모의매매

시세/조건검색은 실전 계좌(KISNative/KISScanner)로 조회하되,
주문은 한투 모의계좌가 아니라 로컬 JSON 포트폴리오에 기록된다.
매매 로직은 Broker 인터페이스에만 의존한다.
"""
from kis_trader import KisPaperTrader


class Position:
    """보유 종목 스냅샷"""

    def __init__(self, symbol, qty, buy_price):
        self.symbol = symbol
        self.qty = int(qty)
        self.buy_price = float(buy_price)

    def __repr__(self):
        return f"Position(symbol={self.symbol}, qty={self.qty}, buy_price={self.buy_price:,.0f})"


class Broker:
    """계좌 조회 / 주문 실행 인터페이스"""

    def get_position(self, code):
        """보유 중이면 Position, 미보유 시 None 반환"""
        raise NotImplementedError

    def get_cash(self):
        """주문 가능 금액(예수금) 조회"""
        raise NotImplementedError

    def buy_cash_ratio(self, code, price, ratio):
        """예수금의 ratio 비율만큼 시장가 매수"""
        raise NotImplementedError

    def sell(self, code, qty, price):
        """보유 수량 시장가 매도"""
        raise NotImplementedError

    def get_holding_codes(self):
        """보유 종목 코드 목록"""
        raise NotImplementedError

    def status(self):
        """자산 현황 출력"""
        raise NotImplementedError


# ==============================
# 🧪 로컬 모의투자 (KisPaperTrader 래퍼)
# ==============================
class PaperBroker(Broker):
    """KisPaperTrader를 이용한 로컬 모의매매.

    한투 모의계좌에 접속하지 않고 JSON 파일로 잔고/체결 내역만 관리하므로,
    실전 시세로 판단하면서도 주문은 안전하게 로컬에서 확인할 수 있다.
    """

    def __init__(self, initial_krw=10_000_000, balance_file="stock_balance.json", orders_file="stock_orders.json"):
        self.trader = KisPaperTrader(
            initial_krw=initial_krw,
            balance_file=balance_file,
            orders_file=orders_file
        )

    def get_position(self, code):
        qty = int(self.trader.get_balance(code))
        if qty <= 0:
            return None
        return Position(code, qty, self.trader.avg_buy_price.get(code, 0))

    def get_holding_codes(self):
        return [code for code, qty in self.trader.balances.items() if qty > 0]

    def get_cash(self):
        return float(self.trader.get_balance("KRW"))

    def buy_cash_ratio(self, code, price, ratio=0.10):
        amount = self.get_cash() * ratio
        if amount <= 0:
            print(f"⚠️ 매수 가능 금액이 없습니다. (종목: {code})")
            return None

        print(f"🧪 [{code}] 예수금의 {ratio:.0%}인 {amount:,.0f}원 규모로 모의 매수를 진행합니다.")
        return self.trader.buy_market_order(code, amount, price)

    def sell(self, code, qty, price):
        # 로컬 모의매매는 보유 전량 매도만 지원
        return self.trader.sell_market_order(code, price)

    def status(self):
        self.trader.get_status()
