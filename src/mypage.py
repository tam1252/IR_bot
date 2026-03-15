# ============================================================
# mypage.py - マイページ・ユーザーデータ参照ロジック
# Google Sheets からユーザーの成績・LR2ID を取得する同期関数を提供する
# ============================================================

import asyncio

import gspread

from src.common import _authorize_gc


# ============================================================
# 内部ユーティリティ
# ============================================================

def _get_value_fuzzy(d: dict, *keys) -> object:
    """
    列名のゆれ（大文字小文字・日本語）を許容して辞書から値を取得する。
    キーをそのまま検索し、見つからなければ小文字化して再検索する。
    """
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] not in (None, ""):
            return lower[lk]
    return None


def _norm_round(v) -> str:
    """回数を正規化して文字列で返す（例: 1 / '1' / '1.0' → '1'）。"""
    s = str(v).strip()
    try:
        return str(int(float(s)))
    except (ValueError, TypeError):
        return s

# ============================================================
# CourseData 読み込み
# ============================================================

def _get_course_meta_sync(
    main_sheet_id: str,
    round_value: str | int,
    ws_title: str = "CourseData",
) -> dict | None:
    """
    CourseData タブから指定の回（Round または 回）の {title, diff} を返す。
    見つからない場合は None を返す。
    """
    gc = _authorize_gc()
    sh = gc.open_by_key(main_sheet_id)
    ws = sh.worksheet(ws_title)
    rows = ws.get_all_records()
    target = str(round_value).strip()
    for r in rows:
        rv = r.get("Round", r.get("回"))
        if rv is not None and str(rv).strip() == target:
            return {
                "title": r.get("title", r.get("曲名") or ""),
                "diff":  r.get("diff",  r.get("難易度") or ""),
            }
    return None


def load_course_meta_map_sync(
    main_sheet_id: str,
    ws_title: str = "CourseData",
) -> dict[str, dict]:
    """
    CourseData タブを読み込み、{ '1': {'title': '...', 'diff': '...'}, ... } を返す。
    許容ヘッダー: Round/回, title/曲名, diff/難易度
    """
    gc = _authorize_gc()
    sh = gc.open_by_key(main_sheet_id)
    ws = sh.worksheet(ws_title)
    rows = ws.get_all_records()

    meta = {}
    for r in rows:
        rnd = _get_value_fuzzy(r, "Round", "回")
        if rnd in (None, ""):
            continue
        key = _norm_round(rnd)
        title = _get_value_fuzzy(r, "title", "曲名", "Title")
        diff  = _get_value_fuzzy(r, "diff", "難易度", "Diff")
        meta[key] = {"title": title or "", "diff": diff or ""}
    return meta

# ============================================================
# 結果シート読み込み
# ============================================================

def _fetch_round_worksheet_sync(
    result_sheet_id: str,
    round_value: str | int,
) -> gspread.Worksheet | None:
    """
    NebukawaIR(result) から指定の回のワークシートを返す。
    タブが存在しない場合は None を返す。
    """
    gc = _authorize_gc()
    sh = gc.open_by_key(result_sheet_id)
    title = str(round_value).strip()
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return None


def _fetch_user_record_one_round_sync(
    result_sheet_id: str,
    round_value: str | int,
    lr2id: str,
) -> tuple[dict | None, int]:
    """
    指定の回のタブから LR2ID が一致する1行を返す。
    戻り値: (行データ dict または None, 総参加人数)
    期待カラム: Rank, LR2ID, PlayerName, Score, Score Rate (%), BPI
    """
    ws = _fetch_round_worksheet_sync(result_sheet_id, round_value)
    if ws is None:
        return None, 0
    rows = ws.get_all_records()
    total = len(rows)
    for r in rows:
        if str(r.get("LR2ID")).strip() == str(lr2id).strip():
            return r, total
    return None, total


def _fetch_user_records_all_rounds_sync(
    result_sheet_id: str,
    lr2id: str,
) -> list[dict]:
    """
    NebukawaIR(result) の全タブを走査し、ユーザーの全記録を返す。
    タブ名が数字のもの（回ごとのシート）のみ対象とする。
    戻り値: [{'round': int, 'row': dict, 'total': int}, ...]
    """
    gc = _authorize_gc()
    sh = gc.open_by_key(result_sheet_id)
    results = []
    for ws in sh.worksheets():
        title = ws.title.strip()
        # タブ名が数字でないもの（例: CourseData）はスキップ
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

# ============================================================
# UserData 読み込み
# ============================================================

def _get_lr2id_by_discord_sync(
    sheet_id: str,
    ws_title: str,
    discord_id: str,
) -> str | None:
    """
    UserData タブから DiscordID に対応する LR2ID を返す。
    見つからない場合は None を返す。
    許容列名: DiscordID / discord_id / ディスコードID, LR2ID / lr2_id / lr2id
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
