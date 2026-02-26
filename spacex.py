import os
import json
import time
import random
import requests

# ================== 配置 ==================
QUERY = "SpaceX"  # 仅 SpaceX
POLL_SECONDS = 600
STATE_FILE = "state.json"

BOT_TOKEN = "8411860989:AAHOhCcDEgqxZjtTDkz7R-x8IyDPb2N-Yb4"
CHAT_ID = "-1003455972438"
SEC_UA = os.getenv("SEC_USER_AGENT", "EdgarIpoWatch/1.0 (your_email@example.com)")

SEC_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
TG_SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
RETRY_STATUS = {429, 500, 502, 503, 504}

# IPO/上市相关常见表单（尽量全一点，宁可多抓再过滤）
FORM_WHITELIST = {
    "S-1", "S-1/A",
    "F-1", "F-1/A",
    "S-4", "S-4/A",
    "F-4", "F-4/A",
    "8-A12B", "8-A12B/A",
    "8-A12G", "8-A12G/A",
    "424B1", "424B2", "424B3", "424B4", "424B5", "424B7",
    "424B", "424H", "424I",
    "FWP",
    "POS AM", "POSASR",
    "EFFECT",
}

# ================== 工具函数 ==================
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen_ids": [], "consecutive_failures": 0}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)
    s.setdefault("seen_ids", [])
    s.setdefault("consecutive_failures", 0)
    return s


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def tg_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("请设置环境变量 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")
    payload = {
        "chat_id": CHAT_ID,
        "text": text[:3900],
        "disable_web_page_preview": True,
    }
    r = requests.post(TG_SEND_URL, json=payload, timeout=30)
    r.raise_for_status()


def sec_search(session: requests.Session):
    params = {
        "q": QUERY,
        "dateRange": "custom",
        "startdt": "2001-01-01",
        "enddt": "2099-12-31",
        "category": "form-cat0",
        "from": 0,
        "size": 50,
        "sort": "desc",
    }
    headers = {
        "User-Agent": SEC_UA,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.sec.gov/",
        "Origin": "https://www.sec.gov",
    }

    backoff = 10
    attempts = 6
    last_err = None

    for i in range(1, attempts + 1):
        try:
            r = session.get(SEC_SEARCH_URL, params=params, headers=headers, timeout=30)
            if r.status_code in RETRY_STATUS:
                raise requests.HTTPError(f"temporary HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if i == attempts:
                raise
            time.sleep(backoff + random.uniform(0, 2.0))
            backoff = min(backoff * 2, 600)

    raise last_err


def normalize_items(data):
    hits = (((data or {}).get("hits") or {}).get("hits") or [])
    items = []
    for h in hits:
        src = (h or {}).get("_source") or {}
        _id = (h or {}).get("_id") or ""

        cik = str(src.get("cik") or "").lstrip("0")
        adsh = str(src.get("adsh") or "")
        form = str(src.get("form") or "")
        filed_at = src.get("filedAt") or src.get("filing_date") or ""

        company = ""
        dn = src.get("display_names")
        if isinstance(dn, list) and dn:
            company = dn[0]
        company = company or (src.get("companyName") or "")

        link = src.get("url") or ""
        if (not link) and cik and adsh:
            link = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-','')}/"

        stable_id = _id or f"{cik}-{adsh}-{form}-{filed_at}"

        items.append(
            {
                "id": stable_id,
                "cik": cik,
                "adsh": adsh,
                "form": form,
                "filed_at": filed_at,
                "company": company,
                "link": link,
            }
        )
    return items


def filter_ipo_spacex(items):
    wl = {f.upper() for f in FORM_WHITELIST}
    out = []
    for it in items:
        form = (it.get("form") or "").strip().upper()
        company = (it.get("company") or "").strip()
        if form not in wl:
            continue
        # 仅 company 名称里包含 SpaceX（更严格，噪音更低）
        if "SPACEX" not in company.upper():
            continue
        out.append(it)
    return out


def fmt_item(it):
    parts = [f"[{it.get('form','')}] {it.get('company','')}".strip()]
    if it.get("filed_at"):
        parts.append(f"Filed: {it['filed_at']}")
    if it.get("cik"):
        parts.append(f"CIK: {it['cik']}")
    if it.get("link"):
        parts.append(it["link"])
    return "\n".join(parts).strip()


def main():
    state = load_state()
    seen = set(state.get("seen_ids") or [])

    tg_send("SEC IPO watcher started: forms whitelist + company contains 'SpaceX'.")

    with requests.Session() as session:
        while True:
            try:
                data = sec_search(session)
                items = normalize_items(data)
                items = filter_ipo_spacex(items)

                new_items = [it for it in items if it["id"] not in seen]

                state["consecutive_failures"] = 0
                save_state(state)

                if new_items:
                    msg = "NEW SEC IPO-related hits (SpaceX filtered):\n\n" + "\n\n---\n\n".join(
                        fmt_item(it) for it in new_items
                    )
                    tg_send(msg)

                    for it in new_items:
                        seen.add(it["id"])
                    state["seen_ids"] = list(seen)[-1000:]
                    save_state(state)

                time.sleep(POLL_SECONDS)

            except Exception as e:
                state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
                save_state(state)
                try:
                    tg_send(f"SEC IPO watcher error: {type(e).__name__}: {e}"[:3900])
                except Exception:
                    pass
                time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).")