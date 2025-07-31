import os
import json
import asyncio
from io import StringIO, BytesIO
from datetime import datetime, timedelta

import discord
from discord import app_commands, ui, Interaction, Embed
from discord.ext import commands
from dotenv import load_dotenv
from pandas import DataFrame

from src import lr2ir  # fetch_lr2_ranking を含む自作モジュール

load_dotenv()

# === 設定 ===
CHANNEL_ID = 123456789012345678  # ← 投稿チャンネルIDに置換
COURSE_JSON_PATH = "course_id.json"
LR2ID_DB_FILE = "lr2_users.json"
ANNOUNCE_ROLE_NAME = "運営"

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
      label="LR2ID または URL",
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

        await channel.send(
            f"# 第{self.round.value}回\n"
            f"**{self.songtitle.value}** ({self.difficulty.value})\n"
            f"[LR2ID: {lr2id}]({lr2_url})\n"
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
            for round_str, course_info in course_map.items():
                df = lr2ir.fetch_lr2_ranking(course_info["LR2ID"])
                record = df[df["LR2ID"] == lr2id]
                if not record.empty:
                    row = record.iloc[0]
                    rank = int(row["順位"])
                    total = len(df)
                    color_map = {1: "gold", 2: "silver", 3: "#cd7f32"}
                    rank_str = f'<span style="color:{color_map.get(rank, 'black')}; font-weight:bold;">{rank}位</span>' if rank <= 3 else f"{rank}位"
                    combined.append({
                        "回": int(round_str),
                        "曲名": course_info["title"],
                        "難易度": format_difficulty(course_info["diff"]),
                        "順位": f"{rank_str} / {total}人",
                        "スコア": row["スコア"]
                    })

            if not combined:
                await interaction.followup.send("記録が見つかりませんでした。", ephemeral=True)
                return

            result_df = DataFrame(combined).sort_values("回")
            html = generate_bootstrap_html_table(result_df, "あなたのねぶかわウィークリー成績一覧")
            await interaction.followup.send(
                content="HTMLを送信します。",
                file=discord.File(BytesIO(html.encode()), filename="mypage_all.html"),
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

@bot.event
async def setup_hook():
    await bot.add_cog(LR2Cog(bot))

# === 起動 ===
bot.run(os.getenv("DISCORD_TOKEN"))
