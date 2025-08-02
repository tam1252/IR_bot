# lr2ranking.py
import requests
from bs4 import BeautifulSoup
import pandas as pd
from io import StringIO

BASE_URL = 'http://www.dream-pro.info/~lavalse/LR2IR/search.cgi?mode=ranking&courseid='

def fetch_lr2_ranking(course_id: int) -> pd.DataFrame:
    url = f'{BASE_URL}{course_id}'

    try:
        res = requests.get(url)
        res.encoding = 'cp932'
        soup = BeautifulSoup(res.text, 'html.parser')
        tables = soup.find_all('table')

        if len(tables) < 4:
            raise ValueError("テーブルの数が予期より少ないためデータ取得に失敗しました。")

        target_table = tables[3]
        df = pd.read_html(StringIO(str(target_table)))[0]

        # プレイヤーリンクからIDを抽出
        player_links = []
        for row in target_table.find_all('tr')[1:]:  # ヘッダー除外
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

        # 不正な行を除外し、必要なカラムだけにする
        df = df.dropna(subset=[df.columns[3]])[['順位', 'LR2ID', 'プレイヤー', 'スコア', 'PG', 'GR']]
        df = df.reset_index(drop=True)
        print(df)
        return df

    except Exception as e:
        print(f"エラーが発生しました: {e}")
        return pd.DataFrame()  # 空のDataFrameを返す
