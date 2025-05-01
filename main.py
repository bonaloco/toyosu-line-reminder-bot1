import os
import json
import sqlite3
from flask import Flask, request, abort
from apscheduler.schedulers.background import BackgroundScheduler
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GROUP_ID = os.getenv("LINE_GROUP_ID")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

DB_PATH = "schedule.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS schedule (
                        day TEXT PRIMARY KEY,
                        data TEXT NOT NULL,
                        updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )''')
        conn.commit()

def save_schedule_to_db(schedule):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        for day, data in schedule.items():
            c.execute("REPLACE INTO schedule (day, data) VALUES (?, ?)", (day, json.dumps(data)))
        conn.commit()

def load_schedule_from_db():
    schedule = {}
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        for row in c.execute("SELECT day, data FROM schedule"):
            schedule[row[0]] = json.loads(row[1])
    return schedule

def clear_schedule():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM schedule")
        conn.commit()

def parse_schedule(text):
    # 簡易パーサー（必要に応じて改善）
    days = ["月", "火", "水", "木", "金", "土", "日"]
    schedule = {}
    lines = text.splitlines()
    current_day = 0
    for line in lines:
        if line.strip() == "":
            current_day += 1
            continue
        day = days[current_day % 7]
        schedule.setdefault(day, []).append(line)
    return schedule

def create_today_message():
    today = ["月", "火", "水", "木", "金", "土", "日"][datetime.now().weekday()]
    schedule = load_schedule_from_db()
    if today in schedule:
        msg = f"{today}曜日の担当:\n" + "\n".join(schedule[today])
    else:
        msg = "本日の予定は登録されていません。"
    return msg

@app.route("/", methods=['GET'])
def wakeup():
    return "I'm awake!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"handle error: {e}")
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    if all(kw in text for kw in ["救急", "AM院内", "PM院内", "残り番"]):
        schedule = parse_schedule(text)
        save_schedule_to_db(schedule)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="週間予定を登録しました！"))
    elif "今週の予定を確認" in text:
        schedule = load_schedule_from_db()
        if schedule:
            reply = "\n\n".join([f"{day}曜日:\n" + "\n".join(items) for day, items in schedule.items()])
        else:
            reply = "予定表が登録されていません。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    else:
        pass  # 反応しない

@app.route("/test-reminder", methods=['GET'])
def test_reminder():
    msg = create_today_message()
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=msg))
    return "Test reminder sent.", 200

@app.route("/test-weekly-reminder", methods=['GET'])
def test_weekly_reminder():
    clear_schedule()
    msg = "来週分の週間予定を入力してください。前週のデータは削除されました。"
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=msg))
    return "Weekly reset and prompt sent.", 200

def daily_reminder():
    msg = create_today_message()
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=msg))

def weekly_request_reminder():
    clear_schedule()
    msg = "来週分の週間予定を入力してください。前週のデータは削除されました。"
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=msg))

init_db()
scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(daily_reminder, 'cron', hour=7, minute=30)
scheduler.add_job(weekly_request_reminder, 'cron', day_of_week='sun', hour=19, minute=0)
scheduler.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
