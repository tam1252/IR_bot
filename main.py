import os
import json
import asyncio
from io import StringIO, BytesIO
import re
import requests
import numpy as np
from datetime import datetime, timedelta

import discord
from discord import app_commands, ui, Interaction, Embed
from discord.ext import commands
from dotenv import load_dotenv
from pandas import DataFrame, json_normalize
import pandas as pd
import requests

from src import lr2ir  # fetch_lr2_ranking を含む自作モジュール

load_dotenv()

# === 設定 ===
CHANNEL_ID = 123456789012345678  # ← 投稿チャンネルIDに置換
COURSE_JSON_PATH = "course_id.json"
COURSE_RESULT_FILE = "course_result.json"
LR2ID_DB_FILE = "lr2_users.json"
ANNOUNCE_ROLE_NAME = "管理者"

insane_scores = pd.read_csv('insane_scores.csv')
insane_scores["label"] = insane_scores.apply(lambda row: f"★{row['level']} {row['title']}", axis=1)
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# === 共通ユーティリティ ===
def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

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

def generate_bootstrap_html_table(df, title="LR2IR ランキング一覧"):
    table_html = df.to_html(classes="table table-striped table-bordered", index=False, escape=False)
    return f"""
<!DOCTYPE html>
<html lang=\"ja\">
<head>
  <meta charset=\"UTF-8\">
  <title>{title}</title>
  <link href=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css\" rel=\"stylesheet\">
  <style>
    body {{ margin: 40px; background-color: #f8f9fa; }}
    h1 {{ margin-bottom: 30px; }}
    table {{ font-size: 0.95rem; }}
  </style>
</head>
<body>
  <div class=\"container\">
    <h1>{title}</h1>
    {table_html}
  </div>
</body>
</html>
"""

# === 初期データ読み込み ===
user_lr2_map = load_json(LR2ID_DB_FILE)
course_map = load_json(COURSE_JSON_PATH)

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
        now = datetime.now()
        start = now.replace(hour=23, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=(7 - start.weekday() + 1))
        end = end.replace(hour=23, minute=59, second=59)

        def to_slug(text):
            return ''.join(c.lower() if c.isalnum() else '_' for c in text).strip('_')

        slug = f"{self.round.value}_{to_slug(self.difficulty.value)}_{to_slug(self.songtitle.value)}"
        channel = await interaction.guild.create_text_channel(slug)

        # LR2ID抽出
        lr2id_raw = self.lr2id.value.strip()
        if "courseid=" in lr2id_raw:
            lr2id = int(lr2id_raw.split("courseid=")[1].split("&")[0])
        else:
            try:
                lr2id = int(lr2id_raw)
            except ValueError:
                await interaction.response.send_message("LR2IDが正しくありません。", ephemeral=True)
                return

        # JSON登録
        course_data = load_json(COURSE_JSON_PATH)
        course_data[self.round.value] = {
            "title": self.songtitle.value,
            "diff": self.difficulty.value,
            "LR2ID": lr2id
        }
        save_json(COURSE_JSON_PATH, course_data)

        lr2_url = f"http://www.dream-pro.info/~lavalse/LR2IR/search.cgi?mode=ranking&courseid={lr2id}"
        lr2_course_url = f"http://www.dream-pro.info/~lavalse/LR2IR/search.cgi?mode=downloadcourse&courseid={lr2id}"
        await channel.send(
            f"# 第{self.round.value}回\n"
            f"**{self.songtitle.value}** ({format_difficulty(self.difficulty.value)})\n"
            f"[コースURL]({lr2_url}) [コースファイルダウンロードはここから]({lr2_course_url})\n"
            f"開催期間: {start.strftime('%Y/%m/%d %H:%M:%S')} ～ {end.strftime('%Y/%m/%d %H:%M:%S')}"
        )
        await interaction.response.send_message(f"{channel.mention} にアナウンスを投稿しました。", ephemeral=True)

# === コマンド登録 ===
@bot.tree.command(name="announce", description="イベントアナウンス（運営専用）")
async def announce(interaction: Interaction):
    if not any(role.name == ANNOUNCE_ROLE_NAME for role in interaction.user.roles):
        await interaction.response.send_message("このコマンドは運営のみ使用できます。", ephemeral=True)
        return
    await interaction.response.send_modal(AnnounceModal())

@bot.tree.command(name="upload_course", description="コースファイルをアップロード（運営専用）")
@app_commands.describe(channel="投稿先チャンネル", file="アップロードする .lr2crs ファイル")
async def upload_course(interaction: Interaction, channel: discord.TextChannel, file: discord.Attachment):
    if not any(role.name == ANNOUNCE_ROLE_NAME for role in interaction.user.roles):
        await interaction.response.send_message("このコマンドは運営のみ使用できます。", ephemeral=True)
        return

    await channel.send(content="コースファイルをアップロードしました：", file=await file.to_file())
    await interaction.response.send_message("ファイルをアップロードしました。", ephemeral=True)

#リザルト表示
@bot.tree.command(name="result", description="指定した回のランキングを表示")
@app_commands.describe(event="対象の回数（例: 1）")
async def result(interaction: discord.Interaction, event: str):
    await interaction.response.defer(thinking=True)

    if not any(role.name == ANNOUNCE_ROLE_NAME for role in interaction.user.roles):
        await interaction.followup.send("このコマンドは運営のみ使用できます。", ephemeral=True)
        return

    if event not in course_map:
        await interaction.followup.send("その回のデータは存在しません。", ephemeral=True)
        return

    course_info = course_map[event]
    df = lr2ir.fetch_lr2_ranking(course_info["LR2ID"])
    df = df.dropna()

    if not all(col in df.columns for col in ["順位", "スコア", "LR2ID"]):
        await interaction.followup.send("必要な列が見つかりませんでした。", ephemeral=True)
        return

    player_col = next((col for col in df.columns if "プレイヤー" in col or "名前" in col), None)
    if not player_col:
        await interaction.followup.send("プレイヤー名の列が見つかりませんでした。", ephemeral=True)
        return

    # --- BMSID取得（コースページのHTMLから）
    course_url = f"http://www.dream-pro.info/~lavalse/LR2IR/search.cgi?mode=ranking&courseid={course_info['LR2ID']}"
    html = requests.get(course_url).text
    match = re.search(r'search\.cgi\?mode=ranking&bmsid=(\d+)', html)
    if not match:
        await interaction.followup.send("BMSIDの取得に失敗しました。", ephemeral=True)
        return
    bmsid = int(match.group(1))

    # --- insane_scoresの行を取得
    score_row = insane_scores[insane_scores['lr2_bmsid'] == bmsid]
    if score_row.empty:
        await interaction.followup.send("insane_scoresに該当するBMSIDが見つかりませんでした。", ephemeral=True)
        return

    s_row = score_row.iloc[0]
    m = s_row["theoretical_score"]
    k = s_row["average_score"]
    z = s_row["top_score"]
    p = max(s_row["optimized_p"], 0.8)

    # --- BPI算出とデータ保存用構造構築
    result_list = []
    df = df.sort_values("順位").reset_index(drop=True)
    for _, row in df.iterrows():
        lr2id = str(row["LR2ID"])
        score_str = row["スコア"]
        match = re.match(r"(\d+)/", score_str)
        s = int(match.group(1)) if match else 0
        raw_bpi = calculate_bpi(s, k, z, m, p)
        bpi = round(raw_bpi, 2) if not np.isnan(raw_bpi) else -15

        result_list.append({
            "順位": int(row["順位"]),
            "LR2ID": lr2id,
            "プレイヤー": row[player_col],
            "スコア": score_str,
            "PG": int(row["PG"]),
            "GR": int(row["GR"]),
            "BPI": bpi
        })

    # --- JSON保存（course_result.json）
    course_result = load_json("course_result.json")
    course_result[event] = result_list
    save_json("course_result.json", course_result)

    # --- 表示用メッセージ
    user_map = load_json(LR2ID_DB_FILE)
    id_to_name = {}
    for user_id, lr2id in user_map.items():
        try:
            member = await interaction.guild.fetch_member(int(user_id))
            id_to_name[str(lr2id)] = member.display_name
        except:
            continue

    medals = ["🥇", "🥈", "🥉"]
    msg = f"**第{event}回 ランキング結果**\n"
    current_rank = 1
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

    await interaction.followup.send(msg)

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
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="register", description="自分のLR2IDを登録")
    @app_commands.describe(lr2id="LR2IRのplayerid")
    async def register(self, interaction: Interaction, lr2id: str):
        user_lr2_map[str(interaction.user.id)] = lr2id
        save_json(LR2ID_DB_FILE, user_lr2_map)
        await interaction.response.send_message(f"LR2ID `{lr2id}` を登録しました。", ephemeral=True)

    @app_commands.command(name="mypage", description="自分の過去のランキングを確認")
    @app_commands.describe(event="対象の回数（例: 1）、または 'all'")
    async def mypage(self, interaction: Interaction, event: str):
        user_id = str(interaction.user.id)
        if user_id not in user_lr2_map:
            await interaction.response.send_message("先に `/register` でLR2IDを登録してください。", ephemeral=True)
            return

        lr2id = user_lr2_map[user_id]

        if event.lower() == "all":
            await interaction.response.defer(thinking=True, ephemeral=True)
            combined = []

            # course_result.jsonを読み込み
            result_data = load_json("course_result.json")  # load_json はすでに定義済みのはず

            for round_str, course_info in course_map.items():
                if round_str not in result_data:
                    continue

                df = json_normalize(result_data[round_str])
                record = df[df["LR2ID"] == lr2id]

                if not record.empty:
                    row = record.iloc[0]
                    rank = int(row["順位"])
                    total = len(df)

                    color_map = {1: "gold", 2: "silver", 3: "#cd7f32"}
                    rank_str = (
                        f'<span style="color:{color_map[rank]}; font-weight:bold;">{rank}位</span>'
                        if rank in color_map
                        else f"{rank}位"
                    )

                    combined.append({
                        "回": int(round_str),
                        "曲名": course_info["title"],
                        "難易度": format_difficulty(course_info["diff"]),
                        "順位": f"{rank_str} / {total}人",
                        "スコア": row["スコア"],
                        "BPI": row["BPI"],
                    })

            if not combined:
                await interaction.followup.send("記録が見つかりませんでした。", ephemeral=True)
                return

            result_df = DataFrame(combined).sort_values("回")

            # HTML生成
            html = generate_bootstrap_html_table(result_df, "あなたのねぶかわウィークリー成績一覧")
            html_bytes = BytesIO(html.encode("utf-8"))

            await interaction.followup.send(
                content="あなたの全記録をHTML形式で送信します。",
                file=discord.File(html_bytes, filename="mypage_all.html"),
                ephemeral=True
            )
            return


        if event not in course_map:
            await interaction.response.send_message("指定された回のデータは存在しません。", ephemeral=True)
            return

        df = lr2ir.fetch_lr2_ranking(course_map[event]["LR2ID"])
        record = df[df["LR2ID"] == lr2id]
        if record.empty:
            await interaction.response.send_message(f"第{event}回での記録が見つかりませんでした。", ephemeral=True)
            return

        row = record.iloc[0]
        embed = Embed(
            title=f"第{event}回 ランキング",
            description=f"{course_map[event]['title']}（{format_difficulty(course_map[event]['diff'])}）",
            color=discord.Color.green()
        )
        embed.add_field(name="順位", value=f"{row['順位']} 位", inline=True)
        embed.add_field(name="スコア", value=f"{row['スコア']} ({int(row['PG'])}/{int(row['GR'])})", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


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

        embed.set_footer(text="質問や不具合は運営かbot制作者までどうぞ！")
        await interaction.response.send_message(embed=embed, ephemeral=True)  # ユーザーのみに表示

@bot.event
async def setup_hook():
    await bot.add_cog(LR2Cog(bot))
    await bot.add_cog(Help(bot))

# === 起動 ===
bot.run(os.getenv("DISCORD_TOKEN"))
