FROM python:3.13-slim

# 作業ディレクトリを設定
WORKDIR /app

# 依存関係ファイルをコピー
COPY requirements.txt .

# 必要なパッケージをインストール
RUN pip install --no-cache-dir -r requirements.txt

# アプリの全コードをコピー
COPY . .

# main.py を実行
CMD ["python", "main.py"]