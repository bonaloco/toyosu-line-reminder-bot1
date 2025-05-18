import os
import json
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import pytz

# 環境変数から取得
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
GROUP_ID_A = os.getenv("GROUP_ID_A")
GROUP_ID_B = os.getenv("GROUP_ID_B")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = Flask(__name__)
weekly_schedule = {}

# ファイル保存
def save_schedule_to_file(schedule):
    with open("weekly_schedule.json", "w") as f:
        json.dump(schedule, f)

def load_schedule_from_file():
    global weekly_schedule
    try:
        with open("weekly_schedule.json", "r") as f:
            weekly_schedule = json.load(f)
    except FileNotFoundError:
        weekly_schedule = {}

# 予定のパース
def parse_schedule(text):
    lines = text.strip().split("\n")
    return {"lines": lines}

# 今週の予定の要約
def create_weekly_summary(schedule):
    return "\n".join(schedule.get("lines", ["予定が登録されていません"]))

# 毎朝のリマインダー
def daily_reminder():
    load_schedule_from_file()
    today = datetime.now(pytz.timezone("Asia/Tokyo")).weekday()
    if today >= 7:
        return
    msg = weekly_schedule.get("lines", ["予定が登録されていません"])[today] if weekly_schedule else "予定が登録されていません"
    for group_id in [GROUP_ID_A, GROUP_ID_B]:
        line_bot_api.push_message(group_id, TextSendMessage(text=f"本日の当番: {msg}"))

# 毎週日曜の登録リマインド
def weekly_request_reminder():
    line_bot_api.push_message(GROUP_ID_B, TextSendMessage(text="来週の週間予定を登録してください。"))

# LINE webhook エンドポイント
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        abort(400)
    return "OK"

# メッセージ受信時の処理
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    global weekly_schedule
    text = event.message.text
    group_id = getattr(event.source, "group_id", None)

    if text.startswith("救急") and all(x in text for x in ["AM院内", "PM院内", "残り番"]):
        if group_id == GROUP_ID_B:
            weekly_schedule = parse_schedule(text)
            save_schedule_to_file(weekly_schedule)
            reply = "週間予定を登録しました！"
        else:
            reply = "このグループでは週間予定の登録はできません。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    elif "今週の予定を確認" in text:
        summary = create_weekly_summary(weekly_schedule)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=summary))

# スリープ対策用エンドポイント
@app.route("/", methods=["GET"])
def wakeup():
    return "I'm awake!", 200

# テスト用手動エンドポイント
@app.route("/test-reminder", methods=["GET"])
def test_reminder():
    daily_reminder()
    return "Daily reminder sent", 200

@app.route("/test-weekly-reminder", methods=["GET"])
def test_weekly():
    weekly_request_reminder()
    return "Weekly reminder sent", 200

# スケジューラー
scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(daily_reminder, "cron", hour=7, minute=30)
scheduler.add_job(weekly_request_reminder, "cron", day_of_week="sun", hour=19, minute=0)
scheduler.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
