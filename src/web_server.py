# ============================================================
# web_server.py - マイページ配信用 Web サーバー
# aiohttp を使って生成済み HTML をインメモリで保持し、
# ユニークな URL でブラウザ閲覧できるようにする
# ============================================================

import time
import uuid

from aiohttp import web

# ============================================================
# インメモリ ページストア
# ============================================================

# token → (html, expiry_timestamp)
_pages: dict[str, tuple[str, float]] = {}

# ページの有効期限（秒）。デフォルト 24 時間
PAGE_TTL: float = 60 * 60 * 24


def store_page(html: str) -> str:
    """
    HTML 文字列をインメモリに保存し、アクセス用トークンを返す。
    TTL 切れのページは次のアクセス時に自動削除される。
    """
    # 古いページを掃除
    now = time.time()
    expired = [t for t, (_, exp) in _pages.items() if now > exp]
    for t in expired:
        del _pages[t]

    token = uuid.uuid4().hex
    _pages[token] = (html, now + PAGE_TTL)
    return token


# ============================================================
# aiohttp ルートハンドラー
# ============================================================

async def _handle_page(request: web.Request) -> web.Response:
    """
    GET /mypage/{token} — 保存済み HTML を返す。
    トークンが存在しない、または有効期限切れの場合は 404 を返す。
    """
    token = request.match_info["token"]
    entry = _pages.get(token)
    if entry is None:
        raise web.HTTPNotFound(text="このページは存在しないか、有効期限切れです。")
    html, expiry = entry
    if time.time() > expiry:
        del _pages[token]
        raise web.HTTPNotFound(text="このページは有効期限切れです。再度コマンドを実行してください。")
    return web.Response(text=html, content_type="text/html", charset="utf-8")


# ============================================================
# サーバー起動
# ============================================================

async def start_web_server(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """
    aiohttp サーバーを起動して AppRunner を返す。
    bot の setup_hook から呼び出す想定。
    環境変数:
      - WEB_HOST: バインドアドレス（デフォルト: 0.0.0.0）
      - WEB_PORT: ポート番号（デフォルト: 8080）
    """
    app = web.Application()
    app.router.add_get("/mypage/{token}", _handle_page)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
