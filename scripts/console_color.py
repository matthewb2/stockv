# -*- coding: utf-8 -*-
"""
콘솔 출력 색상 정의.

색상 코드는 여러 모듈에서 쓰이므로 한 곳에 모아둔다.
"""
RESET = "\033[0m"


class Color:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    BOLD = "\033[1m"
    BG_GREEN = "\033[42m\033[30m"
    RESET = RESET