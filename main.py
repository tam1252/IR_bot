import interactions
import discord
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from interactions.ext.wait_for import wait_for, setup
import os
from data import song_list, max_score

Token = os.environ["DISCORD_BOT_TOKEN"]
scope = [
  'https://spreadsheets.google.com/feeds',
  'https://www.googleapis.com/auth/drive'
]
credentials = ServiceAccountCredentials.from_json_keyfile_name(
  'gspread_sheet.json', scope)
gc = gspread.authorize(credentials)
SPREAD_SHEET_KEY = os.environ["SPREAD_SHEET_KEY"]
workbook = gc.open_by_key(SPREAD_SHEET_KEY)

bot = interactions.Client(token=Token, intents=interactions.Intents.ALL)
setup(bot)


@bot.command(
  name="test",
  description="Test Command",
)
async def test(ctx: interactions.CommandContext):
  await ctx.send("コマンドが実行されました")


@bot.command(
  name="ir",  #コマンド名
  description="IRのスコアを提出します",
  options=[
    interactions.Option(
      type=interactions.OptionType.INTEGER,
      name="song_title",
      description="曲名を選んでください",
      required=True,
      choices = [interactions.Choice(name=song_list[i], value=i) for i in [1, 2, 3, 4, 5]]
    ),
    interactions.Option(
      type=interactions.OptionType.INTEGER,
      name="score",
      description="スコアを入力してください",
      required=True,
    ),
    interactions.Option(
      name="result",
      description="リザルト画像を添付してください",
      type=interactions.OptionType.ATTACHMENT,  #ファイル添付を行うオプション
      required=True,
    ),
  ])
async def ir(ctx, song_title: int, score: int, result):
  embed = discord.Embed(title="IR Submitted!!",
                        description="Your score added to scoresheet.",
                        color=0xffff00)
  
  embed.add_field(name="Song", value=song_list[song_title])
  embed.add_field(
    name="Score",
    value=
    f"{score}/{max_score[song_title]}({round(100*score/max_score[song_title], 2)}%)"
  )

  #embed.add_field(name="Score Rate", value=f"")
  worksheet_list = workbook.worksheets()
  worksheet_list[0].update_cell(4 + song_title,
                                1 + 4 * player_list[str(ctx.user)], str(score))
  worksheet_list[0].update_cell(4 + song_title,
                                4 + 4 * player_list[str(ctx.user)],
                                str(result.url))
  embed.add_field(name="Total Score",
                  value=str(worksheet_list[0].acell(pic_cell1[str(ctx.user)]).value))
  embed.add_field(
    name="順位",
    value=str(worksheet_list[0].acell(pic_cell2[str(ctx.user)]).value) + "位")
  embed.set_image(url=str(result.url))
  print(embed)
  await ctx.send(
    embeds=[interactions.Embed(**embed.to_dict())],  #embedの変換
  )

pic_cell1 = {
  "ひたらぎ": "E11",
  "egpt": "I11",
  "strngi": "M11",
  "きょん": "Q11",
  "STYU--": "U11"
}
pic_cell2 = {
  "ひたらぎ": "E13",
  "egpt": "I13",
  "strngi": "M13",
  "きょん": "Q13",
  "STYU--": "U13"
}
player_list = {"ひたらぎ": 1, "egpt": 2, "strngi": 3, "きょん": 4, "STYU--": 5}



bot.start()
