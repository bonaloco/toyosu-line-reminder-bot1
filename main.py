from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from apscheduler.schedulers.background import BackgroundScheduler
import os
import datetime

app = Flask(__name__)

# 環境変数からトークン類を読み込み
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 簡易的に予定表保存（本番ではDBに置き換え可）
weekly_schedule = {}

def parse_schedule(text):
    sections = {
        '救急': [],
        'AM院内': [],
        'PM院内': [],
        '残り番': []
    }
    current_section = None
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line in sections:
            current_section = line
            continue
        if current_section:
            sections[current_section].append(line)
    return sections

def get_today_assignment(sections):
    today = datetime.datetime.now()
    weekday = today.weekday()
    assignment = {}
    for key in ['救急', 'AM院内', 'PM院内']:
        assignment[key] = sections.get(key, ["未設定"])[weekday] if weekday < len(sections.get(key, [])) else "未設定"
    idx = weekday * 2
    if idx + 1 < len(sections.get('残り番', [])):
        assignment['残り番'] = (sections['残り番'][idx], sections['残り番'][idx+1])
    else:
        assignment['残り番'] = ("未設定", "未設定")
    return assignment

def create_reminder(assignments):
    if not assignments:
        return "本日はリマインド対象日ではありません。"
    message = "【本日の担当者】\n\n"
    message += f"救急(リハ診)：{assignments['救急']}\n"
    message += f"AM院内：{assignments['AM院内']}\n"
    message += f"PM院内：{assignments['PM院内']}\n"
    first, second = assignments['残り番']
    message += f"残り番：1st {first} ／ 2nd {second}\n\n"
    message += "よろしくお願いします！"
    return message

def daily_reminder():
    if not weekly_schedule:
        print("予定表未登録")
        return
    msg = create_reminder(get_today_assignment(weekly_schedule))
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=msg))

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    global weekly_schedule
    text = event.message.text
    if "救急" in text and "AM院内" in text and "PM院内" in text and "残り番" in text:
        weekly_schedule = parse_schedule(text)
        reply = "週間予定表を登録しました！"
    else:
        reply = "週間予定表ではないメッセージを受信しました。"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# 朝7:30に自動実行（日本時間）
scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(daily_reminder, 'cron', hour=7, minute=30)
scheduler.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
