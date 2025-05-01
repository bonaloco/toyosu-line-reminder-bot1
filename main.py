from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import os
import pytz
import sqlite3

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
        c.execute('''
            CREATE TABLE IF NOT EXISTS schedule (
                category TEXT,
                day INTEGER,
                person TEXT
            )
        ''')
        conn.commit()

def save_schedule_to_db(schedule):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('DELETE FROM schedule')
        for category, names in schedule.items():
            for day, person in enumerate(names):
                c.execute('INSERT INTO schedule (category, day, person) VALUES (?, ?, ?)', (category, day, person))
        conn.commit()

def load_schedule_from_db():
    schedule = {"救急": [], "AM院内": [], "PM院内": [], "残り番": []}
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        for category in schedule.keys():
            c.execute('SELECT person FROM schedule WHERE category = ? ORDER BY day ASC', (category,))
            results = c.fetchall()
            schedule[category] = [row[0] for row in results]
    return schedule

def clear_schedule_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('DELETE FROM schedule')
        conn.commit()

def parse_schedule(text):
    blocks = text.strip().split("\n")
    schedule = {"救急": [], "AM院内": [], "PM院内": [], "残り番": []}
    current = None
    for line in blocks:
        line = line.strip()
        if line in schedule:
            current = line
        elif current:
            schedule[current].append(line)
    return schedule

def create_today_message(schedule):
    today = datetime.now(pytz.timezone('Asia/Tokyo')).weekday()
    days = ['月', '火', '水', '木', '金', '土', '日']
    msg = f"{days[today]}曜日の当番\n"
    for key in ["救急", "AM院内", "PM院内", "残り番"]:
        if today < len(schedule[key]):
            label = "救急(リハ診)" if key == "救急" else key
            if key == "残り番":
                value1 = schedule[key][today] if today < len(schedule[key]) else ""
                value2 = schedule[key][today + 7] if today + 7 < len(schedule[key]) else ""
                msg += f"{label}：{value1} / {value2}\n"
            else:
                msg += f"{label}：{schedule[key][today]}\n"
    return msg

def create_schedule_summary(schedule):
    msg = "\n\n【登録された週間予定】\n"
    for key in ["救急", "AM院内", "PM院内", "残り番"]:
        msg += f"{key}\n" + "\n".join(schedule[key]) + "\n"
    return msg

def daily_reminder():
    schedule = load_schedule_from_db()
    if schedule["救急"]:
        msg = create_today_message(schedule)
    else:
        msg = "予定表未登録"
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=msg))

def weekly_request_reminder():
    clear_schedule_db()
    msg = "来週分の週間予定を送ってください"
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=msg))

@app.route("/", methods=["GET"])
def wakeup():
    return "I'm awake!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print("Error:", e)
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    if all(k in text for k in ['救急', 'AM院内', 'PM院内', '残り番']):
        schedule = parse_schedule(text)
        save_schedule_to_db(schedule)
        reply = "週間予定表を登録しました"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    elif '今週の予定を確認' in text:
        schedule = load_schedule_from_db()
        if schedule["救急"]:
            summary = create_schedule_summary(schedule)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=summary))
    else:
        pass

init_db()

scheduler = BackgroundScheduler(timezone='Asia/Tokyo')
scheduler.add_job(daily_reminder, 'cron', hour=7, minute=30)
scheduler.add_job(weekly_request_reminder, 'cron', day_of_week='sun', hour=19, minute=0)
scheduler.start()

if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)