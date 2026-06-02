from predictfun_data.normalize import (
    side_to_sdk, side_from_sdk, normalize_order, normalize_orderbook,
    batch_ids, BookLevel, NormalizedBook,
)


def test_side_round_trip():
    assert side_to_sdk("BUY") == 0
    assert side_to_sdk("sell") == 1
    assert side_from_sdk(0) == "BUY"
    assert side_from_sdk(1) == "SELL"


def test_normalize_order_maps_fields():
    raw = {
        "id": "123", "tokenId": "tok", "marketId": 7,
        "side": "Bid", "price": "0.52", "quantity": "100",
        "quantityMatched": "10", "status": "OPEN",
    }
    o = normalize_order(raw)
    assert o["id"] == "123"
    assert o["token_id"] == "tok"
    assert o["market_id"] == 7
    assert o["side"] == "BUY"
    assert o["price"] == 0.52
    assert o["size"] == 100.0
    assert o["size_matched"] == 10.0
    assert o["status"] == "LIVE"


def test_normalize_order_sell_and_nonlive_status():
    o = normalize_order({"id": "1", "side": "Ask", "price": "0.6",
                         "quantity": "5", "status": "CANCELLED"})
    assert o["side"] == "SELL"
    assert o["status"] == "CANCELLED"


def test_normalize_orderbook_yes_terms():
    raw = {"marketId": 7, "bids": [[0.49, 120], [0.48, 80]],
           "asks": [[0.51, 100], [0.52, 60]]}
    book = normalize_orderbook(raw)
    assert isinstance(book, NormalizedBook)
    assert book.market_id == 7
    assert book.bids[0].price == 0.49 and book.bids[0].size == 120.0
    assert book.asks[1].price == 0.52 and book.asks[1].size == 60.0


def test_batch_ids_chunks_by_100():
    ids = [str(i) for i in range(250)]
    chunks = batch_ids(ids, 100)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_batch_ids_empty():
    assert batch_ids([], 100) == []
