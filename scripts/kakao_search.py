# -*- coding: utf-8 -*-
"""
카카오(다음) 검색 API 래퍼.

https://developers.kakao.com/docs/ko/daum-search/dev-guide 기준.
- 엔드포인트: https://dapi.kakao.com/v2/search/web
- 인증: Authorization: KakaoAK {REST API 키}   (헤더 X-Daum-Appkey 가 아님)
- 응답: {"meta": {...}, "documents": [{title, contents, url, datetime}]}

주의: 카카오 검색 API 에는 뉴스 전용 엔드포인트가 없다.
      web/image/blog/cafe/vclip/book 만 제공되므로, 종목 호재 확인에는
      sort=recency(최신순) 웹문서 검색을 사용한다.

응답의 title/contents 에는 <b> 강조 태그가 포함되므로 제거해 LLM 프롬프트에 쓴다.
키가 없거나 호출이 실패해도 프로그램이 죽지 않도록 빈 목록을 돌려준다.
(뉴스를 못 얻으면 Groq 판단이 HOLD 로 수렴해야 하므로 조용히 넘어간다)
"""
import html
import os
import re

import requests

from scripts.console_color import Color

DEFAULT_SIZE = 10
DEFAULT_TIMEOUT = 5

# 웹문서 검색은 size 1~50 지원
MAX_SIZE = 50

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text):
    """검색 결과의 태그·엔티티·여분 공백을 정리한다

    카카오 응답은 "&#8203;" "<b>" "&nbsp;" 등이 섞여 있으므로
    태그만 벗기면 프롬프트에 그대로 넘어가므로 함께 디코딩한다.
    """
    if not text:
        return ""
    plain = html.unescape(_TAG_RE.sub("", str(text)))
    return _WS_RE.sub(" ", plain).strip()


def _format_datetime(value):
    """'2026-10-03T09:12:00.000+09:00' -> '2026-10-03 09:12'"""
    if not value:
        return ""
    text = str(value).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", text)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return text[:16]


class KakaoSearch:
    """카카오(다음) 검색 API 클라이언트"""

    BASE_URL = "https://dapi.kakao.com/v2/search/web"

    def __init__(self, api_key=None, timeout=DEFAULT_TIMEOUT, session=None):
        self.api_key = api_key or os.getenv("KAKAO_API")
        self.timeout = timeout
        self.session = session or requests

    @property
    def enabled(self):
        return bool(self.api_key)

    def search_news(self, query, size=DEFAULT_SIZE, sort="recency", page=1):
        """검색어에 해당하는 최신 웹문서 목록 반환

        실패 시 빈 리스트 (호출부가 예외 처리를 덜 고민해도 된다).
        """
        if not self.api_key:
            print(f"{Color.YELLOW}⚠️ KAKAO_API 키가 없어 뉴스 검색을 건너뜁니다."
                  f"{Color.RESET}")
            return []

        size = max(1, min(int(size), MAX_SIZE))
        params = {"query": query, "sort": sort, "page": page, "size": size}
        headers = {"Authorization": f"KakaoAK {self.api_key}"}

        try:
            res = self.session.get(self.BASE_URL, headers=headers,
                                   params=params, timeout=self.timeout)
        except Exception as e:
            print(f"{Color.YELLOW}⚠️ 카카오 검색 요청 실패 ({query}): {e}"
                  f"{Color.RESET}")
            return []

        if res.status_code != 200:
            print(f"{Color.YELLOW}⚠️ 카카오 검색 실패 ({query}): "
                  f"{res.status_code} {res.text[:120]}{Color.RESET}")
            return []

        try:
            items = (res.json() or {}).get("documents") or []
        except Exception as e:
            print(f"{Color.YELLOW}⚠️ 카카오 검색 응답 파싱 실패 ({query}): {e}"
                  f"{Color.RESET}")
            return []

        news = []
        for item in items:
            title = strip_html(item.get("title"))
            if not title:
                continue
            news.append({
                "title": title,
                "summary": strip_html(item.get("contents"))[:300],
                "press": "웹",
                "date": _format_datetime(item.get("datetime")),
                "link": item.get("url", ""),
            })

        return news

    def search_stock(self, name, code=None, size=DEFAULT_SIZE):
        """종목 기준 뉴스 검색

        종목명이 있으면 "종목명"을, 없으면 코드로 검색한다.
        """
        query = f"{name} 주가" if name else str(code)
        return self.search_news(query, size=size)