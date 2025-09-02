# 先頭の import 付近に追加
import os, json, gspread, asyncio
from google.oauth2.service_account import Credentials
from pandas import DataFrame

def _authorize_gc():
    sa_info = json.loads(os.environ["GCP_SA_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)

def _get_course_meta_sync(main_sheet_id: str, round_value: str | int, ws_title: str = "CourseData"):
    """NebukawaIR の CourseData から Round（or 回）で {title, diff} を取る。見つからなければ None."""
    gc = _authorize_gc()
    sh = gc.open_by_key(main_sheet_id)
    ws = sh.worksheet(ws_title)
    rows = ws.get_all_records()  # list[dict]
    target = str(round_value).strip()
    for r in rows:
        rv = r.get("Round", r.get("回"))
        if rv is not None and str(rv).strip() == target:
            return {
                "title": r.get("title", r.get("曲名") or ""),
                "diff":  r.get("diff",  r.get("難易度") or ""),
            }
    return None

def _fetch_round_worksheet_sync(result_sheet_id: str, round_value: str | int):
    """NebukawaIR(result) から 該当回のワークシートを返す（無ければ None）"""
    gc = _authorize_gc()
    sh = gc.open_by_key(result_sheet_id)
    title = str(round_value).strip()
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return None

def _fetch_user_record_one_round_sync(result_sheet_id: str, round_value: str | int, lr2id: str):
    """
    該当回タブから、LR2ID 一致の1行を dict で返す。無ければ None。
    期待カラム: Rank, LR2ID, PlayerName, Score, Score Rate (%), BPI
    """
    ws = _fetch_round_worksheet_sync(result_sheet_id, round_value)
    if ws is None:
        return None, 0  # df人数0
    rows = ws.get_all_records()  # list[dict]
    total = len(rows)
    for r in rows:
        if str(r.get("LR2ID")).strip() == str(lr2id).strip():
            return r, total
    return None, total

def _fetch_user_records_all_rounds_sync(result_sheet_id: str, lr2id: str):
    """
    NebukawaIR(result) のすべてのタブを走査し、ユーザーの全記録を {round:int, row:dict, total:int} のリストで返す。
    """
    gc = _authorize_gc()
    sh = gc.open_by_key(result_sheet_id)
    results = []
    for ws in sh.worksheets():
        # タブ名が数字でないもの（CourseDataなど）はスキップ
        title = ws.title.strip()
        if not title.isdigit():
            continue
        round_no = int(title)
        rows = ws.get_all_records()
        total = len(rows)
        for r in rows:
            if str(r.get("LR2ID")).strip() == str(lr2id).strip():
                results.append({"round": round_no, "row": r, "total": total})
                break
    return results

def _norm_round(v) -> str:
    """1, '1', '1.0' → '1' に正規化"""
    s = str(v).strip()
    try:
        return str(int(float(s)))
    except Exception:
        return s  # 数字でなければそのまま

def _get_any(d: dict, keys: list[str]):
    """大小文字/日本語を許容して値を取得"""
    # まずそのまま
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    # 小文字キーでも探索
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] not in (None, ""):
            return lower[lk]
    return None

def load_course_meta_map_sync(main_sheet_id: str, ws_title: str = "CourseData") -> dict[str, dict]:
    """
    CourseData を読み込み、{ '1': {'title': '...', 'diff': '...'}, ... } を返す
    許容ヘッダ: Round/回, title/曲名, diff/難易度
    """
    gc = _authorize_gc()
    sh = gc.open_by_key(main_sheet_id)
    ws = sh.worksheet(ws_title)
    rows = ws.get_all_records()  # list[dict]

    meta = {}
    for r in rows:
        rnd = _get_any(r, ["Round", "回"])
        if rnd in (None, ""):
            continue
        key = _norm_round(rnd)
        title = _get_any(r, ["title", "曲名", "Title"])
        diff  = _get_any(r, ["diff", "難易度", "Diff"])
        meta[key] = {"title": title or "", "diff": diff or ""}
    return meta

def _get_value_fuzzy(d: dict, *keys):
    """列名のゆれ（小文字/日本語）を許容して値を取る"""
    # そのまま
    for k in keys:
        if k in d and d[k] not in ("", None):
            return d[k]
    # 小文字で照合
    lower = {str(k).lower(): v for k,v in d.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] not in ("", None):
            return lower[lk]
    return None

def _get_lr2id_by_discord_sync(sheet_id: str, ws_title: str, discord_id: str):
    """
    UserData タブから DiscordID=discord_id の LR2ID を返す（見つからなければ None）
    許容列名:
      - DiscordID / discord_id / ディスコードID
      - LR2ID / lr2_id / lr2id
    """
    gc = _authorize_gc()
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(ws_title)
    rows = ws.get_all_records()
    target = str(discord_id).strip()
    for r in rows:
        did = _get_value_fuzzy(r, "DiscordID", "discord_id", "discordid", "ディスコードID")
        if did is None:
            continue
        if str(did).strip() == target:
            lr2 = _get_value_fuzzy(r, "LR2ID", "lr2_id", "lr2id")
            return str(lr2).strip() if lr2 not in ("", None) else None
    return None