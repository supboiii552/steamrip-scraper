import concurrent.futures
import json
import random
import re
import sys
import threading
import time
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
BASE = "https://steamrip.com"

PROXY_SOURCES = [
    ("monosans", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("speedx", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    ("proxyscrape", "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all"),
]

HYDRA_HOSTS = {
    "gofile.io", "gofile.me", "mediafire.com", "mediafire.net",
    "pixeldrain.com", "datanodes.cc", "vikingfile.com", "rootz.io",
    "buzzheavier.com", "bzzhr.to", "fuckingfast.co", "real-debrid.com",
    "realdebrid.com", "alldebrid.com", "premiumize.me", "torbox.app", "mega.nz"
}

SKIP_DOMAINS = {
    "steamrip.com", "twitter.com", "x.com", "discord.gg", "t.me",
    "reddit.com", "youtube.com", "facebook.com", "instagram.com",
    "linkedin.com", "twitch.tv", "tiktok.com"
}

BACKOFF_LOCK = threading.Lock()
BACKOFF_UNTIL = 0.0

def handle_backoff(delay_seconds):
    global BACKOFF_UNTIL
    with BACKOFF_LOCK:
        now = time.time()
        if now + delay_seconds > BACKOFF_UNTIL:
            BACKOFF_UNTIL = now + delay_seconds
            print(f"\n[!] Rate limit/block detected. Pausing workers for {delay_seconds}s...")

def wait_if_backed_off():
    global BACKOFF_UNTIL
    while True:
        now = time.time()
        with BACKOFF_LOCK:
            remaining = BACKOFF_UNTIL - now
        if remaining <= 0:
            break
        time.sleep(min(remaining, 1.0))

def load_proxies():
    all_proxies = []
    for name, url in PROXY_SOURCES:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            proxies = []
            for line in r.text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)", line)
                if m:
                    ip_port = m.group(1)
                    if not ip_port.startswith("0.0.0.0"):
                        proxies.append(f"http://{ip_port}")
            if proxies:
                all_proxies.extend(proxies)
        except Exception:
            pass
    return list(dict.fromkeys(all_proxies))

def validate_proxies(proxies, sample_size=200):
    if not proxies:
        return []
    sample = random.sample(proxies, min(sample_size, len(proxies)))
    alive = []
    lock = threading.Lock()

    def test(p):
        s = requests.Session()
        s.headers.update(HEADERS)
        s.proxies = {"http": p, "https": p}
        try:
            r = s.get(f"{BASE}/", timeout=4)
            return r.status_code < 500
        except Exception:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
        futures = {pool.submit(test, p): p for p in sample}
        for fut in concurrent.futures.as_completed(futures):
            p = futures[fut]
            if fut.result():
                with lock:
                    alive.append(p)

    dead = set(sample) - set(alive)
    return [p for p in proxies if p not in dead]

def scrape_list():
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(f"{BASE}/games-list/", timeout=20)
    soup = BeautifulSoup(r.text, "html.parser")
    anchors = soup.select("ul.az-list li.az-list-item a[href]")
    urls = []
    seen = set()
    for a in anchors:
        href = a["href"].strip()
        full_url = urljoin(BASE, href)
        if full_url not in seen:
            seen.add(full_url)
            urls.append(full_url)
    return urls

def extract_game(url, soup):
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else url.strip("/").split("/")[-1]
    title = re.sub(r"\s*Free Download.*$", "", title, flags=re.I)

    text = soup.get_text()
    m = re.search(r"([\d.]+\s*(?:GB|MB))", text, re.I)
    size = m.group(1) if m else "Unknown"

    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    date = m.group(1) + "T00:00:00+00:00" if m else ""

    uris = []
    for a in soup.select("a[href]"):
        href = a["href"].strip()
        if href.startswith("magnet:"):
            uris.append(href)
            continue

        full_url = urljoin(url, href)
        parsed = urlparse(full_url)
        if parsed.scheme not in ("http", "https"):
            continue

        domain = parsed.netloc.lower()
        if any(domain == d or domain.endswith("." + d) for d in SKIP_DOMAINS):
            continue

        if domain == "bzzhr.to" or domain.endswith(".bzzhr.to"):
            full_url = parsed._replace(netloc="buzzheavier.com").geturl()
            parsed = urlparse(full_url)
            domain = parsed.netloc.lower()

        if parsed.path.lower().endswith(".torrent"):
            uris.append(full_url)
            continue

        if any(domain == h or domain.endswith("." + h) for h in HYDRA_HOSTS):
            uris.append(full_url)

    return {"title": title, "fileSize": size, "uris": list(set(uris)), "uploadDate": date}

def scrape_game(url, proxies, dead_set, lock, backoff_delay=15):
    last_err = None
    for attempt in range(4):
        wait_if_backed_off()
        time.sleep(random.uniform(0.1, 0.3))

        s = requests.Session()
        s.headers.update(HEADERS)
        used_proxy = None

        if proxies:
            with lock:
                alive = [p for p in proxies if p not in dead_set]
            if alive:
                used_proxy = random.choice(alive)
                s.proxies = {"http": used_proxy, "https": used_proxy}

        try:
            r = s.get(url, timeout=12)
            if r.status_code in (429, 403, 503):
                handle_backoff(backoff_delay * (attempt + 1))
                raise Exception(f"HTTP {r.status_code} Blocked")

            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                return extract_game(url, soup)

            raise Exception(f"HTTP {r.status_code}")

        except Exception as e:
            last_err = e
            if used_proxy:
                with lock:
                    dead_set.add(used_proxy)
            if any(err in str(e) for err in ("10054", "RemoteDisconnected", "Max retries", "Blocked")):
                handle_backoff(backoff_delay)
            time.sleep((2 ** attempt) + random.uniform(0.2, 0.8))

    raise last_err

def main():
    limit = 5000
    workers = 5
    use_proxies = False
    backoff_delay = 15

    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])
    if "--backoff" in sys.argv:
        backoff_delay = int(sys.argv[sys.argv.index("--backoff") + 1])
    if "--use-proxy" in sys.argv:
        use_proxies = True

    proxies = []
    if use_proxies:
        print("[*] Proxy mode enabled. Loading proxies...")
        raw = load_proxies()
        if raw:
            proxies = validate_proxies(raw)

    if not proxies:
        print("[*] Running scraper with direct connections...")

    print("[*] Fetching games list from SteamRIP...")
    urls = scrape_list()
    target_count = min(limit, len(urls))
    print(f"[+] Found {len(urls)} games. Processing {target_count} pages with {workers} workers...\n")

    dead_set = set()
    lock = threading.Lock()
    games = []
    failed = 0
    done = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(scrape_game, url, proxies, dead_set, lock, backoff_delay): url
            for url in urls[:limit]
        }
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            url = futures[fut]
            try:
                g = fut.result()
                games.append(g)
                if done % 50 == 0 or done == target_count:
                    print(f"  [{done}/{target_count}] {g['title']} — {len(g['uris'])} links")
            except Exception as e:
                failed += 1
                if failed <= 10:
                    print(f"  SKIP {url}: {str(e)[:80]}")

    games.sort(key=lambda x: x["title"].lower())
    data = {"name": "SteamRip", "downloads": games}
    with open("steamrip.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    total_links = sum(len(g["uris"]) for g in games)
    print(f"\n[+] Extraction finished: {len(games)} games scraped, {total_links} total links, {failed} failed.")

if __name__ == "__main__":
    main()
