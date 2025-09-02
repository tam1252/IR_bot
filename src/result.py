# 先頭の import 付近に追加
import os, json, gspread, asyncio
from google.oauth2.service_account import Credentials

def _authorize_gc():
    sa_info = json.loads(os.environ["GCP_SA_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)


def _load_user_rows_sync(sheet_id: str, ws_title: str = "UserData"):
    """UserData タブを [{DiscordID:..., LR2ID:...}, ...] で返す（列名ゆれ吸収）"""
    gc = _authorize_gc()
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(ws_title)
    rows = ws.get_all_records()
    norm = []
    for r in rows:
        # 大文字小文字／日本語のゆれにも一応対応
        def get(d, *keys):
            for k in keys:
                if k in d and d[k] not in ("", None):
                    return d[k]
                lk = str(k).lower()
                for dk, dv in d.items():
                    if str(dk).lower() == lk and dv not in ("", None):
                        return dv
            return None
        discord_id = get(r, "DiscordID", "discord_id", "discordid", "ディスコードID")
        lr2id      = get(r, "LR2ID", "lr2_id", "lr2id")
        if discord_id and lr2id:
            norm.append({"DiscordID": str(discord_id).strip(), "LR2ID": str(lr2id).strip()})
    return norm

async def build_id_to_name_from_sheet(guild) -> dict[str, str]:
    """
    UserData（DiscordID/LR2ID）を読み、{ LR2ID: Discord表示名 } を返す。
    環境変数:
      - USERDATA_ID があれば優先
      - なければ MAIN_ID を利用
      - USERDATA_WS（省略時 "UserData"）
    """
    sheet_id = os.getenv("USERDATA_ID") or os.getenv("MAIN_ID")
    ws_title = os.getenv("USERDATA_WS", "UserData")
    if not sheet_id:
        # 必要なら raise にして上位でハンドリングしてもOK
        return {}

    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _load_user_rows_sync, sheet_id, ws_title)

    id_to_name: dict[str, str] = {}
    for row in rows:
        did = row["DiscordID"]
        lr2 = row["LR2ID"]
        # Guild キャッシュを優先（API呼び出し回数節約）
        member = guild.get_member(int(did))
        if member is None:
            try:
                member = await guild.fetch_member(int(did))
            except Exception:
                continue
        id_to_name[lr2] = member.display_name
    return id_to_name