# ============================================================
# common.py - プロジェクト共通ユーティリティ
# Google Sheets 認証・Discord Interaction ヘルパーを提供する
# ============================================================

import os
import json

import discord
import gspread
from google.oauth2.service_account import Credentials


def _authorize_gc() -> gspread.Client:
    """
    環境変数 GCP_SA_JSON のサービスアカウント情報で gspread クライアントを生成して返す。
    Sheets / Drive スコープを付与する。
    """
    sa_info = json.loads(os.environ["GCP_SA_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)


async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = True) -> None:
    """
    Interaction の defer を安全に呼び出す。
    すでに応答済みの場合は何もしない。
    """
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        except discord.InteractionResponded:
            pass
