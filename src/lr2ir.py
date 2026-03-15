# ============================================================
# lr2ir.py - LR2IR ランキング取得モジュール
# LR2IR のランキングページをスクレイピングして DataFrame で返す
# ============================================================

from io import StringIO

import pandas as pd
import requests
from bs4 import BeautifulSoup


# LR2IR ランキングページのベース URL
BASE_URL = 'http://www.dream-pro.info/~lavalse/LR2IR/search.cgi?mode=ranking&courseid='


def fetch_lr2_ranking(course_id: int) -> pd.DataFrame:
    """
    指定した CourseID の LR2IR ランキングを取得して DataFrame で返す。
    取得に失敗した場合は空の DataFrame を返す。
    カラム: 順位, LR2ID, プレイヤー, スコア, PG, GR
    """
    url = f'{BASE_URL}{course_id}'

    try:
        res = requests.get(url)
        res.encoding = 'cp932'  # LR2IR は Shift-JIS 系エンコーディング
        soup = BeautifulSoup(res.text, 'html.parser')
        tables = soup.find_all('table')

        if len(tables) < 4:
            raise ValueError("テーブルの数が予期より少ないためデータ取得に失敗しました。")

        # ランキングテーブルは4番目のテーブル（0-indexed で index=3）
        target_table = tables[3]
        df = pd.read_html(StringIO(str(target_table)))[0]

        # プレイヤーのリンクタグから LR2ID（playerid）を抽出
        player_links = []
        for row in target_table.find_all('tr')[1:]:  # ヘッダー行を除外
            cols = row.find_all('td')
            if len(cols) >= 2:
                a_tag = cols[1].find('a')
                if a_tag and a_tag.get('href') and "playerid=" in a_tag['href']:
                    player_links.append(a_tag['href'].split("playerid=")[1])
                else:
                    player_links.append(None)
            else:
                player_links.append(None)

        df['LR2ID'] = player_links

        # スコアが空の不正行を除外し、必要なカラムのみに絞る
        df = df.dropna(subset=[df.columns[3]])[['順位', 'LR2ID', 'プレイヤー', 'スコア', 'PG', 'GR']]
        df = df.reset_index(drop=True)
        return df

    except Exception as e:
        print(f"ランキング取得中にエラーが発生しました: {e}")
        return pd.DataFrame()
