import os
import sys
import json
import datetime
import pytz
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

# ファイル保存用パス
SCHEDULE_FILE = "schedule.json"

def save_schedule_to_file(schedule):
    try:
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(schedule, f, ensure_ascii=False, indent=2)
    except Exception as e:
        sys.stderr.write(f"保存エラー: {e}\n")

def load_schedule_from_file():
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

# 起動時に読み込み
weekly_schedule = load_schedule_from_file()

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
    jst = pytz.timezone('Asia/Tokyo')
    today = datetime.datetime.now(jst)
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
    global weekly_schedule
    message = "【お知らせ】\n来週分の週間予定表を入力してください！\n（※現在の予定はリセットされました）"
    line_bot_api.push_message(GROUP_ID, TextSendMessage(text=message))
    weekly_schedule = {}
    try:
        if os.path.exists(SCHEDULE_FILE):
            os.remove(SCHEDULE_FILE)
            sys.stderr.write("✅ 旧スケジュールを削除しました。\n")
    except Exception as e:
        sys.stderr.write(f"スケジュール削除エラー: {e}\n")

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
    sys.stderr.write("===================\n")

    if '救急' in text and 'AM院内' in text and 'PM院内' in text and '残り番' in text:
        weekly_schedule = parse_schedule(text)
        save_schedule_to_file(weekly_schedule)
        reply = "週間予定表を登録しました！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    elif '今週の予定を確認' in text:
        summary = create_weekly_summary(weekly_schedule)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=summary))
    else:
        pass

@app.route("/", methods=["GET"])
def wakeup():
    return "I'm awake!", 200

@app.route("/test-reminder", methods=["GET"])
def test_reminder():
    daily_reminder()
    return "Daily reminder sent manually", 200

@app.route("/test-weekly-reminder", methods=["GET"])
def test_weekly_reminder():
    weekly_request_reminder()
    return "Weekly reminder sent manually", 200

scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(daily_reminder, 'cron', hour=11, minute=15)
scheduler.add_job(weekly_request_reminder, 'cron', day_of_week='sun', hour=19, minute=0)
scheduler.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
