import requests
import os

class DiscordNotifier:
    def __init__(self):
        self.url = os.getenv('DISCORD_WEBHOOK_URL')

    def send(self, title, message):
        payload = {"embeds": [{"title": title, "description": message, "color": 3447003}]}
        requests.post(self.url, json=payload)


class ConsoleNotifier:
    """콘솔에만 출력하는 알림기 (로컬 테스트용, 외부 요청 없음)"""

    def send(self, title, message):
        print(f"\n📨 [{title}]\n{message}\n")


def build_notifier():
    """웹훅 설정 여부에 따라 알림기를 선택"""
    if os.getenv('DISCORD_WEBHOOK_URL'):
        return DiscordNotifier()

    print("ℹ️ DISCORD_WEBHOOK_URL이 없어 알림을 콘솔로 출력합니다.")
    return ConsoleNotifier()