import cloudscraper
from bs4 import BeautifulSoup
import time
import re
import os
import json
import threading
from concurrent.futures import ThreadPoolExecutor

# Ayarlar
BASE_URL = "https://www.kanald.com.tr"
MAX_EPISODE_PAGES = 50            # Bir dizi için taranacak maksimum bölüm sayfası
STATE_FILE = "kanald_state.json"  # Daha önce çekilenlerin kaydı
M3U_FILE = "kanald.m3u"
FULL_REFRESH = os.environ.get("FULL_REFRESH") == "1"  # 1 ise kayıtlar yok sayılır, her şey baştan çekilir

WORKERS = int(os.environ.get("WORKERS", "6"))                  # Aynı anda çözülen bölüm sayısı
TIME_BUDGET_MIN = int(os.environ.get("TIME_BUDGET_MIN", "320"))  # Bu süre dolunca temiz çıkış (kayıt + yükleme yapılsın)
MAX_RETRIES = 3          # Geçici hatalarda (timeout, 403/429/5xx) en fazla deneme
RECHECK_NEWEST = 15      # Linki olmayan bölümlerden en yeni N tanesi...
RECHECK_SECONDS = 86400  # ...günde bir tekrar denenir (yayına yeni girmiş olabilir)

START_TIME = time.time()
_local = threading.local()

# Taranacak URL Listesi
TARGETS = [
    {"url": "https://www.kanald.com.tr/diziler", "type": "DIZI", "is_archive": False},
    {"url": "https://www.kanald.com.tr/programlar", "type": "PROGRAM", "is_archive": False},
    {"url": "https://www.kanald.com.tr/diziler/arsiv?page=", "type": "DIZI", "is_archive": True},
    {"url": "https://www.kanald.com.tr/programlar/arsiv?page=", "type": "PROGRAM", "is_archive": True}
]


def new_scraper():
    return cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})


def thread_scraper():
    """Her thread kendi oturumunu kullanır (oturumlar thread-safe değil)."""
    s = getattr(_local, "s", None)
    if s is None:
        s = new_scraper()
        _local.s = s
    return s


def time_up():
    return (time.time() - START_TIME) / 60 >= TIME_BUDGET_MIN


def load_state():
    if FULL_REFRESH:
        print("♻️ FULL_REFRESH: kayıtlar yok sayılıyor")
        return {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ State okunamadı, sıfırdan başlanıyor: {e}")
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE_FILE)


def is_retryable_status(code):
    return code in (403, 408, 429) or code >= 500


def get_real_m3u8(bolum_url):
    """
    Bölüm sayfasından ve embed içinden gerçek M3U8 linkini bulur.
    (m3u8, retryable) döner. retryable=True: geçici hata, tekrar denenebilir.
    retryable=False ve m3u8=None: sayfa sorunsuz açıldı ama stream yok (kalıcı).
    """
    scraper = thread_scraper()
    try:
        # 1. Aşama: Bölüm sayfasından Embed URL'yi çek
        r1 = scraper.get(bolum_url, timeout=15)
        if r1.status_code >= 400:
            return None, is_retryable_status(r1.status_code)
        embed_match = re.search(r'<link[^>]+itemprop=["\']embedURL["\'][^>]+href=["\']([^"\']+)["\']', r1.text)

        if not embed_match:
            # Alternatif: Iframe src içinde ara
            soup = BeautifulSoup(r1.text, 'html.parser')
            iframe = soup.find('iframe', src=re.compile(r'embed'))
            if iframe:
                embed_url = iframe['src']
                if embed_url.startswith('//'):
                    embed_url = "https:" + embed_url
            else:
                return None, False
        else:
            embed_url = embed_match.group(1)

        # 2. Aşama: Embed sayfasının içine girip M3U8 pattern'lerini ara
        r2 = scraper.get(embed_url, timeout=15, headers={"Referer": BASE_URL})
        if r2.status_code >= 400:
            return None, is_retryable_status(r2.status_code)
        embed_html = r2.text

        patterns = [
            r'https?://vod[0-9]*\.cf\.dmcdn\.net/[^\s"\']+\.m3u8',  # DMCDN Pattern
            r'https?://[^\s"\']+\.m3u8',                           # Genel M3U8
            r'["\']videoUrl["\']\s*:\s*["\']([^"\']+)["\']',       # JS VideoURL
            r'src=["\']([^"\']+\.m3u8)["\']'                       # Src tag
        ]

        for p in patterns:
            m = re.search(p, embed_html)
            if m:
                found_url = m.group(1) if "(" in p else m.group(0)
                return found_url.replace('\\/', '/'), False

        return None, False
    except Exception as e:
        print(f"      Link bulma hatası: {e}")
        return None, True


def resolve_parallel(todo):
    """[(url, ad), ...] -> [(m3u8, retryable), ...] (sıra korunur)"""
    if not todo:
        return []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return list(ex.map(lambda t: get_real_m3u8(t[0]), todo))


def get_episodes(scraper, show_url, known_pages, failed, incremental, max_pages=MAX_EPISODE_PAGES):
    """
    Bir dizinin/programın YENİ bölümlerini çeker (sayfalama ile).
    known_pages : linki zaten kayıtlı bölüm sayfaları (tekrar çekilmez)
    failed      : {sayfa_url: {"n": deneme, "t": zaman}} linki bulunamayanlar (yerinde güncellenir)
    incremental : True ise tüm sayfa zaten biliniyorsa daha eskilere inilmez
    (yeni_bölümler, tamamlandı_mı) döner.
    """
    new_episodes = []
    seen = set()
    complete = True
    now = int(time.time())
    base = show_url.rstrip('/') + "/bolumler"

    for page in range(1, max_pages + 1):
        if time_up():
            complete = False
            print("    ⏱️ Süre doldu, bu dizi yarıda bırakıldı.")
            break

        page_url = base if page == 1 else f"{base}?page={page}"
        try:
            resp = scraper.get(page_url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            cards = soup.select('.story-card, .content-card, .video-card, .card-item')
        except Exception as e:
            print(f"    Bölümler çekilirken hata (sayfa {page}): {e}")
            complete = False
            break

        unseen_on_page = 0
        todo = []  # [(url, ad)]

        for card in cards:
            link_tag = card.find('a', href=True) or (card if card.name == 'a' else None)
            name_tag = card.select_one('.title, h3, h2, .caption, .card-title')
            if not (link_tag and name_tag):
                continue

            b_url = link_tag['href']
            if not b_url.startswith('http'):
                b_url = BASE_URL + b_url if b_url.startswith('/') else BASE_URL + '/' + b_url

            if b_url in seen:
                continue
            seen.add(b_url)
            unseen_on_page += 1

            if b_url in known_pages:
                continue

            f = failed.get(b_url)
            if f and f.get("n", 0) >= MAX_RETRIES:
                # Kalıcı olarak linksiz: sadece en yeni bölümler günde bir tekrar denenir
                recheck = page == 1 and unseen_on_page <= RECHECK_NEWEST and now - f.get("t", 0) > RECHECK_SECONDS
                if not recheck:
                    continue

            todo.append((b_url, name_tag.get_text(strip=True)))

        results = resolve_parallel(todo)
        for (b_url, ep_name), (m3u8, retryable) in zip(todo, results):
            if m3u8:
                new_episodes.append({"name": ep_name, "url": m3u8, "page": b_url})
                failed.pop(b_url, None)
                print(f"      🔗 Yeni: {ep_name[:30]}...")
            else:
                n = failed.get(b_url, {}).get("n", 0)
                failed[b_url] = {"n": (n + 1) if retryable else MAX_RETRIES, "t": now}
                print(f"      ⚠️ Stream bulunamadı: {ep_name[:30]}...")

        # Sayfada hiç yeni kart yoksa son sayfa
        if unseen_on_page == 0:
            break
        # Daha önce tamamen taranmış dizide, sayfadaki her şey zaten işlenmişse eskilere inme
        if incremental and not todo:
            break
        time.sleep(0.3)

    return new_episodes, complete


def run_scraper():
    print("🚀 Kanal D M3U8 Scraper Başlatıldı...")
    scraper = new_scraper()

    state = load_state()  # { "Dizi Adı": { poster, type, url, complete, failed, bolumler: [...] } }
    print(f"📦 Kayıtlı içerik: {len(state)} dizi/program")

    processed_this_run = set()
    stop = False

    for target in TARGETS:
        if stop:
            break
        print(f"\n📂 Kategori Taranıyor: {target['url']} ({target['type']})")

        page_range = range(1, 6) if target['is_archive'] else range(1, 2)

        for page in page_range:
            if stop:
                break
            current_url = f"{target['url']}{page}" if target['is_archive'] else target['url']
            if target['is_archive']:
                print(f"  📄 Sayfa {page}...")

            try:
                resp = scraper.get(current_url, timeout=15)
                soup = BeautifulSoup(resp.text, 'html.parser')
                cards = soup.select('a.poster-card, .program-card a, .series-card a')

                if not cards and target['is_archive']:
                    print("    Bu sayfada içerik yok, döngüden çıkılıyor.")
                    break

                for card in cards:
                    href = card.get('href')
                    if not href:
                        continue

                    full_url = BASE_URL + href if href.startswith('/') else href

                    img_tag = card.find('img')
                    title = card.get('title')
                    if not title and img_tag:
                        title = img_tag.get('alt')
                    if not title:
                        title = href.strip('/').split('/')[-1].replace('-', ' ').title()

                    poster = ""
                    if img_tag:
                        poster = img_tag.get('data-src') or img_tag.get('src') or ""

                    if title in processed_this_run:
                        continue
                    processed_this_run.add(title)

                    entry = state.get(title)
                    known_pages = {e["page"] for e in entry["bolumler"]} if entry else set()
                    failed = entry.setdefault("failed", {}) if entry else {}
                    incremental = bool(entry and entry.get("complete", True))

                    if entry:
                        print(f"  🔄 Kontrol: {title} ({len(known_pages)} kayıtlı bölüm)")
                    else:
                        print(f"  📺 Yeni içerik: {title}")

                    before = (len(known_pages), json.dumps(failed, sort_keys=True), entry.get("complete") if entry else None)
                    new_eps, complete = get_episodes(scraper, full_url, known_pages, failed, incremental)

                    if entry is None:
                        # Hiç linki çıkmasa bile kaydet: bir daha baştan taranmasın
                        state[title] = {
                            "poster": poster,
                            "type": target['type'],
                            "url": full_url,
                            "complete": complete,
                            "failed": failed,
                            "bolumler": new_eps
                        }
                        changed = True
                    else:
                        if new_eps:
                            entry["bolumler"] = new_eps + entry["bolumler"]
                        if poster:
                            entry["poster"] = poster
                        entry["complete"] = complete
                        after = (len(known_pages) + len(new_eps), json.dumps(failed, sort_keys=True), complete)
                        changed = bool(new_eps) or before[1:] != after[1:]

                    if changed:
                        save_state(state)  # Her diziden sonra kaydet
                        create_m3u(state, quiet=True)

                    if time_up():
                        print("\n⏱️ Süre doldu, temiz çıkış yapılıyor (kalan diziler sonraki çalışmada).")
                        stop = True
                        break

            except Exception as e:
                print(f"  ❌ Sayfa hatası: {e}")
                continue

    save_state(state)
    create_m3u(state)


def create_m3u(data, quiet=False):
    if not quiet:
        print(f"\n📝 {M3U_FILE} dosyası oluşturuluyor...")

    with open(M3U_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")

        for title, content in data.items():
            poster = content.get('poster', '')

            for ep in content['bolumler']:
                display_name = f"{title} - {ep['name']}"
                f.write(f'#EXTINF:-1 group-title="{title}" tvg-logo="{poster}",{display_name}\n')
                f.write(f"{ep['url']}\n")

    if not quiet:
        print("✅ M3U dosyası hazır!")


if __name__ == "__main__":
    run_scraper()
