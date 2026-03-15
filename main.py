# ============================================================
# IR Bot - メインエントリーポイント
# LR2IR を利用したランキング管理・BPI計算 Discord Bot
# ============================================================

import os
import re
import json
import asyncio
from datetime import datetime, timedelta
import logging

import discord
import gspread
import gspread_asyncio
import numpy as np
import pandas as pd
import requests
from discord import app_commands, ui, Interaction, Embed
from discord.ext import commands
from dotenv import load_dotenv
from pandas import DataFrame
from google.oauth2.service_account import Credentials

from src.mypage import (
    load_course_meta_map_sync,
    _fetch_user_record_one_round_sync,
    _fetch_user_records_all_rounds_sync,
    _get_lr2id_by_discord_sync,
)
from src.result import build_id_to_name_from_sheet
from src.generate_table import generate_bootstrap_html_table
from src.common import safe_defer, _authorize_gc
from src.web_server import store_page, start_web_server
from src import lr2ir  # fetch_lr2_ranking を含む自作モジュール

# .env ファイルから環境変数を読み込む
load_dotenv()

# ============================================================
# ログ設定
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(name)s: %(message)s',
    handlers=[
        logging.FileHandler('ir_bot.log', encoding='utf-8'),
        logging.StreamHandler(),
    ]
)
# コンソールには WARNING 以上のみ表示
console_logger = logging.getLogger()
console_logger.setLevel(logging.WARNING)

# ============================================================
# 定数・設定
# ============================================================
COURSE_RESULT_FILE = "course_result.json"
LR2ID_DB_FILE = "lr2_users.json"
ANNOUNCE_ROLE_NAME = "管理者"
SHEET_NAME = "NebukawaIR"           # メインスプレッドシート名
SHEET_NANE_SCORE = "NebukawaIR(result)"
WS_USERDATA = "UserData"            # ユーザーデータタブ（DiscordID | LR2ID）
SHEET_ID = os.environ.get("MAIN_ID")
COURSE_WS = "CourseData"            # コースデータタブ
COURSE_JSON_PATH = 'course_id.json'
SCORETA_CATEGORY_NAME = "開催中のスコアタ"  # 告知チャンネルを作成するカテゴリー名
ANNOUNCE_CHANNEL_NAME = os.environ.get("ANNOUNCE_CHANNEL", "一般")  # @everyone告知を投稿するチャンネル名

# insane_scores.csv を読み込み、表示用ラベル列を追加
insane_scores = pd.read_csv('insane_scores.csv')
insane_scores["label"] = insane_scores.apply(
    lambda row: f"★{row['level']} {row['title']}", axis=1
)

# Bot の初期化
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# ============================================================
# 認証・Google Sheets ユーティリティ
# ============================================================

def _create_async_creds() -> Credentials:
    """gspread_asyncio 用の非同期認証情報を生成して返す。"""
    sa_info = json.loads(os.environ["GCP_SA_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    return Credentials.from_service_account_info(sa_info, scopes=scopes)


def _get_or_create_ws(sh, title: str, rows: int, cols: int):
    """指定タイトルのワークシートを取得し、存在しなければ新規作成して返す。"""
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=max(6, cols))


def _open_or_create_ws_by_name(spreadsheet_id: str, ws_title: str):
    """
    スプレッドシートIDとタブ名を指定してワークシートを開く。
    存在しない場合は CourseData 用ヘッダーで新規作成する。
    """
    HEADERS = ["回", "diff", "title", "CourseID"]
    gc = _authorize_gc()
    sh = gc.open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet(ws_title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=ws_title, rows=100, cols=len(HEADERS))
        ws.update("A1", [HEADERS])
    return ws

# ============================================================
# データ操作ユーティリティ
# ============================================================

def save_json(path: str, data) -> None:
    """データを JSON ファイルに保存する。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def extract_lr2id_from_bytes(content: bytes) -> int:
    """バイト列のコースファイルから COURSEID を抽出して返す。"""
    for line in content.decode(errors="ignore").splitlines():
        if line.startswith("#COURSEID"):
            return int(line.strip().split()[1])
    raise ValueError("COURSEIDが見つかりませんでした")


def format_difficulty(diff) -> str:
    """
    難易度を "★N" 形式にフォーマットして返す。
    すでに "★" で始まる文字列はそのまま返す。
    """
    if isinstance(diff, str) and diff.startswith("★"):
        return diff
    try:
        return f"★{int(diff)}"
    except (ValueError, TypeError):
        return str(diff)


def _parse_score(score_text: str):
    """
    "aaaa/bbbb(cc.cc%)" 形式のスコア文字列を解析する。
    戻り値: (自スコア: int, スコアレート: float)
    パース失敗時は (None, None) を返す。
    """
    SCORE_RE = re.compile(
        r'(?P<a>\d[\d,]*)\s*/\s*(?P<b>\d[\d,]*)\s*\(\s*(?P<p>[\d.]+)\s*%\s*\)'
    )
    if not score_text:
        return None, None
    m = SCORE_RE.search(str(score_text))
    if not m:
        return None, None
    own = int(m.group("a").replace(",", ""))
    rate = float(m.group("p"))
    return own, rate

# ============================================================
# BPI 計算
# ============================================================

def pgf(x, m):
    """BPI 計算用のスコア変換関数。"""
    if x == 1:
        return m
    return 0.5 / (1 - x)


def calculate_bpi(s, k, z, m, p) -> float:
    """
    単曲 BPI を計算して返す。
    s: 自スコア, k: 平均スコア, z: トップスコア, m: 理論値, p: 補正係数
    """
    S = pgf(s / m, m)
    K = pgf(k / m, m)
    Z = pgf(z / m, m)
    S_prime = S / K
    Z_prime = Z / K

    if s >= k:
        return float(round(100 * (np.log(S_prime) ** p) / (np.log(Z_prime) ** p), 2))
    else:
        return float(round(
            max(-100 * ((np.abs(np.log(S_prime)) ** p) / (np.log(Z_prime) ** p)), -15), 2
        ))

# ============================================================
# スプレッドシート書き込み
# ============================================================

def upsert_course_row(
    spreadsheet_id: str,
    worksheet_title: str,
    round_no: int,
    diff: str | int,
    title: str,
    course_id: int,
) -> str:
    """
    CourseData タブの指定「回」の行を上書き、存在しなければ末尾に追加する。
    戻り値: "updated" or "inserted"
    """
    HEADERS = ["回", "diff", "title", "CourseID"]
    ws = _open_or_create_ws_by_name(spreadsheet_id, worksheet_title)

    # A列（回）を取得し、ヘッダー行を除外して検索
    col = ws.col_values(1)
    header = col[0] if col else ""
    rows = col[1:] if header == HEADERS[0] else col
    base_row = 2 if header == HEADERS[0] else 1

    try:
        idx = [int(x) for x in rows].index(int(round_no))
        row_num = base_row + idx
        # 既存行を上書き（A:回, B:diff, C:title, D:CourseID）
        ws.update(
            values=[[round_no, diff, title, course_id]],
            range_name=f"A{row_num}:D{row_num}"
        )
        return "updated"
    except ValueError:
        # 対象行が存在しないので末尾に追加
        ws.append_row([round_no, diff, title, course_id], value_input_option="RAW")
        return "inserted"


def write_round_result_to_sheet(
    spreadsheet_id: str,
    round_title: str,
    result_list: list[dict],
) -> None:
    """
    1回分の result_list を指定スプレッドシートの「round_title」タブへ
    A1 起点で一括書き込みする。
    カラム: Rank, LR2ID, PlayerName, Score, Score Rate (%), BPI
    """
    HEADERS = ["Rank", "LR2ID", "PlayerName", "Score", "Score Rate (%)", "BPI"]
    gc = _authorize_gc()
    sh = gc.open_by_key(spreadsheet_id)

    rows = []
    for e in result_list:
        own, rate = _parse_score(e.get("スコア"))
        rows.append([
            e.get("順位"),
            e.get("LR2ID"),
            e.get("プレイヤー"),
            own,   # 自スコア
            rate,  # スコアレート (%)
            e.get("BPI"),
        ])

    values = [HEADERS] + rows
    ws = _get_or_create_ws(sh, round_title, rows=len(values), cols=len(HEADERS))
    ws.update("A1", values, value_input_option="RAW")


def fetch_course_id_by_round_sync(
    spreadsheet_id: str,
    worksheet_title: str,
    round_value: str | int,
) -> int:
    """
    CourseData タブから round_value に対応する CourseID を取得して返す。
    ヘッダーは 'Round' または '回' を許容する。
    見つからない場合は ValueError を送出する。
    """
    gc = _authorize_gc()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(worksheet_title)

    rows = ws.get_all_records()
    target = str(round_value).strip()

    for r in rows:
        # 'Round' または '回' キーで値を取得
        key_round = r.get("Round", r.get("回"))
        if key_round is None:
            continue
        if str(key_round).strip() == target:
            cid = r.get("CourseID")
            if cid is None or str(cid).strip() == "":
                raise ValueError(f"CourseID が空です（Round={target}）")
            return int(str(cid).strip())

    raise ValueError(f"Round={target} が CourseData に見つかりません")

# ============================================================
# Discord ユーティリティ
# ============================================================

async def safe_reply(inter: Interaction, content: str, *, ephemeral: bool = True):
    """Interaction に対して安全に返信する。応答済みの場合は followup を使う。"""
    try:
        if inter.response.is_done():
            await inter.followup.send(content, ephemeral=ephemeral)
        else:
            await inter.response.send_message(content, ephemeral=ephemeral)
    except discord.errors.NotFound:
        # interaction が失効している場合は無視
        pass


async def _safe_defer(interaction: discord.Interaction, ephemeral: bool = True) -> bool:
    """defer を安全に試みる。応答済みまたは失敗した場合は False を返す。"""
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        return True
    except (discord.NotFound, discord.HTTPException):
        return False


async def _safe_send(interaction: discord.Interaction, content: str, ephemeral: bool = True):
    """
    応答状態に応じて send_message / followup.send を使い分けて送信する。
    Interaction が失効している場合はチャンネルへフォールバックする。
    """
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except (discord.NotFound, discord.HTTPException):
        # Interaction 失効時はチャンネルに直接送信
        await interaction.channel.send(content)

# ============================================================
# Bot イベント
# ============================================================

@bot.event
async def on_ready():
    """Bot 起動時にスラッシュコマンドをグローバルに同期する。"""
    print(f"ログインしました: {bot.user}")
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"コマンド同期エラー: {e}")

# ============================================================
# /announce コマンド（管理者専用）
# ============================================================

class AnnounceModal(ui.Modal, title="イベントアナウンス"):
    """イベント情報を入力するモーダルフォーム。"""

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

        sheet_id = os.environ.get("MAIN_ID")
        course_ws = "CourseData"
        if not sheet_id:
            await interaction.followup.send("MAIN_ID が未設定です。.env を確認してください。", ephemeral=True)
            return

        # 開催期間の計算（当日23:00 〜 翌週火曜23:59）
        now = datetime.now()
        start = now.replace(hour=23, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=(7 - start.weekday() + 1))).replace(
            hour=23, minute=59, second=59
        )

        # チャンネル名用スラッグを生成
        # ★ はそのまま保持、それ以外の記号はアンダースコアに変換
        def to_slug(text: str) -> str:
            return ''.join(
                c.lower() if c.isalnum() else ('★' if c == '★' else '_')
                for c in text
            ).strip('_')

        slug = (
            f"{self.round.value}_"
            f"{to_slug(format_difficulty(self.difficulty.value))}_"
            f"{to_slug(self.songtitle.value)}"
        )

        # アーカイブカテゴリ名を計算する（例: round=15 → "11-20"）
        def archive_category_name(round_no: int) -> str:
            lower = ((round_no - 1) // 10) * 10 + 1
            return f"{lower}-{lower + 9}"

        # 「開催中のスコアタ」カテゴリーを取得（なければ作成）
        active_category = discord.utils.get(
            interaction.guild.categories, name=SCORETA_CATEGORY_NAME
        )
        if active_category is None:
            active_category = await interaction.guild.create_category(SCORETA_CATEGORY_NAME)

        # 「開催中のスコアタ」内の既存チャンネルをアーカイブカテゴリへ移動
        for ch in list(active_category.channels):
            # チャンネル名の先頭部分から回数を抽出（例: "5_★12_air" → 5）
            try:
                ch_round = int(ch.name.split("_")[0])
            except (ValueError, IndexError):
                continue
            arch_name = archive_category_name(ch_round)
            arch_category = discord.utils.get(interaction.guild.categories, name=arch_name)
            if arch_category is None:
                arch_category = await interaction.guild.create_category(arch_name)
            await ch.edit(category=arch_category)

        # 新チャンネルを「開催中のスコアタ」に作成
        channel = await interaction.guild.create_text_channel(slug, category=active_category)

        # URL または数値から LR2 CourseID を抽出
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

        # スプレッドシートへのアップサート（同期I/Oはスレッドプールで実行）
        loop = interaction.client.loop
        try:
            await loop.run_in_executor(
                None,
                upsert_course_row,
                sheet_id,
                course_ws,
                int(self.round.value),
                self.difficulty.value,
                str(self.songtitle.value),
                int(lr2id_val),
            )
        except Exception as e:
            await interaction.followup.send(
                f"スプレッドシート書き込みに失敗しました。\n```\n{e}\n```",
                ephemeral=True
            )
            return

        # 告知チャンネル（新規作成したチャンネル）にイベント詳細を投稿
        lr2_url = (
            f"http://www.dream-pro.info/~lavalse/LR2IR/search.cgi"
            f"?mode=ranking&courseid={lr2id_val}"
        )
        lr2_course_url = (
            f"http://www.dream-pro.info/~lavalse/LR2IR/search.cgi"
            f"?mode=downloadcourse&courseid={lr2id_val}"
        )
        await channel.send(
            f"# 第{self.round.value}回\n"
            f"**{self.songtitle.value}** ({format_difficulty(self.difficulty.value)})\n"
            f"[コースURL]({lr2_url}) [コースファイルダウンロードはここから]({lr2_course_url})\n"
            f"開催期間: {start.strftime('%Y/%m/%d %H:%M:%S')} ～ {end.strftime('%Y/%m/%d %H:%M:%S')}"
        )

        # 「一般」チャンネルに @everyone メンション付きの告知を投稿
        general_ch = discord.utils.get(
            interaction.guild.text_channels, name=ANNOUNCE_CHANNEL_NAME
        )
        if general_ch:
            await general_ch.send(
                f"@everyone\n"
                f"#{self.round.value} 開催期間:{start.strftime('%Y/%m/%d %H:%M:%S')}～{end.strftime('%Y/%m/%d %H:%M:%S')}\n"
                f"課題曲: {format_difficulty(self.difficulty.value)} {self.songtitle.value}"
            )

        await interaction.followup.send(
            f"{channel.mention} にアナウンスを投稿しました。",
            ephemeral=True
        )


@bot.tree.command(name="announce", description="イベントアナウンス（運営専用）")
async def announce(interaction: Interaction):
    """管理者ロールを持つユーザーのみアナウンスモーダルを開く。"""
    if not any(role.name == ANNOUNCE_ROLE_NAME for role in interaction.user.roles):
        await interaction.response.send_message("このコマンドは運営のみ使用できます。", ephemeral=True)
        return
    await interaction.response.send_modal(AnnounceModal())

# ============================================================
# /result コマンド（管理者専用）
# ============================================================

@bot.tree.command(name="result", description="指定した回のランキングを表示")
@app_commands.describe(event="対象の回数（例: 1）")
async def result(interaction: discord.Interaction, event: str):
    """LR2IR からランキングを取得し、BPI を計算してスプレッドシートへ保存・表示する。"""
    # 1) 3秒タイムアウト対策として最初に defer
    await _safe_defer(interaction, ephemeral=True)

    # 2) 管理者ロールのチェック
    if not any(role.name == ANNOUNCE_ROLE_NAME for role in interaction.user.roles):
        await _safe_send(interaction, "このコマンドは運営のみ使用できます。", ephemeral=True)
        return

    # 3) 環境変数からスプレッドシートIDを取得
    sheet_id = os.getenv("MAIN_ID")
    output_id = os.getenv("SCORE_ID")
    course_ws = "CourseData"
    if not sheet_id:
        await _safe_send(interaction, "MAIN_ID（または SCORE_ID）が未設定です。.env を確認してください。", ephemeral=True)
        return

    # 4) CourseData タブから対象回の CourseID を取得
    try:
        loop = asyncio.get_running_loop()
        course_id = await loop.run_in_executor(
            None,
            fetch_course_id_by_round_sync,
            sheet_id,
            course_ws,
            event
        )
    except Exception as e:
        await _safe_send(interaction, f"CourseData から回 {event} の CourseID を取得できませんでした。\n```\n{e}\n```", ephemeral=True)
        return

    # 5) LR2IR からランキングデータを取得
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

    # 6) LR2IR のランキングページから BMSID を取得して BPI パラメータを引く
    try:
        course_url = (
            f"http://www.dream-pro.info/~lavalse/LR2IR/search.cgi"
            f"?mode=ranking&courseid={course_id}"
        )
        html = requests.get(course_url, timeout=10).text
        m = re.search(r'search\.cgi\?mode=ranking&bmsid=(\d+)', html)
        if not m:
            await _safe_send(interaction, "BMSIDの取得に失敗しました。", ephemeral=True)
            return
        bmsid = int(m.group(1))
    except Exception as e:
        await _safe_send(interaction, f"BMSID取得中にエラー: {e}", ephemeral=True)
        return

    # 7) insane_scores から BPI 計算用パラメータを取得
    score_row = insane_scores[insane_scores['lr2_bmsid'] == bmsid]
    if score_row.empty:
        await _safe_send(interaction, "insane_scoresに該当するBMSIDが見つかりませんでした。", ephemeral=True)
        return

    s_row = score_row.iloc[0]
    m = s_row["theoretical_score"]
    k = s_row["average_score"]
    z = s_row["top_score"]
    p = max(s_row["optimized_p"], 0.8)  # p の下限を 0.8 に設定

    # 8) 各プレイヤーの BPI を算出してリスト化
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
            "BPI": bpi,
        })

    # 9) 結果をスプレッドシートへ書き込み（同期I/Oはスレッドプールで実行）
    try:
        await _safe_send(interaction, "スプレッドシートへ書き込み中…", ephemeral=True)
        await loop.run_in_executor(
            None,
            write_round_result_to_sheet,
            output_id,
            str(event),
            result_list,
        )
    except Exception as e:
        await _safe_send(interaction, f"スプレッドシート書き込みに失敗しました。\n```\n{e}\n```", ephemeral=True)
        return

    # 10) Discord への表示メッセージを生成（メダル表示あり）
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

        # 同順位が続いている間はメダルインデックスを進めない
        if prev_rank is not None and rank != prev_rank:
            medal_idx += count_same_rank
            count_same_rank = 0

        prefix = medals[medal_idx] if medal_idx < len(medals) else f"{rank}位"
        msg += f"{prefix} {name} - {score} - BPI: {bpi}\n"
        prev_rank = rank
        count_same_rank += 1

    await _safe_send(interaction, msg, ephemeral=False)

# ============================================================
# /bpi コマンド
# ============================================================

@bot.tree.command(name="bpi", description="スコアからBPIを計算")
@app_commands.describe(
    song="★難易度と曲名（例: ★16 Born [29Another]）",
    score="あなたのスコア（整数）"
)
async def bpi(interaction: discord.Interaction, song: str, score: int):
    """指定した曲名とスコアから BPI を計算して表示する。"""
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        level_title = song.strip()
        row = insane_scores[insane_scores["label"] == level_title].iloc[0]
    except IndexError:
        await interaction.followup.send("該当する楽曲が見つかりませんでした。", ephemeral=True)
        return

    bpi_value = calculate_bpi(
        s=score,
        k=row["average_score"],
        z=row["top_score"],
        m=row["theoretical_score"],
        p=row["optimized_p"]
    )

    await interaction.followup.send(
        f"**{row['title']} (★{row['level']}) の BPI**\n"
        f"あなたのスコア: {score}\n"
        f"→ **BPI: {bpi_value}**",
        ephemeral=True
    )


@bpi.autocomplete("song")
async def song_autocomplete(interaction: discord.Interaction, current: str):
    """曲名の入力に対して insane_scores からオートコンプリート候補を返す（最大25件）。"""
    filtered = [
        label for label in insane_scores["label"]
        if current.lower() in label.lower()
    ][:25]
    return [app_commands.Choice(name=label, value=label) for label in filtered]

# ============================================================
# LR2Cog（/register・/mypage コマンド）
# ============================================================

class LR2Cog(commands.Cog):
    """LR2ID の登録とマイページ表示を担当する Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # gspread_asyncio のクライアントマネージャーを初期化
        self.agcm = gspread_asyncio.AsyncioGspreadClientManager(_create_async_creds)
        self._agc = None  # 認証済みクライアントのキャッシュ

    async def _get_ws(self):
        """UserData ワークシートを取得する（クライアントをキャッシュして再利用）。"""
        if self._agc is None:
            self._agc = await self.agcm.authorize()
        sh = await self._agc.open(SHEET_NAME)
        ws = await sh.worksheet(WS_USERDATA)
        return ws

    async def _upsert_user(self, discord_id: str, lr2id: str) -> str:
        """
        UserData シートの DiscordID を検索し、存在すれば LR2ID を更新、
        なければ末尾に新規追加する。
        期待ヘッダー: A列=DiscordID, B列=LR2ID
        戻り値: "updated" or "inserted"
        """
        ws = await self._get_ws()

        # A列の DiscordID を一括取得し、ヘッダー行を除外
        col = await ws.col_values(1)
        if col and col[0] == "DiscordID":
            discord_ids = col[1:]
            base_row = 2
        else:
            discord_ids = col
            base_row = 1

        try:
            # 既存DiscordIDが見つかれば B列（LR2ID）を更新
            idx = discord_ids.index(discord_id)
            row_num = base_row + idx
            await ws.update_cell(row_num, 2, lr2id)
            return "updated"
        except ValueError:
            # 存在しない場合は末尾に追記
            await ws.append_row([discord_id, lr2id], value_input_option="RAW")
            return "inserted"

    @app_commands.command(name="register", description="自分のLR2IDを登録")
    @app_commands.describe(lr2id="LR2IRのplayerid")
    async def register(self, interaction: Interaction, lr2id: str):
        """Discord ID と LR2ID を紐づけて UserData シートに保存する。"""
        discord_id = str(interaction.user.id)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self._upsert_user(discord_id, lr2id)
            if result == "updated":
                msg = f"更新しました。(LR2ID を `{lr2id}` に変更)"
            else:
                msg = "新規登録しました。"
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            # 権限・シート名・ネットワーク等のエラー
            await interaction.followup.send(
                f"登録に失敗しました。設定を確認してください。\n```\n{e}\n```",
                ephemeral=True
            )

    @app_commands.command(name="mypage", description="自分の過去のランキングを確認")
    @app_commands.describe(event="対象の回数（例: 1）、または 'all'")
    async def mypage(self, interaction: Interaction, event: str):
        """指定した回（または全回）の自分のランキング結果を表示する。"""
        # UserData の参照先シートを環境変数から取得
        userdata_sheet_id = os.getenv("USERDATA_ID") or os.getenv("MAIN_ID")
        userdata_ws = os.getenv("USERDATA_WS", "UserData")
        if not userdata_sheet_id:
            await interaction.response.send_message("USERDATA_ID（または MAIN_ID）が未設定です。.env を確認してください。", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        # UserData から自分の LR2ID を取得（同期I/Oはスレッドプールで実行）
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
            await interaction.followup.send(
                "先に `/register` でLR2IDを登録してください。（UserDataに見つかりませんでした）",
                ephemeral=True
            )
            return

        # 各スプレッドシートIDとタブ名を環境変数から取得
        main_sheet_id = os.getenv("MAIN_ID")
        result_sheet_id = os.getenv("SCORE_ID")
        course_ws = os.getenv("COURSE_WS", "CourseData")

        if not result_sheet_id:
            await interaction.response.send_message("SCORE_ID が未設定です。.env を確認してください。", ephemeral=True)
            return
        if not main_sheet_id:
            await interaction.response.send_message("MAIN_ID が未設定です（曲名・難易度の表示に必要）。", ephemeral=True)
            return

        await safe_defer(interaction, ephemeral=True)
        loop = asyncio.get_running_loop()

        # CourseData をロードしてメタ情報マップ（回 → {title, diff}）を構築
        try:
            meta_map = await loop.run_in_executor(
                None, load_course_meta_map_sync, main_sheet_id, course_ws
            )
        except Exception as e:
            await interaction.followup.send(f"CourseData 取得に失敗しました。\n```\n{e}\n```", ephemeral=True)
            return

        # ---- all モード: 全回の成績を HTML テーブルで送信 ----
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
                diff = meta.get("diff", "")

                # 上位3位はカラーハイライト付きで表示
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
            token = store_page(html)
            base_url = os.getenv("WEB_BASE_URL", f"http://localhost:{os.getenv('WEB_PORT', '8080')}")
            url = f"{base_url}/mypage/{token}"
            await interaction.followup.send(
                content=f"マイページを生成しました（有効期限: 24時間）\n{url}",
                ephemeral=True
            )
            return

        # ---- 単一回モード: 指定回の成績を Embed で表示 ----
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

        # float 表記（例: "1.0"）にも対応してキーを検索
        meta = meta_map.get(str(int(float(event)))) or meta_map.get(str(event), {"title": "", "diff": ""})
        title = meta.get("title", "")
        diff = meta.get("diff", "")

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

# ============================================================
# Help Cog（/help コマンド）
# ============================================================

class Help(commands.Cog):
    """コマンド一覧を表示する Cog。"""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Botの使い方とコマンド一覧を表示します")
    async def help(self, interaction: Interaction):
        """利用可能なコマンドの一覧を Embed で表示する。"""
        embed = Embed(
            title="IR_Bot ヘルプ",
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
        embed.add_field(
            name="/changelog",
            value="Botの最近の更新情報を表示します。",
            inline=False
        )
        embed.set_footer(text="質問や不具合は運営(ねぶかわ)かbot制作者(ひたらぎ)までどうぞ！")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="changelog", description="Botの最近の更新情報を表示します")
    async def changelog(self, interaction: Interaction):
        """UPDATES.md を読み込んでユーザー向け更新情報を Embed で表示する。"""
        updates_path = os.path.join(os.path.dirname(__file__), "UPDATES.md")
        try:
            with open(updates_path, encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            await interaction.response.send_message("更新情報ファイルが見つかりません。", ephemeral=True)
            return

        # `## ` を区切りにセクションを分割（空セクションは除外）
        sections = [s.strip() for s in content.split("## ") if s.strip()]

        embed = Embed(
            title="IR_Bot 更新情報",
            color=discord.Color.blurple(),
        )

        image_set = False
        for section in sections:
            lines = section.splitlines()
            title = lines[0].strip()   # 1行目がセクション名（日付など）

            # `![](URL)` 形式の画像行を本文から分離し、最初の1枚を Embed 画像に設定
            body_lines = []
            for line in lines[1:]:
                m = re.match(r'!\[.*?\]\((https?://\S+)\)', line.strip())
                if m and not image_set:
                    embed.set_image(url=m.group(1))
                    image_set = True
                else:
                    body_lines.append(line)

            body = "\n".join(body_lines).strip()
            embed.add_field(name=title, value=body or "（内容なし）", inline=False)

        await interaction.response.send_message(embed=embed)

# ============================================================
# Cog のセットアップ・Bot 起動
# ============================================================

@bot.event
async def setup_hook():
    """Bot 起動前に Cog を登録し、マイページ配信用 Web サーバーを起動する。"""
    await bot.add_cog(LR2Cog(bot))
    await bot.add_cog(Help(bot))

    # マイページ配信用 HTTP サーバーを起動
    web_host = os.getenv("WEB_HOST", "0.0.0.0")
    web_port = int(os.getenv("WEB_PORT", "8080"))
    await start_web_server(web_host, web_port)
    logging.getLogger(__name__).info("Web server started on %s:%s", web_host, web_port)


# Bot を起動
bot.run(os.getenv("DISCORD_TOKEN"))
