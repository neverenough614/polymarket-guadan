def _token(token_id, source="Normal LP", efficiency=0.0):
    return {
        "token_id": token_id,
        "token_type": "YES",
        "question": f"Question {token_id}",
        "source": source,
        "small_edge_efficiency": efficiency,
    }


def test_batch_order_book_fetch_maps_returned_books_and_omits_missing():
    import main

    class FakeClient:
        def get_order_books(self, token_ids):
            return {
                "token-a": "book-a",
                "token-c": "book-c",
            }

    results = main.get_all_order_books_batch(FakeClient(), ["token-a", "token-b", "token-c"])

    assert results == {"token-a": "book-a", "token-c": "book-c"}


def test_concurrent_order_book_fetch_falls_back_when_batch_fails(monkeypatch):
    import main

    class FakeClient:
        def __init__(self):
            self.client = self

        def get_order_books(self, token_ids):
            raise RuntimeError("batch failed")

        def get_order_book(self, token_id):
            return f"single-{token_id}"

    monkeypatch.setattr(main, "MAX_CONCURRENT_WORKERS", 2)
    results = main.get_all_order_books_concurrent(FakeClient(), ["token-a", "token-b"])

    assert results == {"token-a": "single-token-a", "token-b": "single-token-b"}


def test_placement_limit_keeps_normal_first_and_caps_small_edge(monkeypatch):
    import main

    monkeypatch.setattr(main, "MAX_TOTAL_TOKENS_PER_PLACEMENT_RUN", 5)
    monkeypatch.setattr(main, "MAX_SMALL_EDGE_TOKENS_PER_RUN", 3)

    normal = [_token(f"normal-{i}") for i in range(3)]
    small = [_token(f"small-{i}", "Small Edge", efficiency=i) for i in range(10)]

    selected = main.limit_tokens_for_placement(normal + small)

    assert [t["token_id"] for t in selected] == [
        "normal-0",
        "normal-1",
        "normal-2",
        "small-9",
        "small-8",
    ]


def test_monitor_targets_scan_small_edge_in_round_robin_chunks(monkeypatch):
    import main

    monkeypatch.setattr(main, "MONITOR_MAX_BOOKS_PER_SCAN", 5)
    normal = [_token(f"normal-{i}") for i in range(3)]
    small = [_token(f"small-{i}", "Small Edge") for i in range(8)]

    first = main.select_monitor_targets_for_scan(normal + small, scan_count=1)
    second = main.select_monitor_targets_for_scan(normal + small, scan_count=2)

    assert [t["token_id"] for t in first[:3]] == ["normal-0", "normal-1", "normal-2"]
    assert [t["token_id"] for t in second[:3]] == ["normal-0", "normal-1", "normal-2"]
    assert len(first) == 5
    assert len(second) == 5
    assert {t["token_id"] for t in first[3:]} != {t["token_id"] for t in second[3:]}


def test_probe_batch_skips_active_and_prioritizes_normal(monkeypatch):
    import main

    monkeypatch.setattr(main, "PROBE_BATCH_SIZE", 4)
    monkeypatch.setattr(main, "STABLE_ACTIVE_TARGET", 10)
    monkeypatch.setattr(main, "MAX_ACTIVE_TOKENS", 20)
    monkeypatch.setattr(main, "TARGET_MONITOR_SCAN_SECONDS", 20)
    monkeypatch.setattr(main, "MAX_ACCEPTABLE_MONITOR_SCAN_SECONDS", 30)
    tokens = [
        _token("small-good", "Small Edge", efficiency=9),
        _token("normal-active", "Normal LP"),
        _token("normal-1", "Normal LP"),
        _token("high-1", "High Reward"),
        _token("chain-1", "Chain Rewards"),
        _token("small-ok", "Small Edge", efficiency=5),
    ]

    selected = main.select_probe_batch_tokens(
        tokens,
        active_token_ids={"normal-active"},
        cooldown_until={},
        now=1000.0,
        avg_scan_seconds=10.0,
    )

    assert [t["token_id"] for t in selected] == [
        "small-good",
        "small-ok",
    ]


def test_probe_batch_respects_cooldown(monkeypatch):
    import main

    monkeypatch.setattr(main, "PROBE_BATCH_SIZE", 3)
    monkeypatch.setattr(main, "STABLE_ACTIVE_TARGET", 10)
    monkeypatch.setattr(main, "MAX_ACTIVE_TOKENS", 20)
    monkeypatch.setattr(main, "TARGET_MONITOR_SCAN_SECONDS", 20)
    monkeypatch.setattr(main, "MAX_ACCEPTABLE_MONITOR_SCAN_SECONDS", 30)
    tokens = [
        _token("small-cooldown", "Small Edge", efficiency=20),
        _token("small-ready", "Small Edge", efficiency=10),
    ]

    selected = main.select_probe_batch_tokens(
        tokens,
        active_token_ids=set(),
        cooldown_until={"small-cooldown": 1200.0},
        now=1000.0,
        avg_scan_seconds=10.0,
    )

    assert [t["token_id"] for t in selected] == ["small-ready"]


def test_probe_batch_only_uses_small_edge_tokens(monkeypatch):
    import main

    monkeypatch.setattr(main, "PROBE_BATCH_SIZE", 10)
    monkeypatch.setattr(main, "MAX_ACTIVE_TOKENS", 20)
    monkeypatch.setattr(main, "TARGET_MONITOR_SCAN_SECONDS", 20)

    selected = main.select_probe_batch_tokens(
        [
            _token("normal-1", "Normal LP"),
            _token("high-1", "High Reward"),
            _token("small-1", "Small Edge", efficiency=1),
            _token("small-2", "Small Edge", efficiency=2),
        ],
        active_token_ids=set(),
        cooldown_until={},
        probed_token_ids=set(),
        now=1000.0,
        avg_scan_seconds=10.0,
    )

    assert [t["token_id"] for t in selected] == ["small-2", "small-1"]


def test_probe_batch_prioritizes_unprobed_small_edge(monkeypatch):
    import main

    monkeypatch.setattr(main, "PROBE_BATCH_SIZE", 2)
    monkeypatch.setattr(main, "MAX_ACTIVE_TOKENS", 20)
    monkeypatch.setattr(main, "TARGET_MONITOR_SCAN_SECONDS", 20)

    selected = main.select_probe_batch_tokens(
        [
            _token("small-high-probed", "Small Edge", efficiency=100),
            _token("small-low-new", "Small Edge", efficiency=1),
            _token("small-mid-new", "Small Edge", efficiency=5),
        ],
        active_token_ids=set(),
        cooldown_until={},
        probed_token_ids={"small-high-probed"},
        now=1000.0,
        avg_scan_seconds=10.0,
    )

    assert [t["token_id"] for t in selected] == ["small-mid-new", "small-low-new"]


def test_probe_batch_restarts_after_all_small_edge_probed(monkeypatch):
    import main

    monkeypatch.setattr(main, "PROBE_BATCH_SIZE", 2)
    monkeypatch.setattr(main, "MAX_ACTIVE_TOKENS", 20)
    monkeypatch.setattr(main, "TARGET_MONITOR_SCAN_SECONDS", 20)

    selected = main.select_probe_batch_tokens(
        [
            _token("small-a", "Small Edge", efficiency=1),
            _token("small-b", "Small Edge", efficiency=2),
        ],
        active_token_ids=set(),
        cooldown_until={},
        probed_token_ids={"small-a", "small-b"},
        now=1000.0,
        avg_scan_seconds=10.0,
    )

    assert [t["token_id"] for t in selected] == ["small-b", "small-a"]


def test_probe_batch_continues_past_stable_target_when_monitor_is_fast(monkeypatch):
    import main

    monkeypatch.setattr(main, "PROBE_BATCH_SIZE", 4)
    monkeypatch.setattr(main, "STABLE_ACTIVE_TARGET", 3)
    monkeypatch.setattr(main, "MAX_ACTIVE_TOKENS", 10)
    monkeypatch.setattr(main, "TARGET_MONITOR_SCAN_SECONDS", 20)
    monkeypatch.setattr(main, "MAX_ACCEPTABLE_MONITOR_SCAN_SECONDS", 30)

    selected = main.select_probe_batch_tokens(
        [_token("small-new", "Small Edge", efficiency=1), _token("small-new-2", "Small Edge", efficiency=2)],
        active_token_ids={"a", "b", "c"},
        cooldown_until={},
        now=1000.0,
        avg_scan_seconds=10.0,
    )

    assert [t["token_id"] for t in selected] == ["small-new-2", "small-new"]


def test_probe_batch_pauses_when_monitor_is_slow(monkeypatch):
    import main

    monkeypatch.setattr(main, "PROBE_BATCH_SIZE", 4)
    monkeypatch.setattr(main, "STABLE_ACTIVE_TARGET", 10)
    monkeypatch.setattr(main, "MAX_ACTIVE_TOKENS", 20)
    monkeypatch.setattr(main, "TARGET_MONITOR_SCAN_SECONDS", 20)
    monkeypatch.setattr(main, "MAX_ACCEPTABLE_MONITOR_SCAN_SECONDS", 30)

    selected = main.select_probe_batch_tokens(
        [_token("normal-new", "Normal LP")],
        active_token_ids={"a"},
        cooldown_until={},
        now=1000.0,
        avg_scan_seconds=35.0,
    )

    assert selected == []


def test_probe_batch_uses_half_batch_when_monitor_is_warm(monkeypatch):
    import main

    monkeypatch.setattr(main, "PROBE_BATCH_SIZE", 4)
    monkeypatch.setattr(main, "STABLE_ACTIVE_TARGET", 5)
    monkeypatch.setattr(main, "MAX_ACTIVE_TOKENS", 20)
    monkeypatch.setattr(main, "TARGET_MONITOR_SCAN_SECONDS", 20)
    monkeypatch.setattr(main, "MAX_ACCEPTABLE_MONITOR_SCAN_SECONDS", 30)

    selected = main.select_probe_batch_tokens(
        [_token(f"small-{i}", "Small Edge", efficiency=i) for i in range(10)],
        active_token_ids={"a", "b"},
        cooldown_until={},
        now=1000.0,
        avg_scan_seconds=25.0,
    )

    assert len(selected) == 2


def test_probe_batch_never_exceeds_max_active_slots(monkeypatch):
    import main

    monkeypatch.setattr(main, "PROBE_BATCH_SIZE", 4)
    monkeypatch.setattr(main, "STABLE_ACTIVE_TARGET", 10)
    monkeypatch.setattr(main, "MAX_ACTIVE_TOKENS", 5)
    monkeypatch.setattr(main, "TARGET_MONITOR_SCAN_SECONDS", 20)
    monkeypatch.setattr(main, "MAX_ACCEPTABLE_MONITOR_SCAN_SECONDS", 30)

    selected = main.select_probe_batch_tokens(
        [_token(f"small-{i}", "Small Edge", efficiency=i) for i in range(10)],
        active_token_ids={"a", "b", "c"},
        cooldown_until={},
        now=1000.0,
        avg_scan_seconds=10.0,
    )

    assert len(selected) == 2


def test_split_primary_and_small_edge_tokens():
    import main

    primary, small_edge = main.split_primary_and_small_edge_tokens(
        [
            _token("normal", "Normal LP"),
            _token("high", "High Reward"),
            _token("chain", "Chain Rewards"),
            _token("small", "Small Edge"),
        ]
    )

    assert [t["token_id"] for t in primary] == ["normal", "high", "chain"]
    assert [t["token_id"] for t in small_edge] == ["small"]
