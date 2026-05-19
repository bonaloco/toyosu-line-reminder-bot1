import os
import sys
import json
import datetime
import pytz
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ── 環境変数 ────────────────────────────────────────────
CHANNEL_SECRET        = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GROUP_ID_A            = os.getenv("GROUP_ID_A")   # リマインド送信先グループ
GROUP_ID_B            = os.getenv("GROUP_ID_B")   # 週間予定入力グループ
SPREADSHEET_ID        = os.getenv("SPREADSHEET_ID")
TRIGGER_TOKEN         = os.getenv("TRIGGER_TOKEN")  # cron-job.orgからの認証トークン
GOOGLE_CREDS_JSON     = os.getenv("GOOGLE_CREDS_JSON")  # サービスアカウントJSONを1行に

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(CHANNEL_SECRET)

# ── Google Sheets クライアント ───────────────────────────
def get_sheet():
    """Google Sheetsのワークシートを取得する"""
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds  = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc     = gspread.authorize(creds)
    sh     = gc.open_by_key(SPREADSHEET_ID)
    # "schedule" という名前のシートを使う（なければ自動作成）
    try:
        ws = sh.worksheet("schedule")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="schedule", rows="50", cols="10")
    return ws

def save_schedule(schedule: dict):
    """スケジュールをGoogle Sheetsに保存（セルA1にJSON文字列として保存）"""
    try:
        ws = get_sheet()
        ws.update("A1", [[json.dumps(schedule, ensure_ascii=False)]])
        sys.stderr.write("✅ スケジュールをSheetsに保存しました\n")
    except Exception as e:
        sys.stderr.write(f"❌ Sheets保存エラー: {e}\n")
        raise

def load_schedule() -> dict:
    """Google Sheetsからスケジュールを読み込む"""
    try:
        ws  = get_sheet()
        val = ws.acell("A1").value
        if val:
            return json.loads(val)
    except Exception as e:
        sys.stderr.write(f"❌ Sheets読み込みエラー: {e}\n")
    return {}

def clear_schedule():
    """Google Sheetsのスケジュールをクリアする"""
    try:
        ws = get_sheet()
        ws.update("A1", [[""]])
        sys.stderr.write("✅ スケジュールをクリアしました\n")
    except Exception as e:
        sys.stderr.write(f"❌ Sheetsクリアエラー: {e}\n")

# ── スケジュールのパース／整形 ───────────────────────────
def parse_schedule(text: str) -> dict:
    sections = {
        '救急': [], 'AM院内': [], 'PM院内': [],
        'AM医連': [], 'PM医連': [], '残り番': []
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

def get_today_assignment(sections: dict) -> dict:
    jst     = pytz.timezone('Asia/Tokyo')
    today   = datetime.datetime.now(jst)
    weekday = today.weekday()  # 月=0 … 日=6
    assignment = {}
    for key in ['救急', 'AM院内', 'PM院内', 'AM医連', 'PM医連']:
        lst = sections.get(key, [])
        assignment[key] = lst[weekday] if weekday < len(lst) else "未設定"
    idx = weekday * 2
    zanri = sections.get('残り番', [])
    assignment['残り番'] = (
        zanri[idx]   if idx   < len(zanri) else "未設定",
        zanri[idx+1] if idx+1 < len(zanri) else "未設定",
    )
    return assignment

def create_reminder(assignments: dict) -> str:
    if not assignments:
        return "本日はリマインド対象日ではありません。"
    first, second = assignments['残り番']
    return (
        "【本日の担当者】\n\n"
        f"救急(リハ診)：{assignments['救急']}\n"
        f"院内：AM {assignments['AM院内']} → PM {assignments['PM院内']}\n"
        f"医連：AM {assignments['AM医連']} → PM {assignments['PM医連']}\n"
        f"残り番：1st {first} ／ 2nd {second}\n\n"
        "よろしくお願いします！"
    )

def create_weekly_summary(sections: dict) -> str:
    if not sections:
        return "まだ週間予定表が登録されていません。"
    days    = ["月", "火", "水", "木", "金", "土", "日"]
    lines   = ["【今週の予定】\n"]
    zanri   = sections.get('残り番', [])
    for i, day in enumerate(days):
        def v(key): return sections.get(key, [])[i] if i < len(sections.get(key, [])) else "未設定"
        first  = zanri[i*2]   if i*2   < len(zanri) else "未設定"
        second = zanri[i*2+1] if i*2+1 < len(zanri) else "未設定"
        lines.append(
            f"{day}曜:\n"
            f" 救急(リハ診)：{v('救急')}\n"
            f" AM院内：{v('AM院内')}　PM院内：{v('PM院内')}\n"
            f" AM医連：{v('AM医連')}　PM医連：{v('PM医連')}\n"
            f" 残り番：1st {first} ／ 2nd {second}\n"
        )
    return "\n".join(lines)

# ── リマインダー本体 ─────────────────────────────────────
def daily_reminder():
    schedule = load_schedule()  # 毎回Sheetsから最新を取得
    if not schedule:
        sys.stderr.write("❌ 予定表未登録\n")
        return
    msg = create_reminder(get_today_assignment(schedule))
    line_bot_api.push_message(GROUP_ID_A, TextSendMessage(text=msg))
    sys.stderr.write("✅ daily reminder 送信完了\n")

def weekly_request_reminder():
    message = (
        "【お知らせ】\n"
        "来週分の週間予定表を入力してください！\n"
        "（※現在の予定はリセットされました）"
    )
    line_bot_api.push_message(GROUP_ID_B, TextSendMessage(text=message))
    clear_schedule()
    sys.stderr.write("✅ weekly reminder 送信・スケジュールクリア完了\n")

# ── Flask エンドポイント ─────────────────────────────────
@app.route("/", methods=["GET"])
def wakeup():
    """死活確認用（cron-job.orgのkeep-aliveにも使える）"""
    return "I'm awake!", 200

def _check_token():
    """cron-job.orgからのリクエストをトークンで認証する"""
    token = request.args.get("token") or request.headers.get("X-Trigger-Token")
    if token != TRIGGER_TOKEN:
        abort(403)

@app.route("/trigger-daily", methods=["GET"])
def trigger_daily():
    """毎朝7:30にcron-job.orgが叩くエンドポイント"""
    _check_token()
    daily_reminder()
    return "Daily reminder sent", 200

@app.route("/trigger-weekly", methods=["GET"])
def trigger_weekly():
    """毎週日曜19:00にcron-job.orgが叩くエンドポイント"""
    _check_token()
    weekly_request_reminder()
    return "Weekly reminder sent", 200

@app.route("/callback", methods=["POST"])
def callback():
    """LINE Webhookの受信口"""
    signature = request.headers["X-Line-Signature"]
    body       = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text

    # デバッグ用：グループIDをログ出力
    if event.source.type == "group":
        sys.stderr.write(f"Group ID = {event.source.group_id}\n")

    if all(kw in text for kw in ['救急', 'AM院内', 'PM院内', '残り番']):
        # 週間予定表の登録
        schedule = parse_schedule(text)
        save_schedule(schedule)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="✅ 週間予定表を登録しました！")
        )
    elif '今週の予定を確認' in text:
        schedule = load_schedule()
        summary  = create_weekly_summary(schedule)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=summary)
        )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
