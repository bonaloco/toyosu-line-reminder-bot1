import os
import json
import datetime
import pytz
import sqlite3
from flask import Flask, request, abort
from apscheduler.schedulers.background import BackgroundScheduler
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# 環境変数
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GROUP_ID = os.getenv("LINE_GROUP_ID")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
app = Flask(__name__)

DB_PATH = "schedule.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                section TEXT NOT NULL,
                person TEXT NOT NULL
            )
        ''')

def save_schedule_to_db(schedule_dict):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM schedule")
        for day, roles in schedule_dict.items():
            for section, person in roles.items():
                conn.execute(
                    "INSERT INTO schedule (date, section, person) VALUES (?, ?, ?)",
                    (day, section, person)
                )

def load_schedule_from_db():
    schedule = {}
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT date, section, person FROM schedule").fetchall()
        for date, section, person in rows:
            schedule.setdefault(date, {})[section] = person
    return schedule

def parse_schedule(text):
    schedule = {}
    lines = text.strip().splitlines()
    current_day = 0
    sections = ['救急', 'AM院内', 'PM院内', '残り番']
    day_names = ['月', '火', '水', '木', '金', '土', '日']

    for line in lines:
        line = line.strip()
        if not line:
            current_day += 1
            continue
        if current_day >= 7:
            break
        section = sections[len(schedule.get(day_names[current_day], {}))]
        schedule.setdefault(day_names[current_day], {})[section] = line

    return schedule

def create_today_summary():
    today = datetime.datetime.now(pytz.timezone('Asia/Tokyo'))
    weekday = ['月', '火', '水', '木', '金', '土', '日'][today.weekday()]
    schedule = load_schedule_from_db()
    if weekday not in schedule:
        return "予定表未登録"
    today_info = schedule[weekday]
    lines = [f"【{weekday}曜日の予定】"]
    for section, person in today_info.items():
        if section == "救急":
            lines.append(f"{section}（リハ診）: {person}")
        elif section == "残り番":
            first, second = person.split() if " " in person else (person, "")
            lines.append(f"{section}: 1st {first}, 2nd {second}")
        else:
            lines.append(f"{section}: {person}")
    return "\n".join(lines)

def clear_schedule():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM schedule")

def daily_reminder():
    msg = create_today_summary()
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=msg))

def weekly_request_reminder():
    clear_schedule()
    msg = "来週の予定表を入力してください（7列・日別に改行）"
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=msg))

@app.route("/", methods=["GET"])
def root():
    return "I'm running", 200

@app.route("/wakeup", methods=["GET"])
def wakeup():
    return "I'm awake", 200

@app.route("/test-reminder", methods=["GET"])
def test_reminder():
    daily_reminder()
    return "Daily reminder sent manually", 200

@app.route("/test-weekly-reminder", methods=["GET"])
def test_weekly_reminder():
    weekly_request_reminder()
    return "Weekly reminder sent manually", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Error: {e}")
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    if '救急' in text and 'AM院内' in text and 'PM院内' in text and '残り番' in text:
        schedule = parse_schedule(text)
        save_schedule_to_db(schedule)
        reply = "週間予定表を登録しました！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    elif '今週の予定を確認' in text:
        summary = create_today_summary()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=summary))

# 初期化とスケジューラー設定
init_db()

scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(daily_reminder, 'cron', hour=7, minute=30)
scheduler.add_job(weekly_request_reminder, 'cron', day_of_week='sun', hour=19, minute=0)
scheduler.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
