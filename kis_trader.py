import os
import json
import time
from datetime import datetime
import pandas as pd
import numpy as np
from dotenv import load_dotenv

# ==============================
# 🏢 한국투자증권 로컬 모의투자 클래스 (업비트 모의 방식 차용)
# ==============================
class KisPaperTrader:
    def __init__(self, initial_krw=10_000_000, balance_file="stock_balance.json", orders_file="stock_orders.json"):
        self.initial_krw = initial_krw
        self.balance_file = balance_file
        self.orders_file = orders_file
        self.load_data()

    def load_data(self):
        # utf-8-sig: 윈도우 편집기(메모장 등)로 저장된 BOM 파일도 읽을 수 있도록 처리
        if os.path.exists(self.balance_file):
            with open(self.balance_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                self.krw = data.get("krw", self.initial_krw)
                self.balances = data.get("balances", {})
                self.avg_buy_price = data.get("avg_buy_price", {})
        else:
            self.krw = self.initial_krw
            self.balances = {}
            self.avg_buy_price = {}
            self.save_balance()

        if os.path.exists(self.orders_file):
            with open(self.orders_file, "r", encoding="utf-8-sig") as f:
                self.orders = json.load(f)
        else:
            self.orders = []
            self.save_orders()

    def save_balance(self):
        data = {
            "krw": self.krw,
            "balances": self.balances,
            "avg_buy_price": self.avg_buy_price
        }
        with open(self.balance_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def save_orders(self):
        with open(self.orders_file, "w", encoding="utf-8") as f:
            json.dump(self.orders, f, ensure_ascii=False, indent=4)

    def get_balance(self, ticker):
        if ticker == "KRW":
            return self.krw
        return self.balances.get(ticker, 0)

    def get_status(self):
        print(f"\n========== 💼 [한투 모의투자 자산 현황] ==========")
        print(f"💵 예수금 (KRW): {self.krw:,.0f}원")
        print(f"📊 보유 주식:")
        for ticker, qty in self.balances.items():
            if qty > 0:
                avg = self.avg_buy_price.get(ticker, 0)
                print(f"   - 종목코드 {ticker}: {qty}주 (평단가: {avg:,.0f}원)")
        print(f"===============================================\n")

    def buy_market_order(self, ticker, amount, current_price):
        if self.krw < amount:
            print(f"⚠️ 예수금 부족으로 [{ticker}] 매수 불가")
            return None
        
        buy_qty = int(amount // current_price)
        if buy_qty <= 0:
            print(f"⚠️ 매수 금액이 부족하여 주식을 살 수 없습니다. (종목: {ticker})")
            return None

        total_cost = buy_qty * current_price
        self.krw -= total_cost
        
        current_qty = self.balances.get(ticker, 0)
        current_avg = self.avg_buy_price.get(ticker, 0)
        
        if current_qty > 0:
            new_avg = ((current_qty * current_avg) + total_cost) / (current_qty + buy_qty)
        else:
            new_avg = current_price

        self.balances[ticker] = current_qty + buy_qty
        self.avg_buy_price[ticker] = new_avg
        
        order_info = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "BUY",
            "ticker": ticker,
            "price": current_price,
            "qty": buy_qty,
            "total": total_cost
        }
        self.orders.append(order_info)
        self.save_balance()
        self.save_orders()
        return order_info

    def sell_market_order(self, ticker, current_price):
        qty = self.balances.get(ticker, 0)
        if qty <= 0:
            print(f"⚠️ 매도할 보유 주량이 없습니다. (종목: {ticker})")
            return None

        avg_price = self.avg_buy_price.get(ticker, current_price)
        total_revenue = qty * current_price
        pnl_krw = total_revenue - (qty * avg_price)
        pnl_pct = ((current_price - avg_price) / avg_price) * 100

        self.krw += total_revenue
        self.balances[ticker] = 0
        self.avg_buy_price[ticker] = 0

        order_info = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "SELL",
            "ticker": ticker,
            "price": current_price,
            "qty": qty,
            "pnl_krw": pnl_krw,
            "pnl_pct": pnl_pct
        }
        self.orders.append(order_info)
        self.save_balance()
        self.save_orders()
        return order_info

