# -*- coding: utf-8 -*-
"""
整形当番bot — PDF自動読み取り版
group Aに投稿される週間予定PDFをClaude APIで解析し、
毎朝の担当リマインドをgroup Aに自動配信する。
管理ダッシュボード(/admin)付き。
"""
import base64
import json
import os
import re
import sys
import datetime
import threading

import pytz
from flask import Flask, request, abort, jsonify, render_template
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, FileMessage, TextSendMessage
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ── 環境変数 ────────────────────────────────────────────
CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GROUP_ID_A           = os.getenv("GROUP_ID_A", "")   # リマインド送信先グループ
GROUP_ID_B           = os.getenv("GROUP_ID_B", "")   # 登録・確認用グループ
SPREADSHEET_ID       = os.getenv("SPREADSHEET_ID", "")
TRIGGER_TOKEN        = os.getenv("TRIGGER_TOKEN", "")     # cron-job.org 認証
ADMIN_TOKEN          = os.getenv("ADMIN_TOKEN", "")       # 管理ダッシュボード認証
GOOGLE_CREDS_JSON    = os.getenv("GOOGLE_CREDS_JSON", "")
# ANTHROPIC_API_KEY は anthropic SDK が環境変数から自動で読む

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN or "unset")
handler      = WebhookHandler(CHANNEL_SECRET or "unset")

JST = pytz.timezone("Asia/Tokyo")

FIELDS = ["救急", "AM院内", "PM院内", "AM医連", "PM医連"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def now_jst():
    return datetime.datetime.now(JST)


# ── Google Sheets ───────────────────────────────────────
def _spreadsheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def _worksheet(name, rows="200", cols="10"):
    sh = _spreadsheet()
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=rows, cols=cols)


def load_schedule():
    """{ "YYYY-MM-DD": {救急,AM院内,PM院内,AM医連,PM医連,残り番:[1st,2nd]} } を返す"""
    try:
        val = _worksheet("schedule").acell("A1").value
        if val:
            data = json.loads(val)
            # 日付キー形式のみ受け付ける(旧・曜日形式のデータは無視)
            return {k: v for k, v in data.items() if DATE_RE.match(str(k))}
    except Exception as e:
        sys.stderr.write("Sheets読み込みエラー: %s\n" % e)
    return {}


def save_schedule(new_days):
    """既存データとマージして保存。7日以上前の日付は削除する。"""
    data = load_schedule()
    data.update(new_days)
    cutoff = (now_jst().date() - datetime.timedelta(days=7)).isoformat()
    data = {d: a for d, a in sorted(data.items()) if d >= cutoff}
    _worksheet("schedule").update("A1", [[json.dumps(data, ensure_ascii=False)]])
    return data


def log_event(level, message):
    """log シートに1行追記(失敗してもbot本体は止めない)"""
    try:
        ws = _worksheet("log")
        ws.append_row([now_jst().strftime("%Y-%m-%d %H:%M"), level, message])
    except Exception as e:
        sys.stderr.write("ログ記録エラー: %s\n" % e)


def load_logs(limit=30):
    try:
        rows = _worksheet("log").get_all_values()
        return [
            {"time": r[0], "level": r[1], "message": r[2]}
            for r in rows[-limit:] if len(r) >= 3
        ][::-1]  # 新しい順
    except Exception as e:
        sys.stderr.write("ログ読み込みエラー: %s\n" % e)
        return []


# ── Claude による予定表解析 ──────────────────────────────
PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date":       {"type": "string", "description": "その行の日付 YYYY-MM-DD"},
                    "weekday":    {"type": "string", "enum": ["月", "火", "水", "木", "金", "土", "日"],
                                   "description": "表の曜日列に書かれている曜日"},
                    "kyukyu":     {"type": "string", "description": "救急(リハ診)担当の医師名"},
                    "am_innai":   {"type": "string", "description": "AM院内担当の医師名"},
                    "pm_innai":   {"type": "string", "description": "PM院内担当の医師名"},
                    "am_iren":    {"type": "string", "description": "AM医連担当の医師名(AM:外来列の(医連-◯◯)表記)"},
                    "pm_iren":    {"type": "string", "description": "PM医連担当の医師名(PM:外来列の(医連-◯◯)表記)"},
                    "zanban_1st": {"type": "string", "description": "残り番の1人目(上段)"},
                    "zanban_2nd": {"type": "string", "description": "残り番の2人目(下段)"},
                    "gaikin": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "外勤等列の各行をそのまま(例: 石川島ー磯崎、平日休ー山木・久保)。なければ空配列",
                    },
                },
                "required": ["date", "weekday", "kyukyu", "am_innai", "pm_innai",
                             "am_iren", "pm_iren", "zanban_1st", "zanban_2nd", "gaikin"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["days"],
    "additionalProperties": False,
}

PARSE_PROMPT = """あなたは整形外科医局の週間予定表から当番情報を抽出する係です。
この予定表から、日付ごとに次の6項目【だけ】を抽出してください:

1. 救急 … 「救急」列の医師名
2. AM院内 / PM院内 … それぞれの列の医師名
3. AM医連 / PM医連 … 「AM:外来」「PM:外来」列の中に (医連-◯◯) や (医連–◯◯) の形で
   書かれている医師名(括弧と「医連」の文字は除き、医師名だけを抽出)
4. 残り番 … 「残り番」列の【上から2つの人名】を1人目・2人目とする。
   - 「◯◯宿直」のような表記はそのまま含めてよい
   - 「◯◯PRP」のように医師名の後ろに処置名が付いている場合は、
     処置名を除いた医師名を採用する(例: 古屋PRP → 古屋)
   - 「PM◯◯」のような3人目以降の記載は無視する
5. 外勤等 … 「外勤等」列(一番右)の各行を、書かれているまま1行ずつ抽出する
   (例: 「石川島ー磯崎」「平日休ー山木・久保」「PM池田ー藤井」)。
   マーカーや色の情報は無視してよい

ルール:
- 手術・術者・外来担当・外勤等の情報は抽出しない
- 医師名以外の情報(患者に関する情報など)があっても絶対に出力に含めない
- 該当欄が空欄の場合は "未設定" とする
- weekday には表の曜日列に書かれている曜日をそのまま出力する
- 日付はYYYY-MM-DD形式。年が表に書かれていない場合は、今日({today})に
  最も近い将来または現在の週になるよう補完する
- 表に載っている全日付(通常7日分)を出力する"""


def _claude_parse(content_block):
    """content_block(document または text)をClaudeに渡して days のリストを得る"""
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": PARSE_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                content_block,
                {"type": "text", "text": PARSE_PROMPT.format(today=now_jst().date().isoformat())},
            ],
        }],
    )
    if response.stop_reason == "refusal":
        raise ValueError("AIが解析を拒否しました")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["days"]


def _corrected_dates(days):
    """年の自動補正。
    予定表には年が書かれていないためAIが年を誤ることがある。
    表に書かれた曜日と暦が一致する年(今年±1)を探して補正する。
    1件までの曜日読み違いは許容し、それ以上ズレていたらエラーにする。"""
    parsed = []
    for d in days:
        date = d.get("date", "")
        if not DATE_RE.match(date):
            raise ValueError("日付の形式が不正です: %s" % date)
        parsed.append(datetime.date.fromisoformat(date))

    best_offset, best_score = None, -1
    for offset in (0, 1, -1):
        try:
            shifted = [p.replace(year=p.year + offset) for p in parsed]
        except ValueError:
            continue  # うるう日など置換できない場合
        score = sum(
            1 for s, d in zip(shifted, days)
            if WEEKDAY_JA[s.weekday()] == d.get("weekday")
        )
        if score > best_score:
            best_offset, best_score = offset, score

    if best_offset is None or best_score < max(len(days) - 1, 1):
        raise ValueError("日付と曜日の整合が取れません(年の判定に失敗)")

    corrected = [p.replace(year=p.year + best_offset) for p in parsed]
    today = now_jst().date()
    if any(abs((c - today).days) > 200 for c in corrected):
        raise ValueError("今日から離れすぎた日付が含まれています: %s" % corrected[0])
    return [c.isoformat() for c in corrected]


def _clean_name(v):
    """医師名の掃除:
    - 前後の空白と末尾の処置名(PRP等)を除く
    - 「◯◯宿直」は「◯◯(宿直)」に整形する"""
    v = re.sub(r"(PRP|ＰＲＰ)$", "", str(v).strip()).strip()
    v = re.sub(r"^(.+?)[\((]?宿直[\))]?$", r"\1(宿直)", v)
    return v or "未設定"


def _validate_and_convert(days):
    """抽出結果を検証し、保存形式 {date: assignment} に変換する"""
    if not days or len(days) > 14:
        raise ValueError("抽出された日数が不正です(%d日)" % len(days or []))
    dates = _corrected_dates(days)
    result = {}
    for date, d in zip(dates, days):
        values = [_clean_name(d.get(k, "")) for k in
                  ("kyukyu", "am_innai", "pm_innai", "am_iren", "pm_iren",
                   "zanban_1st", "zanban_2nd")]
        for v in values:
            # 防御: 医師名として異常な値(長文・改行=患者情報などの混入疑い)は破棄
            if len(v) > 25 or "\n" in v:
                raise ValueError("医師名として不正な値を検出: %s…" % v[:10])
        gaikin = [str(g).strip() for g in (d.get("gaikin") or [])][:15]
        for g in gaikin:
            if len(g) > 30 or "\n" in g:
                raise ValueError("外勤として不正な値を検出: %s…" % g[:10])
        result[date] = {
            "救急":  values[0], "AM院内": values[1], "PM院内": values[2],
            "AM医連": values[3], "PM医連": values[4],
            "残り番": [values[5], values[6]],
            "外勤": [g for g in gaikin if g],
        }
    return result


def parse_pdf(pdf_bytes):
    """PDFバイト列 → {date: assignment}"""
    block = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
        },
    }
    return _validate_and_convert(_claude_parse(block))


def parse_text(text):
    """手動貼り付けテキスト → {date: assignment}(パース経路をPDFと統一)"""
    block = {"type": "text", "text": "以下は予定表のテキストです:\n\n" + text}
    return _validate_and_convert(_claude_parse(block))


# ── メッセージ整形 ───────────────────────────────────────
def format_date_ja(date_str):
    d = datetime.date.fromisoformat(date_str)
    return "%d/%d(%s)" % (d.month, d.day, WEEKDAY_JA[d.weekday()])


def create_reminder(assignment):
    first, second = (assignment.get("残り番") or ["未設定", "未設定"])[:2]
    return (
        "【本日の担当者】\n\n"
        "救急(リハ診)：%s\n"
        "院内：AM %s → PM %s\n"
        "医連：AM %s → PM %s\n"
        "残り番：1st %s ／ 2nd %s\n\n"
        "よろしくお願いします！"
        % (assignment.get("救急", "未設定"),
           assignment.get("AM院内", "未設定"), assignment.get("PM院内", "未設定"),
           assignment.get("AM医連", "未設定"), assignment.get("PM医連", "未設定"),
           first, second)
    )


def create_summary(days):
    """取込結果の確認用サマリ(group Bに投稿)"""
    lines = ["【予定表を取り込みました】\n以下の内容で毎朝配信します。誤りがあれば修正してください。\n"]
    for date in sorted(days):
        a = days[date]
        z = (a.get("残り番") or ["未設定", "未設定"])[:2]
        lines.append(
            "%s\n 救急:%s 院内:%s→%s\n 医連:%s→%s 残り番:%s/%s"
            % (format_date_ja(date), a.get("救急"), a.get("AM院内"), a.get("PM院内"),
               a.get("AM医連"), a.get("PM医連"), z[0], z[1])
        )
    return "\n".join(lines)


def push(group_id, text):
    line_bot_api.push_message(group_id, TextSendMessage(text=text))


# ── 取り込み共通処理 ─────────────────────────────────────
def ingest(days, source):
    save_schedule(days)
    log_event("成功", "%sから%d日分を取り込み" % (source, len(days)))
    push(GROUP_ID_B, create_summary(days))


# ── 定期実行(cron-job.orgから) ──────────────────────────
def delivered_today(logs=None):
    """今日すでに配信済みか(logシートの記録で判定)"""
    today = now_jst().date().isoformat()
    if logs is None:
        logs = load_logs()
    return any(l["level"] == "配信" and l["time"].startswith(today) for l in logs)


def daily_reminder():
    today = now_jst().date().isoformat()
    if delivered_today():
        # ダッシュボードから手動配信済みの日は二重配信しない
        log_event("確認", "本日(%s)は配信済みのため自動配信をスキップ" % today)
        return
    assignment = load_schedule().get(today)
    if assignment:
        push(GROUP_ID_A, create_reminder(assignment))
        log_event("配信", "本日(%s)の担当を配信" % today)
    else:
        push(GROUP_ID_B, "⚠ 本日(%s)の予定が未登録のため、リマインドを配信できませんでした。\nPDFを投稿するか、テキストで登録してください。" % format_date_ja(today))
        log_event("警告", "本日(%s)の予定が未登録" % today)


def weekly_check():
    """日曜19:00: 来週分が未登録なら催促(全消去は廃止)"""
    today = now_jst().date()
    next_monday = today + datetime.timedelta(days=(7 - today.weekday()))
    schedule = load_schedule()
    has_next_week = any(
        (next_monday + datetime.timedelta(days=i)).isoformat() in schedule
        for i in range(7)
    )
    if not has_next_week:
        push(GROUP_ID_B, "【お知らせ】\n来週分の予定表がまだ取り込まれていません。\nPDFをこのグループに転送するか、テキストで登録してください。")
        log_event("警告", "来週分が未登録(日曜チェック)")
    else:
        log_event("確認", "来週分は登録済み(日曜チェック)")


# ── Flask エンドポイント ─────────────────────────────────
@app.route("/", methods=["GET"])
def wakeup():
    return "I'm awake!", 200


def _check_token(expected):
    token = request.args.get("token") or request.headers.get("X-Trigger-Token")
    if not expected or token != expected:
        abort(403)


@app.route("/trigger-daily", methods=["GET"])
def trigger_daily():
    _check_token(TRIGGER_TOKEN)
    daily_reminder()
    return "Daily reminder processed", 200


@app.route("/trigger-weekly", methods=["GET"])
def trigger_weekly():
    _check_token(TRIGGER_TOKEN)
    weekly_check()
    return "Weekly check processed", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


# ── LINE イベントハンドラ ─────────────────────────────────
# LINEのWebhookは数秒以内の応答が必要。AI解析(30〜60秒)を玄関先で行うと
# タイムアウト→再送→再解析の無限ループになるため、
# (1) 応答は即返し、解析は別スレッドで行う
# (2) 処理済みメッセージIDを記憶し、再送されても二度目は解析しない
_processed_ids = set()


def _mark_processed(message_id):
    """このメッセージが初見なら記憶してTrue、処理済みならFalse"""
    if message_id in _processed_ids:
        return False
    if len(_processed_ids) > 500:
        _processed_ids.clear()
    _processed_ids.add(message_id)
    return True


def _source_group(event):
    return event.source.group_id if event.source.type == "group" else None


def _ingest_pdf_async(message_id, file_name):
    try:
        content = line_bot_api.get_message_content(message_id)
        days = parse_pdf(content.content)
        ingest(days, "PDF(%s)" % file_name)
    except Exception as e:
        sys.stderr.write("PDF取り込みエラー: %s\n" % e)
        log_event("エラー", "PDF取り込み失敗: %s" % e)
        try:
            push(GROUP_ID_B, "⚠ PDF(%s)の読み取りに失敗しました。\nテキストでの手動登録をお願いします。\n(理由: %s)" % (file_name, e))
        except Exception:
            pass


@handler.add(MessageEvent, message=FileMessage)
def handle_file(event):
    group = _source_group(event)
    if group:
        sys.stderr.write("Group ID = %s\n" % group)
    if group not in (GROUP_ID_A, GROUP_ID_B):
        return
    name = (event.message.file_name or "").lower()
    if not name.endswith(".pdf"):
        return
    if not _mark_processed(event.message.id):
        return  # 再送された同じPDFは無視
    threading.Thread(
        target=_ingest_pdf_async,
        args=(event.message.id, event.message.file_name),
        daemon=True,
    ).start()


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    group = _source_group(event)
    if group:
        sys.stderr.write("Group ID = %s\n" % group)
    text = event.message.text

    if "今週の予定を確認" in text:
        schedule = load_schedule()
        today = now_jst().date().isoformat()
        upcoming = {d: a for d, a in schedule.items() if d >= today}
        msg = create_summary(upcoming) if upcoming else "登録済みの予定がありません。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # 手動登録: group Bで「救急」「残り番」を含むテキストを予定表とみなす
    # (AI解析に時間がかかるため、応答は即返して解析は別スレッドで行う)
    if group == GROUP_ID_B and "救急" in text and "残り番" in text:
        if not _mark_processed(event.message.id):
            return
        threading.Thread(target=_ingest_text_async, args=(text,), daemon=True).start()


def _ingest_text_async(text):
    try:
        days = parse_text(text)
        save_schedule(days)
        log_event("成功", "テキストから%d日分を登録" % len(days))
        push(GROUP_ID_B, "✅ %d日分の予定を登録しました。\n「今週の予定を確認」で内容を確認できます。" % len(days))
    except Exception as e:
        log_event("エラー", "テキスト登録失敗: %s" % e)
        try:
            push(GROUP_ID_B, "⚠ テキストの読み取りに失敗しました。(理由: %s)" % e)
        except Exception:
            pass


# ── 管理ダッシュボード ───────────────────────────────────
@app.route("/admin", methods=["GET"])
def admin():
    _check_token(ADMIN_TOKEN)
    return render_template("admin.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    _check_token(ADMIN_TOKEN)
    logs = load_logs()
    return jsonify({
        "now": now_jst().strftime("%Y-%m-%d %H:%M"),
        "today": now_jst().date().isoformat(),
        "delivered_today": delivered_today(logs),
        "logs": logs,
    })


@app.route("/api/deliver", methods=["POST"])
def api_deliver():
    """ダッシュボードの「未配信」タップから、本日の担当を今すぐ配信する"""
    _check_token(ADMIN_TOKEN)
    today = now_jst().date().isoformat()
    assignment = load_schedule().get(today)
    if not assignment:
        return jsonify({"error": "本日の予定が未登録のため配信できません"}), 400
    push(GROUP_ID_A, create_reminder(assignment))
    log_event("配信", "本日(%s)の担当を配信(ダッシュボードから手動)" % today)
    return jsonify({"ok": True})


@app.route("/api/schedule", methods=["GET"])
def api_schedule_get():
    _check_token(ADMIN_TOKEN)
    return jsonify(load_schedule())


@app.route("/api/schedule", methods=["POST"])
def api_schedule_post():
    _check_token(ADMIN_TOKEN)
    body = request.get_json(silent=True) or {}
    date = body.get("date", "")
    a = body.get("assignment") or {}
    if not DATE_RE.match(date):
        return jsonify({"error": "日付の形式が不正です"}), 400
    zanban = a.get("残り番") or []
    assignment = {k: str(a.get(k, "未設定"))[:25] for k in FIELDS}
    assignment["残り番"] = [str(z)[:25] for z in (list(zanban) + ["未設定", "未設定"])[:2]]
    assignment["外勤"] = [str(g).strip()[:30] for g in (a.get("外勤") or []) if str(g).strip()][:15]
    save_schedule({date: assignment})
    log_event("編集", "%s の担当をダッシュボードから修正" % date)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
