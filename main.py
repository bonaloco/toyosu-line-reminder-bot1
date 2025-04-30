import os
import sys
import json
import datetime
from flask import Flask, request, abort
from apscheduler.schedulers.background import BackgroundScheduler
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 環境変数
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# スケジュール保存用
weekly_schedule = {}

def parse_schedule(text):
    sections = {'救急': [], 'AM院内': [], 'PM院内': [], '残り番': []}
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
        assignment['残り番'] = (sections['残り番'][idx], sections['残り番'][idx + 1])
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

def create_weekly_summary(sections):
    if not sections:
        return "まだ週間予定表が登録されていません。"
    days = ["月", "火", "水", "木", "金", "土", "日"]
    message = "【今週の予定】\n\n"
    for i, day in enumerate(days):
        message += f"{day}曜:\n"
        message += f" 救急(リハ診)：{sections['救急'][i] if i < len(sections['救急']) else '未設定'}\n"
        message += f" AM院内：{sections['AM院内'][i] if i < len(sections['AM院内']) else '未設定'}\n"
        message += f" PM院内：{sections['PM院内'][i] if i < len(sections['PM院内']) else '未設定'}\n"
        if i*2+1 < len(sections['残り番']):
            first = sections['残り番'][i*2] if i*2 < len(sections['残り番']) else "未設定"
            second = sections['残り番'][i*2+1] if i*2+1 < len(sections['残り番']) else "未設定"
            message += f" 残り番：1st {first} ／ 2nd {second}\n"
        else:
            message += f" 残り番：未設定\n"
        message += "\n"
    return message

def daily_reminder():
    if not weekly_schedule:
        sys.stderr.write("予定表未登録\n")
        return
    msg = create_reminder(get_today_assignment(weekly_schedule))
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=msg))

def weekly_request_reminder():
    message = "【お知らせ】\n来週分の週間予定表を入力してください！"
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=message))

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

    sys.stderr.write("===================\n")
    sys.stderr.write(f"source.type = {event.source.type}\n")
    if event.source.type == "group":
        sys.stderr.write(f"✅ Group ID = {event.source.group_id}\n")
    else:
        sys.stderr.write("これはグループではありません\n")
    sys.stderr.write("===================\n")

    # 週間予定表登録
    if '救急' in text and 'AM院内' in text and 'PM院内' in text and '残り番' in text:
        weekly_schedule = parse_schedule(text)
        reply = "週間予定表を登録しました！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    
    # 「今週の予定を確認」と言われたら返信
    elif '今週の予定を確認' in text:
        summary = create_weekly_summary(weekly_schedule)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=summary))
    
    # それ以外は無視
    else:
        pass

# スケジューラ起動
scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(daily_reminder, 'cron', hour=7, minute=30)
scheduler.add_job(weekly_request_reminder, 'cron', day_of_week='sun', hour=19, minute=0)
scheduler.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# Wakeup endpoint for external cron ping
@app.route("/", methods=["GET"])
def wakeup():
    return "I'm awake!", 200
