import os
import json
import asyncio
from io import BytesIO
import re
import requests
import numpy as np
from datetime import datetime, timedelta
import logging

import discord
from discord import app_commands, ui, Interaction, Embed
from discord.ext import commands
from dotenv import load_dotenv
from pandas import DataFrame, json_normalize
import pandas as pd
import requests
import os
import json
import asyncio
import gspread_asyncio
from google.oauth2.service_account import Credentials
from discord import app_commands, Interaction
from discord.ext import commands
from src.mypage import (load_course_meta_map_sync, _fetch_user_record_one_round_sync,
                        _fetch_user_records_all_rounds_sync, _authorize_gc, _get_lr2id_by_discord_sync)
from src.result import build_id_to_name_from_sheet
from src.generate_table import generate_bootstrap_html_table
from src.common import safe_defer

from src import lr2ir  # fetch_lr2_ranking を含む自作モジュール
import gspread

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(name)s: %(message)s',
    handlers=[
        logging.FileHandler('ir_bot.log', encoding='utf-8'),
        logging.StreamHandler()
        ]
)

console_logger = logging.getLogger()
console_logger.setLevel(logging.WARNING)

# === 設定 ===

COURSE_RESULT_FILE = "course_result.json"
LR2ID_DB_FILE = "lr2_users.json"
ANNOUNCE_ROLE_NAME = "管理者"
SHEET_NAME = "NebukawaIR"     # あなたのスプレッドシート名
SHEET_NANE_SCORE = "NebukawaIR(result)"
WS_USERDATA = "UserData"      # タブ名（ヘッダー: DiscordID | LR2ID）
SHEET_ID = os.environ.get("MAIN_ID")          # NebukawaIR のシートIDを .env へ
COURSE_WS = "CourseData"  # タブ名（デフォルト CourseData）

insane_scores = pd.read_csv('insane_scores.csv')
insane_scores["label"] = insane_scores.apply(lambda row: f"★{row['level']} {row['title']}", axis=1)
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)



# === 共通ユーティリティ ===

async def safe_reply(inter: Interaction, content: str, *, ephemeral: bool = True):
    try:
        if inter.response.is_done():
            # すでに最初の応答を済ませている → followup で送る
            await inter.followup.send(content, ephemeral=ephemeral)
        else:
            # まだ応答していない → 初回応答で送る
            await inter.response.send_message(content, ephemeral=ephemeral)
    except discord.errors.NotFound:
        # interaction が失効している場合のフォールバック（任意）
        # 失敗時はログに残すか、許容できるならチャンネルに通知する
        # await inter.channel.send(f"{inter.user.mention} {content}")
        pass

def _create_async_creds():
    sa_info = json.loads(os.environ["GCP_SA_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    return Credentials.from_service_account_info(sa_info, scopes=scopes)
    
def _parse_score(score_text: str):
    SCORE_RE = re.compile(r'(?P<a>\d[\d,]*)\s*/\s*(?P<b>\d[\d,]*)\s*\(\s*(?P<p>[\d.]+)\s*%\s*\)')
    """
    "aaaa/bbbb(cc.cc%)" -> (own:int, rate:float)
    パース失敗は (None, None)
    """
    if not score_text:
        return None, None
    m = SCORE_RE.search(str(score_text))
    if not m:
        return None, None
    own = int(m.group("a").replace(",", ""))
    rate = float(m.group("p"))
    return own, rate

def _authorize_gc():
    sa_info = json.loads(os.environ["GCP_SA_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)

def _open_or_create_ws_by_name(spreadsheet_id: str, ws_title: str):
    HEADERS = ["回", "diff", "title", "CourseID"]  # ← シートの列
    gc = _authorize_gc()
    sh = gc.open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet(ws_title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=ws_title, rows=100, cols=len(HEADERS))
        ws.update("A1", [HEADERS])
    return ws

def upsert_course_row(
    spreadsheet_id: str,
    worksheet_title: str,
    round_no: int,
    diff: str | int,
    title: str,
    course_id: int,
):
    HEADERS = ["回", "diff", "title", "CourseID"]  # ← シートの列
    """
    CourseData タブで指定の「回」が既にあれば上書き、無ければ末尾に追加。
    """
    ws = _open_or_create_ws_by_name(spreadsheet_id, worksheet_title)

    # A列（回）を取得して探索（ヘッダー除外）
    col = ws.col_values(1)
    header = col[0] if col else ""
    rows = col[1:] if header == HEADERS[0] else col
    base_row = 2 if header == HEADERS[0] else 1

    try:
        idx = [int(x) for x in rows].index(int(round_no))
        rownum = base_row + idx
        # 行全体を上書き（A:回, B:diff, C:title, D:CourseID）
        ws.update(
            values=[[round_no, diff, title, course_id]],
            range_name=f"A{rownum}:D{rownum}"
        )
        return "updated"
    except ValueError:
        ws.append_row([round_no, diff, title, course_id], value_input_option="RAW")
        return "inserted"

def _authorize_gc():
    sa_info = json.loads(os.environ["GCP_SA_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)

def _get_or_create_ws(sh, title: str, rows: int, cols: int):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=max(6, cols))


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def extract_lr2id_from_bytes(content: bytes) -> int:
    for line in content.decode(errors="ignore").splitlines():
        if line.startswith("#COURSEID"):
            return int(line.strip().split()[1])
    raise ValueError("COURSEIDが見つかりませんでした")

def format_difficulty(diff: int) -> str:
    return f"★{diff}"

def pgf(x, m):
    if x == 1:
        return m
    else:
        return 0.5 / (1 - x)

def calculate_bpi(s, k, z, m, p):
    """単曲BPIを計算する関数"""
    S = pgf(s / m, m)
    K = pgf(k / m, m)
    Z = pgf(z / m, m)
    S_prime = S / K
    Z_prime = Z / K

    if s >= k:
        return float(round(100 * (np.log(S_prime) ** p) / (np.log(Z_prime) ** p), 2))
    else:
        return float(round(max(-100 * ((np.abs(np.log(S_prime)) ** p) / (np.log(Z_prime) ** p)), -15), 2))


def write_round_result_to_sheet(spreadsheet_id: str, round_title: str, result_list: list[dict]):
    HEADERS = ["Rank", "LR2ID", "PlayerName", "Score", "Score Rate (%)", "BPI"]
    """
    1回分の result_list（あなたのロジックで作った辞書配列）を
    指定スプレッドシートの「round_title」タブへ一括書き込み（A1起点）。
    カラム: HEADERS
    """
    gc = _authorize_gc()
    sh = gc.open_by_key(spreadsheet_id)

    rows = []
    for e in result_list:
        score_text = e.get("スコア")
        own, rate = _parse_score(score_text)
        rows.append([
            e.get("順位"),
            e.get("LR2ID"),
            e.get("プレイヤー"),
            own,        # Score（自分のスコア）
            rate,       # Score Rate (%)
            e.get("BPI"),
        ])

    values = [HEADERS] + rows
    ws = _get_or_create_ws(sh, round_title, rows=len(values), cols=len(HEADERS))
    ws.update("A1", values, value_input_option="RAW")

def format_difficulty(diff: str) -> str:
    # 例: "★12" or "12" をそのまま/装飾したい場合はここで調整
    # 入力が「★12」の場合はそのまま、"12" なら "★12" など
    if isinstance(diff, str) and diff.startswith("★"):
        return diff
    try:
        return f"★{int(diff)}"
    except:
        return str(diff)

def _authorize_gc():
    sa_info = json.loads(os.environ["GCP_SA_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)

def fetch_course_id_by_round_sync(
    spreadsheet_id: str,
    worksheet_title: str,
    round_value: str | int,
) -> int:
    """
    CourseData から Round==round_value の行を探し、CourseID(int) を返す。
    ヘッダは 'Round' 前提。日本語運用なら '回' もフォールバック。
    見つからなければ ValueError。
    """
    gc = _authorize_gc()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(worksheet_title)

    # [{ 'Round': 1, 'diff': '★12', 'title': 'xxx', 'CourseID': 12345 }, ...]
    rows = ws.get_all_records()
    target = str(round_value).strip()

    for r in rows:
        key_round = r.get("Round", r.get("回"))
        if key_round is None:
            continue
        if str(key_round).strip() == target:
            cid = r.get("CourseID")
            if cid is None or str(cid).strip() == "":
                raise ValueError(f"CourseID が空です（Round={target}）")
            return int(str(cid).strip())

    raise ValueError(f"Round={target} が CourseData に見つかりません")

# === 初期データ読み込み ===
COURSE_JSON_PATH = 'course_id.json'

# === イベント ===
@bot.event
async def on_ready():
    print(f"ログインしました: {bot.user}")
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"コマンド同期エラー: {e}")

# === モーダルによるアナウンス ===

# === モーダル ===
class AnnounceModal(ui.Modal, title="イベントアナウンス"):
    round = ui.TextInput(label="回数（例: 1）", required=True)
    difficulty = ui.TextInput(label="難易度（例: ★12）", required=True)
    songtitle = ui.TextInput(label="曲名（例: Angelic Snow）", required=True)
    lr2id = ui.TextInput(
        label="COURSEID または URL",
        placeholder="例: 13142 or ...courseid=13142",
        required=True
    )

    async def on_submit(self, interaction: Interaction):
        # 最初の応答は defer（以降は followup を使う）
        await interaction.response.defer(ephemeral=True, thinking=True)

        # ★ 環境変数からスプレッドシートID・タブ名を取得（ファイルは読まない）
        sheet_id = os.environ.get("MAIN_ID")             # ← ここを使用
        course_ws = "CourseData"
        if not sheet_id:
            await interaction.followup.send("MAIN_ID が未設定です。.env を確認してください。", ephemeral=True)
            return

        # 開催期間の計算
        now = datetime.now()
        start = now.replace(hour=23, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=(7 - start.weekday() + 1))).replace(hour=23, minute=59, second=59)

        # チャンネル名用スラッグ
        def to_slug(text: str) -> str:
            return ''.join(c.lower() if c.isalnum() else '_' for c in text).strip('_')

        slug = f"{self.round.value}_{to_slug(format_difficulty(self.difficulty.value))}_{to_slug(self.songtitle.value)}"
        channel = await interaction.guild.create_text_channel(slug)

        # LR2ID 抽出（URL or 数値）
        lr2id_raw = self.lr2id.value.strip()
        if "courseid=" in lr2id_raw:
            try:
                lr2id_val = int(lr2id_raw.split("courseid=")[1].split("&")[0])
            except Exception:
                await interaction.followup.send("LR2IDが正しくありません。", ephemeral=True)
                return
        else:
            try:
                lr2id_val = int(lr2id_raw)
            except ValueError:
                await interaction.followup.send("LR2IDが正しくありません。", ephemeral=True)
                return

        # ★ スプレッドシートにアップサート（同期I/Oはスレッドへ）
        diff_value = self.difficulty.value
        loop = interaction.client.loop
        try:
            result = await loop.run_in_executor(
                None,
                upsert_course_row,   # sheets_course_data.py の関数（ファイルは使わない）
                sheet_id,
                course_ws,
                int(self.round.value),
                diff_value,
                str(self.songtitle.value),
                int(lr2id_val),
            )
        except Exception as e:
            await interaction.followup.send(
                f"スプレッドシート書き込みに失敗しました。\n```\n{e}\n```",
                ephemeral=True
            )
            return

        # 案内メッセージ（followup）
        lr2_url = f"http://www.dream-pro.info/~lavalse/LR2IR/search.cgi?mode=ranking&courseid={lr2id_val}"
        lr2_course_url = f"http://www.dream-pro.info/~lavalse/LR2IR/search.cgi?mode=downloadcourse&courseid={lr2id_val}"
        await channel.send(
            f"# 第{self.round.value}回\n"
            f"**{self.songtitle.value}** ({format_difficulty(self.difficulty.value)})\n"
            f"[コースURL]({lr2_url}) [コースファイルダウンロードはここから]({lr2_course_url})\n"
            f"開催期間: {start.strftime('%Y/%m/%d %H:%M:%S')} ～ {end.strftime('%Y/%m/%d %H:%M:%S')}"
        )
        await interaction.followup.send(
            f"{channel.mention} にアナウンスを投稿しました。",
            ephemeral=True
        )

# === コマンド登録 ===
@bot.tree.command(name="announce", description="イベントアナウンス（運営専用）")
async def announce(interaction: Interaction):
    if not any(role.name == ANNOUNCE_ROLE_NAME for role in interaction.user.roles):
        await interaction.response.send_message("このコマンドは運営のみ使用できます。", ephemeral=True)
        return
    await interaction.response.send_modal(AnnounceModal())

async def _safe_defer(interaction: discord.Interaction, ephemeral: bool = True) -> bool:
    """deferを安全に試みる。失敗してもFalseで返す。"""
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        return True
    except discord.NotFound:
        return False
    except discord.HTTPException:
        return False

async def _safe_send(interaction: discord.Interaction, content: str, ephemeral: bool = True):
    """最初の応答/フォローアップを安全に送る。最後の手段で通常メッセージ。"""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except discord.NotFound:
        # ephem は不可だが、とりあえずチャンネルに出す（必要なら運営チャンネル限定にする）
        await interaction.channel.send(content)
    except discord.HTTPException:
        await interaction.channel.send(content)

#リザルト表示
@bot.tree.command(name="result", description="指定した回のランキングを表示")
@app_commands.describe(event="対象の回数（例: 1）")
async def result(interaction: discord.Interaction, event: str):
    # 1) まず超最初にdefer（3秒対策）
    await _safe_defer(interaction, ephemeral=True)

    # 2) 権限チェック
    if not any(role.name == ANNOUNCE_ROLE_NAME for role in interaction.user.roles):
        await _safe_send(interaction, "このコマンドは運営のみ使用できます。", ephemeral=True)
        return

    # 3) CourseData から CourseID を取得（スプレッドシート）
    sheet_id = os.getenv("MAIN_ID")
    output_id = os.getenv("SCORE_ID")
    course_ws = "CourseData"
    if not sheet_id:
        await _safe_send(interaction, "MAIN_ID（または SCORE_ID）が未設定です。.env を確認してください。", ephemeral=True)
        return

    try:
        loop = asyncio.get_running_loop()
        course_id = await loop.run_in_executor(
            None,
            fetch_course_id_by_round_sync,  # 同期関数
            sheet_id,
            course_ws,
            event
        )
    except Exception as e:
        await _safe_send(interaction, f"CourseData から回 {event} の CourseID を取得できませんでした。\n```\n{e}\n```", ephemeral=True)
        return

    # 4) ランキング取得（同期処理が内部であればOK／重いなら executor 化）
    df = lr2ir.fetch_lr2_ranking(course_id)
    df = df.dropna()

    required_cols = ["順位", "スコア", "LR2ID"]
    if not all(col in df.columns for col in required_cols):
        await _safe_send(interaction, "必要な列が見つかりませんでした。", ephemeral=True)
        return

    player_col = next((c for c in df.columns if "プレイヤー" in c or "名前" in c), None)
    if not player_col:
        await _safe_send(interaction, "プレイヤー名の列が見つかりませんでした。", ephemeral=True)
        return

    # 5) BMSID 取得（HTTPは速いが、失敗時のガードのみ）
    try:
        course_url = f"http://www.dream-pro.info/~lavalse/LR2IR/search.cgi?mode=ranking&courseid={course_id}"
        html = requests.get(course_url, timeout=10).text
        m = re.search(r'search\.cgi\?mode=ranking&bmsid=(\d+)', html)
        if not m:
            await _safe_send(interaction, "BMSIDの取得に失敗しました。", ephemeral=True)
            return
        bmsid = int(m.group(1))
    except Exception as e:
        await _safe_send(interaction, f"BMSID取得中にエラー: {e}", ephemeral=True)
        return

    # 6) insane_scores からパラメータ取得
    score_row = insane_scores[insane_scores['lr2_bmsid'] == bmsid]
    if score_row.empty:
        await _safe_send(interaction, "insane_scoresに該当するBMSIDが見つかりませんでした。", ephemeral=True)
        return

    s_row = score_row.iloc[0]
    m = s_row["theoretical_score"]
    k = s_row["average_score"]
    z = s_row["top_score"]
    p = max(s_row["optimized_p"], 0.8)

    # 7) BPI算出 & リスト化
    result_list = []
    df = df.sort_values("順位").reset_index(drop=True)
    for _, row in df.iterrows():
        lr2id = str(row["LR2ID"])
        score_str = row["スコア"]
        mscore = re.match(r"(\d+)/", score_str)
        s = int(mscore.group(1)) if mscore else 0
        raw_bpi = calculate_bpi(s, k, z, m, p)
        bpi = round(raw_bpi, 2) if not np.isnan(raw_bpi) else -15

        result_list.append({
            "順位": int(row["順位"]),
            "LR2ID": lr2id,
            "プレイヤー": row[player_col],
            "スコア": score_str,
            "PG": int(row.get("PG", 0)),
            "GR": int(row.get("GR", 0)),
            "BPI": bpi
        })

    # 8) スプレッドシートへ書き込み（同期I/Oは executor）
    try:
        await _safe_send(interaction, "スプレッドシートへ書き込み中…", ephemeral=True)
        await loop.run_in_executor(
            None,
            write_round_result_to_sheet,  # (spreadsheet_id, round_title, result_list)
            output_id,
            str(event),   # タブ名
            result_list
        )
    except Exception as e:
        await _safe_send(interaction, f"スプレッドシート書き込みに失敗しました。\n```\n{e}\n```", ephemeral=True)
        return

    # 9) 表示メッセージ作成
    id_to_name = await build_id_to_name_from_sheet(interaction.guild)

    medals = ["🥇", "🥈", "🥉"]
    msg = f"**第{event}回 ランキング結果**\n"
    medal_idx = 0
    prev_rank = None
    count_same_rank = 0

    for row in result_list:
        rank = row["順位"]
        name = id_to_name.get(row["LR2ID"], row["プレイヤー"])
        score = row["スコア"]
        bpi = row["BPI"]

        if prev_rank is not None and rank != prev_rank:
            medal_idx += count_same_rank
            count_same_rank = 0

        prefix = medals[medal_idx] if medal_idx < len(medals) else f"{rank}位"
        msg += f"{prefix} {name} - {score} - BPI: {bpi}\n"
        prev_rank = rank
        count_same_rank += 1

    await _safe_send(interaction, msg, ephemeral=False)

# === BPI計算 ===
@bot.tree.command(name="bpi", description="スコアからBPIを計算")
@app_commands.describe(song="★難易度と曲名（例: ★16 Born [29Another]）", score="あなたのスコア（整数）")
async def bpi(interaction: discord.Interaction, song: str, score: int):
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        level_title = song.strip()
        row = insane_scores[insane_scores["label"] == level_title].iloc[0]
    except IndexError:
        await interaction.followup.send("該当する楽曲が見つかりませんでした。", ephemeral=True)
        return

    bpi = calculate_bpi(
        s=score,
        k=row["average_score"],
        z=row["top_score"],
        m=row["theoretical_score"],
        p=row["optimized_p"]
    )

    await interaction.followup.send(
        f"**{row['title']} (★{row['level']}) の BPI**\n"
        f"あなたのスコア: {score}\n"
        f"→ **BPI: {bpi}**",
        ephemeral=True
    )

# === オートコンプリート ===
@bpi.autocomplete("song")
async def song_autocomplete(interaction: discord.Interaction, current: str):
    filtered = [
        label for label in insane_scores["label"]
        if current.lower() in label.lower()
    ][:25]  # Discordの制限

    return [app_commands.Choice(name=label, value=label) for label in filtered]

# === 登録・マイページ ===
class LR2Cog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.agcm = gspread_asyncio.AsyncioGspreadClientManager(_create_async_creds)
        self._agc = None  # authorized client cache

    async def _get_ws(self):
        # 使い回し用にクライアントをキャッシュ
        if self._agc is None:
            self._agc = await self.agcm.authorize()
        sh = await self._agc.open(SHEET_NAME)
        ws = await sh.worksheet(WS_USERDATA)
        return ws

    async def _upsert_user(self, discord_id: str, lr2id: str):
        """
        UserData シートで DiscordID が既に存在すれば LR2ID を上書き、
        無ければ末尾に追記する。
        期待ヘッダー: A列=DiscordID, B列=LR2ID
        """
        ws = await self._get_ws()

        # 1) DiscordID 列を一括取得（ヘッダーを除く）
        #   - 先頭行がヘッダー想定なので A2:A を取得
        col = await ws.col_values(1)  # ['DiscordID', '123...', '456...', ...] の可能性がある
        if col and col[0] == "DiscordID":
            discord_ids = col[1:]  # ヘッダー除外
            base_row = 2
        else:
            # ヘッダーがない場合もケア（そのまま先頭から扱う）
            discord_ids = col
            base_row = 1

        # 2) 既存検索
        try:
            idx = discord_ids.index(discord_id)
            row_num = base_row + idx
            # B列(LR2ID) を更新
            # batch_update でもよいが単セルなら update_cell が簡単
            await ws.update_cell(row_num, 2, lr2id)
            return "updated"
        except ValueError:
            # 3) 見つからなければ追記
            await ws.append_row([discord_id, lr2id], value_input_option="RAW")
            return "inserted"


    @app_commands.command(name="register", description="自分のLR2IDを登録")
    @app_commands.describe(lr2id="LR2IRのplayerid")
    async def register(self, interaction: Interaction, lr2id: str):
        discord_id = str(interaction.user.id)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self._upsert_user(discord_id, lr2id)
            if result == "updated":
                msg = f"更新しました。(LR2ID を `{lr2id}` に変更)"
            else:
                msg = f"新規登録しました。"
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            # 権限（シェア）/ シート名 / ネットワーク等のエラー向け
            await interaction.followup.send(
                f"登録に失敗しました。設定を確認してください。\n```\n{e}\n```",
                ephemeral=True
            )

    @app_commands.command(name="mypage", description="自分の過去のランキングを確認")
    @app_commands.describe(event="対象の回数（例: 1）、または 'all'")
    async def mypage(self, interaction: Interaction, event: str):
        # ▼ ここを UserData 参照に変更 ▼
        userdata_sheet_id = os.getenv("USERDATA_ID") or os.getenv("MAIN_ID")
        userdata_ws = os.getenv("USERDATA_WS", "UserData")
        if not userdata_sheet_id:
            await interaction.response.send_message("USERDATA_ID（または MAIN_ID）が未設定です。.env を確認してください。", ephemeral=True)
            return

        # まず defer（後続は followup で）
        await interaction.response.defer(thinking=True, ephemeral=True)

        # UserData から自分の LR2ID を取得（同期I/Oは executor に逃がす）
        import asyncio
        loop = asyncio.get_running_loop()
        try:
            lr2id = await loop.run_in_executor(
                None,
                _get_lr2id_by_discord_sync,
                userdata_sheet_id,
                userdata_ws,
                str(interaction.user.id)
            )
        except Exception as e:
            await interaction.followup.send(f"UserData 参照中にエラーが発生しました。\n```\n{e}\n```", ephemeral=True)
            return

        if not lr2id:
            await interaction.followup.send("先に `/register` でLR2IDを登録してください。（UserDataに見つかりませんでした）", ephemeral=True)
            return

        main_sheet_id   = os.getenv("MAIN_ID")            # NebukawaIR（CourseData）
        result_sheet_id = os.getenv("SCORE_ID")           # NebukawaIR(result)
        course_ws = os.getenv("COURSE_WS", "CourseData")

        if not result_sheet_id:
            await interaction.response.send_message("SCORE_ID が未設定です。.env を確認してください。", ephemeral=True)
            return
        if not main_sheet_id:
            await interaction.response.send_message("MAIN_ID が未設定です（曲名・難易度の表示に必要）。", ephemeral=True)
            return

        await safe_defer(interaction, ephemeral=True)
        loop = asyncio.get_running_loop()

        # ✅ CourseData を一度だけロードして辞書に
        try:
            meta_map = await loop.run_in_executor(None, load_course_meta_map_sync, main_sheet_id, course_ws)
        except Exception as e:
            await interaction.followup.send(f"CourseData 取得に失敗しました。\n```\n{e}\n```", ephemeral=True)
            return

        # ========== all ==========
        if event.lower() == "all":
            try:
                all_records = await loop.run_in_executor(
                    None, _fetch_user_records_all_rounds_sync, result_sheet_id, lr2id
                )
            except Exception as e:
                await interaction.followup.send(f"結果シート参照中にエラー: {e}", ephemeral=True)
                return

            if not all_records:
                await interaction.followup.send("記録が見つかりませんでした。", ephemeral=True)
                return

            combined = []
            for rec in all_records:
                rno = rec["round"]
                row = rec["row"]
                total = rec["total"]

                meta = meta_map.get(str(rno), {"title": "", "diff": ""})
                title = meta.get("title", "")
                diff  = meta.get("diff", "")

                rank = int(row.get("Rank"))
                color_map = {1: "gold", 2: "silver", 3: "#cd7f32"}
                rank_str = (
                    f'<span style="color:{color_map[rank]}; font-weight:bold;">{rank}位</span>'
                    if rank in color_map else f"{rank}位"
                )

                combined.append({
                    "回": int(rno),
                    "曲名": title,
                    "難易度": format_difficulty(diff) if diff else "",
                    "順位": f"{rank_str} / {total}人",
                    "スコア": row.get("Score"),
                    "スコアレート": f"{row.get('Score Rate (%)')}%",
                    "BPI": row.get("BPI"),
                })

            result_df = DataFrame(combined).sort_values("回")
            html = generate_bootstrap_html_table(result_df, "あなたのねぶかわウィークリー成績一覧")
            html_bytes = BytesIO(html.encode("utf-8"))
            await interaction.followup.send(
                content="あなたの全記録をHTML形式で送信します。",
                file=discord.File(html_bytes, filename="mypage_all.html"),
                ephemeral=True
            )
            return

        # ========== 単一回 ==========
        try:
            rec, total = await loop.run_in_executor(
                None, _fetch_user_record_one_round_sync, result_sheet_id, event, lr2id
            )
        except Exception as e:
            await interaction.followup.send(f"結果シート参照中にエラー: {e}", ephemeral=True)
            return

        if not rec:
            await interaction.followup.send(f"第{event}回での記録が見つかりませんでした。", ephemeral=True)
            return

        meta = meta_map.get(str(int(float(event)))) or meta_map.get(str(event), {"title":"", "diff":""})
        title = meta.get("title", "")
        diff  = meta.get("diff", "")

        embed = Embed(
            title=f"第{event}回 ランキング",
            description=f"{title}（{format_difficulty(diff) if diff else diff}）",
            color=discord.Color.green()
        )
        embed.add_field(name="順位", value=f"{int(rec.get('Rank'))} 位 / {total}人", inline=True)
        embed.add_field(name="スコア", value=f"{rec.get('Score')}", inline=True)
        embed.add_field(name="スコアレート", value=f"{rec.get('Score Rate (%)')}%", inline=True)
        embed.add_field(name="BPI", value=str(rec.get('BPI')), inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Botの使い方とコマンド一覧を表示します")
    async def help(self, interaction: Interaction):
        embed = Embed(
            title="📘 IR Bot ヘルプ",
            description="このBotで使える主なコマンドと機能一覧です。",
            color=0x3498db
        )

        embed.add_field(
            name="/register [LR2ID]",
            value="自分のDiscordアカウントとLR2IDを紐づけます。サーバーに入った人は初めにこのコマンドを実行してください \n 例: `/register 123456`",
            inline=False
        )
        embed.add_field(
            name="/mypage [回数 or all]",
            value="自分の順位・スコア・BPIを表示します。`all`で全履歴をhtml形式で確認できます。 \n 例: `/mypage 1` または `/mypage all`",
            inline=False
        )
        embed.add_field(
            name="/bpi [曲名] [スコア]",
            value="指定した曲のスコアからBPIを計算します。 \n 例: `/bpi ★20 Air 6500 `",
            inline=False
        )
        embed.add_field(
            name="/announce",
            value="新しい大会情報を告知します（管理者用）。",
            inline=False
        )
        embed.add_field(
            name="/result [回数]",
            value="指定された回のランキングを表示します。(管理者用)",
            inline=False
        )

        embed.set_footer(text="質問や不具合は運営(ねぶかわ)かbot制作者(ひたらぎ)までどうぞ！")
        await interaction.response.send_message(embed=embed, ephemeral=True)  # ユーザーのみに表示

@bot.event
async def setup_hook():
    await bot.add_cog(LR2Cog(bot))
    await bot.add_cog(Help(bot))

# === 起動 ===
bot.run(os.getenv("DISCORD_TOKEN"))
