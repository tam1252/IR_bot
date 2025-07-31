import discord
from discord import app_commands, ui, Interaction
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import json
from src import lr2ir  # あなたの fetch_lr2_ranking モジュール

# === 設定 ===
CHANNEL_ID = 123456789012345678  # ← 投稿チャンネルIDに置換
ANNOUNCE_ROLE_NAME = "運営"
LR2ID_DB_FILE = "lr2_users.json"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# === ユーザー保存データ ===
def load_user_map():
    try:
        with open(LR2ID_DB_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_user_map(data):
    with open(LR2ID_DB_FILE, "w") as f:
        json.dump(data, f)

user_lr2_map = load_user_map()

# === 定期投稿タスク ===
@bot.event
async def on_ready():
    print(f'ログインしました: {bot.user}')
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"コマンド同期エラー: {e}")
    weekly_post.start()

@tasks.loop(weeks=1)
async def weekly_post():
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        df = lr2ir.fetch_lr2_ranking(13136)
        if df.empty:
            await channel.send("ランキング取得に失敗しました。")
            return
        top = df.head(5).to_string(index=False)
        await channel.send(f"**LR2ランキング（13136）TOP5**\n```\n{top}\n```")

# === アナウンス用モーダル ===
class AnnounceModal(ui.Modal, title="イベントアナウンス"):
    difficulty = ui.TextInput(label="難易度", required=True)
    title = ui.TextInput(label="曲名", required=True)
    ranking_url = ui.TextInput(label="ランキングURL", required=True)

    async def on_submit(self, interaction: Interaction):
        now = datetime.now()
        start = now.replace(hour=23, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=(7 - start.weekday() + 1))
        end = end.replace(hour=23, minute=59, second=59)
        msg = (
            f"# 開催期間: {start.strftime('%Y/%m/%d %H:%M:%S')}～{end.strftime('%Y/%m/%d %H:%M:%S')}\n"
            f"ランキング: {self.ranking_url}\n"
            f"コースファイルは添付のものを読み込ませてください"
        )
        await interaction.response.send_message(msg, ephemeral=False)

@bot.tree.command(name="announce", description="イベントアナウンス（運営専用）")
async def announce(interaction: Interaction):
    if not any(role.name == ANNOUNCE_ROLE_NAME for role in interaction.user.roles):
        await interaction.response.send_message("このコマンドは運営のみ使用できます。", ephemeral=True)
        return
    await interaction.response.send_modal(AnnounceModal())

# === /register, /mypage 機能 ===
class LR2Cog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="register", description="自分のLR2IDを登録")
    @app_commands.describe(lr2id="LR2IRのplayerid")
    async def register(self, interaction: Interaction, lr2id: str):
        user_lr2_map[str(interaction.user.id)] = lr2id
        save_user_map(user_lr2_map)
        await interaction.response.send_message(f"LR2ID `{lr2id}` を登録しました。", ephemeral=True)

    @app_commands.command(name="mypage", description="自分の過去のランキングを確認")
    @app_commands.describe(course_id="対象のコースID（例: 13136）")
    async def mypage(self, interaction: Interaction, course_id: int):
        user_id = str(interaction.user.id)
        if user_id not in user_lr2_map:
            await interaction.response.send_message("先に `/register` でLR2IDを登録してください。", ephemeral=True)
            return
        lr2id = user_lr2_map[user_id]
        df = lr2ir.fetch_lr2_ranking(course_id)
        if df.empty:
            await interaction.response.send_message("ランキング取得に失敗しました。", ephemeral=True)
            return
        record = df[df['LR2ID'] == lr2id]
        if record.empty:
            await interaction.response.send_message("該当コースでの記録が見つかりませんでした。", ephemeral=True)
            return
        row = record.iloc[0]
        msg = (
            f"**{interaction.user.display_name} さんの結果（コースID: {course_id}）**\n"
            f"順位: {row['順位']}位\nスコア: {row['スコア']}\nPG: {row['PG']}, GR: {row['GR']}"
        )
        await interaction.response.send_message(msg)

@bot.event
async def setup_hook():
    await bot.add_cog(LR2Cog(bot))

# === 起動 ===
bot.run("YOUR_DISCORD_BOT_TOKEN")
