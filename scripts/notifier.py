import requests
import os

class DiscordNotifier:
    def __init__(self):
        self.url = os.getenv('DISCORD_WEBHOOK_URL')

    def send(self, title, message):
        payload = {"embeds": [{"title": title, "description": message, "color": 3447003}]}
        requests.post(self.url, json=payload)