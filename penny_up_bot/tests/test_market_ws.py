"""market_ws 消息分发测试 —— 把 ws 'book'/'price_change' 消息正确落到对应 BookState。"""
from penny_up_bot.book_state import BookState
from penny_up_bot.market_ws import apply_market_message


def make_books(*tids):
    return {t: BookState() for t in tids}


def test_book_snapshot_applied():
    books = make_books("tokA")
    msg = {
        "event_type": "book",
        "asset_id": "tokA",
        "bids": [{"price": "0.60", "size": "100"}],
        "asks": [{"price": "0.62", "size": "50"}],
    }
    assert apply_market_message(books, msg) == "tokA"
    assert books["tokA"].best_competing_bid(None, 0.0) == 0.60


def test_price_change_applied():
    books = make_books("tokA")
    books["tokA"].apply_snapshot([{"price": "0.60", "size": "100"}], [])
    msg = {
        "event_type": "price_change",
        "asset_id": "tokA",
        "price_changes": [{"side": "BUY", "price": "0.61", "size": "30"}],
    }
    assert apply_market_message(books, msg) == "tokA"
    assert books["tokA"].best_competing_bid(None, 0.0) == 0.61


def test_unknown_token_ignored():
    books = make_books("tokA")
    msg = {"event_type": "book", "asset_id": "OTHER", "bids": [], "asks": []}
    assert apply_market_message(books, msg) is None


def test_unknown_event_ignored():
    books = make_books("tokA")
    assert apply_market_message(books, {"event_type": "tick_size_change", "asset_id": "tokA"}) is None


def test_legacy_changes_key_supported():
    books = make_books("tokA")
    books["tokA"].apply_snapshot([{"price": "0.60", "size": "100"}], [])
    msg = {
        "event_type": "price_change",
        "asset_id": "tokA",
        "changes": [{"side": "BUY", "price": "0.55", "size": "0"}],  # 删除 0.55 档（本就没有）
    }
    assert apply_market_message(books, msg) == "tokA"
    assert books["tokA"].best_competing_bid(None, 0.0) == 0.60
