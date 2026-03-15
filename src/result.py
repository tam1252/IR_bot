# ============================================================
# result.py - ランキング表示用ユーティリティ
# Discord サーバーのメンバー情報と UserData を紐づけて
# LR2ID → Discord 表示名の辞書を構築する
# ============================================================

import os
import asyncio

from src.common import _authorize_gc


def _load_user_rows_sync(
    sheet_id: str,
    ws_title: str = "UserData",
) -> list[dict]:
    """
    UserData タブを読み込み、正規化した [{DiscordID: str, LR2ID: str}, ...] を返す。
    列名のゆれ（大文字小文字・日本語）を許容する。
    """
    gc = _authorize_gc()
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(ws_title)
    rows = ws.get_all_records()

    def get_fuzzy(d: dict, *keys) -> str | None:
        """辞書から列名のゆれを許容して値を取得する。"""
        for k in keys:
            if k in d and d[k] not in ("", None):
                return d[k]
        lower = {str(k).lower(): v for k, v in d.items()}
        for k in keys:
            lk = str(k).lower()
            if lk in lower and lower[lk] not in ("", None):
                return lower[lk]
        return None

    norm = []
    for r in rows:
        discord_id = get_fuzzy(r, "DiscordID", "discord_id", "discordid", "ディスコードID")
        lr2id      = get_fuzzy(r, "LR2ID", "lr2_id", "lr2id")
        if discord_id and lr2id:
            norm.append({
                "DiscordID": str(discord_id).strip(),
                "LR2ID":     str(lr2id).strip(),
            })
    return norm


async def build_id_to_name_from_sheet(guild) -> dict[str, str]:
    """
    UserData を読み込み、{LR2ID: Discord 表示名} の辞書を返す。
    Discord メンバーはギルドキャッシュを優先し、なければ API で取得する。
    環境変数:
      - USERDATA_ID: UserData シートのスプレッドシートID（未設定時は MAIN_ID を使用）
      - USERDATA_WS: UserData タブ名（デフォルト: "UserData"）
    """
    sheet_id = os.getenv("USERDATA_ID") or os.getenv("MAIN_ID")
    ws_title = os.getenv("USERDATA_WS", "UserData")
    if not sheet_id:
        return {}

    # 同期 I/O はスレッドプールで実行
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _load_user_rows_sync, sheet_id, ws_title)

    id_to_name: dict[str, str] = {}
    for row in rows:
        did = row["DiscordID"]
        lr2 = row["LR2ID"]
        # ギルドキャッシュを優先して API 呼び出し回数を節約
        member = guild.get_member(int(did))
        if member is None:
            try:
                member = await guild.fetch_member(int(did))
            except Exception:
                continue
        id_to_name[lr2] = member.display_name
    return id_to_name
