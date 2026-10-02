# -*- coding: utf-8 -*-
"""
매수/관망 판단기 추상화.

- GroqDecider : Groq LLM 기반 판단 (GROQ_API_KEY 필요)
- RuleDecider : RSI/거래량 기반 로컬 규칙 판단 (오프라인 테스트용)

두 구현 모두 `decide(code, snapshot)`가
{"action": "BUY"|"HOLD", "reason": str} 형태의 dict를 반환한다.
"""
import os
import re
import json

DEFAULT_ACTION = "HOLD"
BUY_ACTION = "BUY"
JSON_PARSE_ERROR = {"action": DEFAULT_ACTION, "reason": "JSON 파싱 에러"}


def extract_json(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        raise ValueError("JSON 추출 실패")
    return match.group(0)


def safe_json_loads(text):
    try:
        return json.loads(extract_json(text))
    except Exception:
        return dict(JSON_PARSE_ERROR)


def normalize_decision(decision):
    """판단 결과를 통일된 형태(대문자 action + reason)로 변환"""
    if not isinstance(decision, dict):
        return dict(JSON_PARSE_ERROR)

    action = str(decision.get("action", DEFAULT_ACTION)).upper()
    if action != BUY_ACTION:
        action = DEFAULT_ACTION

    return {"action": action, "reason": str(decision.get("reason", ""))}


class Decider:
    """매수 판단기 인터페이스"""

    def decide(self, code, snapshot):
        """
        Args:
            code: 종목 코드
            snapshot: {"price", "rsi", "volumes", "avg_volume", "vol_change"}
        """
        raise NotImplementedError


# ==============================
# 🤖 Groq LLM 판단기
# ==============================
class GroqDecider(Decider):
    """Groq LLM 판단기.

    `groq` 패키지를 실제로 사용할 때만 임포트하므로,
    미설치 환경에서도 로컬 모의매매 테스트가 가능하다.
    """

    SYSTEM_PROMPT = (
        "You are an expert stock trader. Analyze technical indicators and respond ONLY in JSON. "
        "Structure: {\"action\": \"BUY\" or \"HOLD\", \"reason\": \"...\"}"
    )

    def __init__(self, model=None):
        try:
            from groq import Groq  # 선택적 의존성: 실제 LLM 호출 시에만 필요
        except ImportError as e:
            raise RuntimeError(f"groq 패키지가 필요합니다. (pip install groq) - {e}")

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY 환경변수가 설정되지 않았습니다.")

        self.client = Groq(api_key=api_key)
        self.model = model or os.getenv("LLM_MODEL") or "llama-3.3-70b-versatile"

    def decide(self, code, snapshot):
        user_input = (f"Stock: {code}, Price: {snapshot['price']}\n"
                      f"RSI: {snapshot['rsi']:.2f}, "
                      f"Volumes: {snapshot.get('volumes', [])}, "
                      f"Avg: {snapshot.get('avg_volume', 0)}")

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_input},
            ],
            temperature=0
        )

        return normalize_decision(safe_json_loads(response.choices[0].message.content))


# ==============================
# 📐 로컬 규칙 판단기 (오프라인 테스트용)
# ==============================
class RuleDecider(Decider):
    """RSI/거래량 기반 규칙 판단기.

    외부 API 의존성이 없어 매매 로직 자체를 로컬에서 검증할 수 있다.
    """

    def __init__(self, buy_rsi=35.0, strong_vol_change=0.5):
        self.buy_rsi = buy_rsi
        self.strong_vol_change = strong_vol_change

    def decide(self, code, snapshot):
        rsi = snapshot["rsi"]
        vol_change = snapshot["vol_change"]

        if rsi <= self.buy_rsi:
            return {"action": BUY_ACTION, "reason": f"RSI {rsi:.2f} <= {self.buy_rsi} (과매도 진입)"}

        if vol_change >= self.strong_vol_change and rsi < 50:
            return {"action": BUY_ACTION, "reason": f"거래량 급증({vol_change:+.0%}) + 저RSI({rsi:.2f})"}

        return {"action": DEFAULT_ACTION, "reason": f"RSI {rsi:.2f} 중립, 거래량 변화 {vol_change:+.0%}"}