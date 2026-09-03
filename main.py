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
import time
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
    """{ "YYYY-MM-DD": {救急,AM院内,PM院内,AM医連,PM医連,残り番:[1st,2nd]} } を返す。
    読み込み失敗時はSheetsReadError。空辞書を返してしまうと「未登録」と
    区別できず、誤警告(2026-09-01朝に実際に発生)やデータ消失につながる。"""
    try:
        val = _sheet_read_retry(
            lambda: _worksheet("schedule").acell("A1").value, "予定表読み込み"
        )
        if val:
            data = json.loads(val)
            # 日付キー形式のみ受け付ける(旧・曜日形式のデータは無視)
            return {k: v for k, v in data.items() if DATE_RE.match(str(k))}
        return {}
    except SheetsReadError:
        raise
    except Exception as e:
        raise SheetsReadError("予定表の内容が読み取れません: %s" % e)


def save_schedule(new_days):
    """既存データとマージして保存。7日以上前の日付は削除する。
    既存データを読み込めなかった場合は保存せずエラーにする
    (新データだけで上書きして残りの日付が消えるのを防ぐ)。"""
    data = load_schedule()
    data.update(new_days)
    cutoff = (now_jst().date() - datetime.timedelta(days=7)).isoformat()
    data = {d: a for d, a in sorted(data.items()) if d >= cutoff}
    if not _sheet_write_retry(
        lambda: _worksheet("schedule").update("A1", [[json.dumps(data, ensure_ascii=False)]]),
        "予定表保存",
    ):
        raise RuntimeError("予定表の保存に失敗しました。時間をおいて再送してください")
    return data


def _sheet_write_retry(action, desc, attempts=3):
    """Sheetsへの書き込みを最大3回試す(一時的なAPIエラーで記録が消えるのを防ぐ)。
    2026-07-16朝、配信ログの書き込みが一度きり失敗して
    ダッシュボードが「未配信」表示のままになる障害が実際に発生した。"""
    for i in range(attempts):
        try:
            action()
            return True
        except Exception as e:
            sys.stderr.write("%s 失敗(%d回目): %s\n" % (desc, i + 1, e))
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
    return False


class SheetsReadError(Exception):
    """Sheetsからの読み込みが(リトライ後も)失敗したことを示す"""


def _sheet_read_retry(action, desc, attempts=3):
    """Sheetsからの読み込みを最大3回試す。
    2026-09-01朝、予定表の読み込みが一度きり失敗し、データはあるのに
    「予定が未登録」と誤った警告を配信する障害が実際に発生した。"""
    for i in range(attempts):
        try:
            return action()
        except Exception as e:
            sys.stderr.write("%s 失敗(%d回目): %s\n" % (desc, i + 1, e))
            if i == attempts - 1:
                raise SheetsReadError("%s: %s" % (desc, e))
            time.sleep(2 * (i + 1))


def log_event(level, message):
    """log シートに1行追記(失敗してもbot本体は止めない)"""
    row = [now_jst().strftime("%Y-%m-%d %H:%M"), level, message]
    _sheet_write_retry(
        # RAW指定: Sheetsが日時文字列を勝手に日付型に変換して表示形式を変えるのを防ぐ
        lambda: _worksheet("log").append_row(row, value_input_option="RAW"),
        "ログ記録",
    )


def mark_delivered(date_str):
    """配信済み日付をscheduleシートのB1セルに記録(配信判定の正式な記録)"""
    _sheet_write_retry(
        lambda: _worksheet("schedule").update("B1", [[date_str]]),
        "配信記録",
    )


def load_delivered_date():
    """B1の配信済み日付を返す。読み込み失敗時はSheetsReadError
    (空文字を返すと「未配信」と誤判定し二重配信の恐れがあるため)"""
    return _sheet_read_retry(
        lambda: _worksheet("schedule").acell("B1").value, "配信記録読み込み"
    ) or ""


def load_logs(limit=30):
    try:
        rows = _sheet_read_retry(
            lambda: _worksheet("log").get_all_values(), "ログ読み込み"
        )
        return [
            {"time": r[0], "level": r[1], "message": r[2]}
            for r in rows[-limit:] if len(r) >= 3
        ][::-1]  # 新しい順
    except SheetsReadError:
        # 表示用・配信判定の保険用なので、読めない時は空扱いで続行する
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


def _format_assignment(assignment):
    first, second = (assignment.get("残り番") or ["未設定", "未設定"])[:2]
    return (
        "救急(リハ診)：%s\n"
        "院内：AM %s → PM %s\n"
        "医連：AM %s → PM %s\n"
        "残り番：1st %s ／ 2nd %s"
        % (assignment.get("救急", "未設定"),
           assignment.get("AM院内", "未設定"), assignment.get("PM院内", "未設定"),
           assignment.get("AM医連", "未設定"), assignment.get("PM医連", "未設定"),
           first, second)
    )


def create_reminder(days, today):
    """[(date, assignment), ...] → 配信文。当日は「本日」、それ以外は日付見出し
    (金曜は土日の分もまとめて配信するため複数日になる)"""
    blocks = []
    for date, a in days:
        title = "【本日の担当者】" if date == today else "【%sの担当者】" % format_date_ja(date)
        blocks.append(title + "\n" + _format_assignment(a))
    return "\n\n".join(blocks) + "\n\nよろしくお願いします！"


def _reminder_targets(today_d):
    """その日の配信対象日リスト。金曜は土日の分もまとめる(土日は自動配信を休止するため)"""
    targets = [today_d.isoformat()]
    if today_d.weekday() == 4:  # 金曜
        targets += [(today_d + datetime.timedelta(days=i)).isoformat() for i in (1, 2)]
    return targets


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
def _time_is_today(time_str, today):
    """ログの日時文字列が今日かどうか。
    Sheetsに表示形式を変えられている可能性があるため、
    2026-07-16 / 2026/07/16 / 7/16/2026(米国式)のどれでも判定できるようにする。"""
    t = str(time_str).strip()
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", t)          # 年/月/日
    if m:
        y, mo, d = m.groups()
    else:
        m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})", t)      # 月/日/年(米国式)
        if not m:
            return False
        mo, d, y = m.groups()
    return "%04d-%02d-%02d" % (int(y), int(mo), int(d)) == today


def delivered_today(logs=None):
    """今日すでに配信済みか。
    正式にはscheduleシートB1の配信済み日付で判定し、
    保険としてログの「配信」記録の日時でも照合する。"""
    today = now_jst().date().isoformat()
    if load_delivered_date() == today:
        return True
    if logs is None:
        logs = load_logs()
    return any(
        l["level"] == "配信" and _time_is_today(l["time"], today)
        for l in logs
    )


def daily_reminder():
    today_d = now_jst().date()
    today = today_d.isoformat()
    if today_d.weekday() >= 5:
        # 土日は自動配信を休止(金曜朝にまとめて配信済み。LINE通数の節約も兼ねる)
        log_event("確認", "本日(%s)は土日のため自動配信なし(金曜にまとめて配信済み)" % today)
        return
    try:
        if delivered_today():
            # ダッシュボードから手動配信済みの日は二重配信しない
            log_event("確認", "本日(%s)は配信済みのため自動配信をスキップ" % today)
            return
        schedule = load_schedule()
    except SheetsReadError as e:
        # 「未登録」と混同させる警告は出さず、一時エラーとして知らせる
        sys.stderr.write("自動配信中断: %s\n" % e)
        log_event("警告", "一時的なエラーで予定表を読み込めず、自動配信できませんでした")
        push(GROUP_ID_B, "⚠ 一時的なエラーで本日(%s)の予定を読み込めず、リマインドを配信できませんでした。\n予定は消えていません。ダッシュボードの「未配信」をタップして手動配信してください。" % format_date_ja(today))
        return
    targets = _reminder_targets(today_d)
    available = [(d, schedule[d]) for d in targets if d in schedule]
    missing = [d for d in targets if d not in schedule]
    if available:
        push(GROUP_ID_A, create_reminder(available, today))
        mark_delivered(today)
        log_event("配信", "%sの担当を配信" % "・".join(format_date_ja(d) for d, _ in available))
    if missing:
        miss_ja = "・".join(format_date_ja(d) for d in missing)
        if available:
            msg = "⚠ %sの予定が未登録のため、その分は今朝の配信に含められませんでした。\nPDFを投稿するか、テキストで登録してください。" % miss_ja
        else:
            msg = "⚠ %sの予定が未登録のため、リマインドを配信できませんでした。\nPDFを投稿するか、テキストで登録してください。" % miss_ja
        push(GROUP_ID_B, msg)
        log_event("警告", "%sの予定が未登録" % miss_ja)


def weekly_check():
    """日曜19:00: 来週分が未登録なら催促(全消去は廃止)"""
    today = now_jst().date()
    next_monday = today + datetime.timedelta(days=(7 - today.weekday()))
    try:
        schedule = load_schedule()
    except SheetsReadError:
        # 保険的なチェックなので誤った催促はせず、記録だけ残して次週に委ねる
        log_event("警告", "一時的なエラーで予定表を読み込めず、来週分チェックをスキップ")
        return
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
        try:
            schedule = load_schedule()
        except SheetsReadError:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="一時的なエラーで予定を読み込めませんでした。少し待ってからもう一度お試しください。"))
            return
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
@app.errorhandler(SheetsReadError)
def _handle_sheets_read_error(e):
    """ダッシュボードAPI用: 読み込み失敗を「データなし」と偽らず明示的に返す"""
    sys.stderr.write("Sheets読み込みエラー応答: %s\n" % e)
    return jsonify({"error": "一時的なエラーで予定表を読み込めませんでした。少し待ってから再読み込みしてください。"}), 503


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
    today_d = now_jst().date()
    today = today_d.isoformat()
    schedule = load_schedule()
    # 金曜は自動配信と同様に土日分もまとめる。土日の手動配信は当日分のみ(臨時用)
    targets = _reminder_targets(today_d) if today_d.weekday() < 5 else [today]
    available = [(d, schedule[d]) for d in targets if d in schedule]
    if not available:
        return jsonify({"error": "本日の予定が未登録のため配信できません"}), 400
    push(GROUP_ID_A, create_reminder(available, today))
    mark_delivered(today)
    log_event("配信", "%sの担当を配信(ダッシュボードから手動)" % "・".join(format_date_ja(d) for d, _ in available))
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
