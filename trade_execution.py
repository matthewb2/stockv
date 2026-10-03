# -*- coding: utf-8 -*-
"""
매매 실행 로직.

개별종목 국면 판정 결과를 실제 주문으로 연결하는 부분만 떼어낸 모듈이다.
매도 트리거는 전략과 무관하고, 매수는 시장 국면(액티브/보수)을 따른다.

trade_core 의 TradingContext 를 직접 import 하지 않는다.
ctx 를 인자로만 받으므로 순환 import 없이 단독 실행·단독 테스트가 가능하다.
(필요한 속성: ctx.paper, ctx.notifier, ctx.amount_rate, ctx.stock_name())
"""
from market_regime import REGIME_DOWNTREND, REGIME_UPTREND
from scripts.console_color import Color

MIN_BUY_AMOUNT = 10_000      # 최소 주문 금액


# ==============================
# 💱 매매 실행
# ==============================
def execute_trade_logic(ticker, curr_price, regime, ctx, allow_buy=True):
    """개별종목 국면 판정에 따라 매수/매도 실행

    Downtrend → 매도, Uptrend → 매수, Sideways/None → 관망

    allow_buy 가 False 이면 시장 국면이 상승장이 아니므로 매수하지 않고
    보유분을 유지한다 (매도 트리거는 allow_buy 과 무관하게 동작한다).
    """
    try:
        balance = ctx.paper.get_balance(ticker)
        avg_price = ctx.paper.avg_buy_price.get(ticker, curr_price)
        if avg_price is None or avg_price <= 0:
            avg_price = curr_price
        pnl_pct = ((curr_price - avg_price) / avg_price) * 100
        name = ctx.stock_name(ticker)

        # 1. 하락 국면 → 보유분 매도
        if regime == REGIME_DOWNTREND:
            if balance > 0:
                print(f"📉 [{name}({ticker})] 하락 국면 감지! 매도 진행 "
                      f"(수익률 {pnl_pct:+.2f}%)")
                order_res = ctx.paper.sell_market_order(ticker, curr_price)
                if order_res:
                    msg = (f"🔴 **[보유 전략 하락 매도]**\n"
                           f"종목: {name}({ticker})\n"
                           f"수익률: {order_res['pnl_pct']:+.2f}% ({order_res['pnl_krw']:,.0f}원)")
                    ctx.notifier.send("PAPER SELL", msg)
            return

        # 2. 횡보/판단 불가 → 매매 없음
        if regime != REGIME_UPTREND:
            print(f"⏳ [{name}({ticker})] 횡보 또는 판단 보류 상태로 관망합니다.")
            return

        # 3. 시장 국면이 상승이 아니면 신규 매수 보류
        if not allow_buy:
            print(f"⏸️ [{name}({ticker})] 종목은 상승 국면이나 시장 국면이 상승이 "
                  f"아니라 신규 매수를 보류합니다.")
            return

        # 4. 상승 국면 → 매수 검토
        if balance > 0:
            return

        krw_balance = ctx.paper.get_balance("KRW")
        buy_amount = krw_balance * ctx.amount_rate

        if buy_amount <= MIN_BUY_AMOUNT:
            print(f"⚠️ 현금 잔고 부족으로 {name}({ticker}) 매수 불가 (목표액 {buy_amount:,.0f}원)")
            return

        order_res = ctx.paper.buy_market_order(ticker, buy_amount, curr_price)
        if order_res:
            print(f"✅ [{name}({ticker})] 모의 매수 {order_res['qty']}주 @ {curr_price:,.0f}원")
            msg = (f"✅ **[보유 전략 상승 매수]**\n"
                   f"종목: {name}({ticker})\n"
                   f"매수금액: {order_res['total']:,.0f}원")
            ctx.notifier.send("PAPER BUY", msg)
    except Exception as e:
        print(f"{Color.RED}❌ 매매 실행 에러 ({ticker}): {e}{Color.RESET}")