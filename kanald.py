import cloudscraper
from bs4 import BeautifulSoup
import time
import re
import os
import json

# Ayarlar
BASE_URL = "https://www.kanald.com.tr"
MAX_EPISODE_PAGES = 50           # Bir dizi için taranacak maksimum bölüm sayfası
STATE_FILE = "kanald_state.json"  # Daha önce çekilenlerin kaydı
M3U_FILE = "kanald.m3u"
FULL_REFRESH = os.environ.get("FULL_REFRESH") == "1"  # 1 ise kayıtlar yok sayılır, her şey baştan çekilir

# Taranacak URL Listesi
TARGETS = [
    {"url": "https://www.kanald.com.tr/diziler", "type": "DIZI", "is_archive": False},
    {"url": "https://www.kanald.com.tr/programlar", "type": "PROGRAM", "is_archive": False},
    {"url": "https://www.kanald.com.tr/diziler/arsiv?page=", "type": "DIZI", "is_archive": True},
    {"url": "https://www.kanald.com.tr/programlar/arsiv?page=", "type": "PROGRAM", "is_archive": True}
]


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
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def get_real_m3u8(scraper, bolum_url):
    """Bölüm sayfasından ve embed içinden gerçek M3U8 linkini bulur"""
    try:
        # 1. Aşama: Bölüm sayfasından Embed URL'yi çek
        r1 = scraper.get(bolum_url, timeout=15)
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
                return None
        else:
            embed_url = embed_match.group(1)

        # 2. Aşama: Embed sayfasının içine girip M3U8 pattern'lerini ara
        r2 = scraper.get(embed_url, timeout=15, headers={"Referer": BASE_URL})
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
                return found_url.replace('\\/', '/')

        return None
    except Exception as e:
        print(f"      Link bulma hatası: {e}")
        return None


def get_episodes(scraper, show_url, known_pages=None, max_pages=MAX_EPISODE_PAGES):
    """
    Bir dizinin/programın bölümlerini çeker (sayfalama ile).
    known_pages: daha önce işlenmiş bölüm sayfası URL'leri (tekrar çekilmez).
    Sadece YENİ bölümleri döndürür. Bir sayfadaki tüm bölümler zaten biliniyorsa durur.
    """
    known_pages = known_pages or set()
    new_episodes = []
    seen = set()
    base = show_url.rstrip('/') + "/bolumler"

    for page in range(1, max_pages + 1):
        page_url = base if page == 1 else f"{base}?page={page}"
        try:
            resp = scraper.get(page_url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            cards = soup.select('.story-card, .content-card, .video-card, .card-item')
        except Exception as e:
            print(f"    Bölümler çekilirken hata (sayfa {page}): {e}")
            break

        unseen_on_page = 0   # Bu oturumda ilk kez görülen kartlar
        new_on_page = 0      # Gerçekten yeni (state'te olmayan) bölümler

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
                continue  # Daha önce çekilmiş

            new_on_page += 1
            ep_name = name_tag.get_text(strip=True)
            m3u8 = get_real_m3u8(scraper, b_url)

            if m3u8:
                new_episodes.append({"name": ep_name, "url": m3u8, "page": b_url})
                print(f"      🔗 Yeni: {ep_name[:30]}...")
            else:
                print(f"      ⚠️ Stream bulunamadı: {ep_name[:30]}...")

        # Sayfada hiç yeni kart yoksa son sayfa
        if unseen_on_page == 0:
            break
        # Önceden bilinen bir dizide, sayfadaki her şey zaten biliniyorsa daha eskilere inmeye gerek yok
        if known_pages and new_on_page == 0:
            break
        time.sleep(0.5)

    return new_episodes


def run_scraper():
    print("🚀 Kanal D M3U8 Scraper Başlatıldı...")
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})

    state = load_state()  # { "Dizi Adı": { "poster", "type", "url", "bolumler": [{name,url,page}] } }
    print(f"📦 Kayıtlı içerik: {len(state)} dizi/program")

    processed_this_run = set()  # Aynı çalışmada bir diziyi iki kez kontrol etme

    for target in TARGETS:
        print(f"\n📂 Kategori Taranıyor: {target['url']} ({target['type']})")

        page_range = range(1, 6) if target['is_archive'] else range(1, 2)

        for page in page_range:
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

                    if entry:
                        print(f"  🔄 Kontrol: {title} ({len(known_pages)} kayıtlı bölüm)")
                    else:
                        print(f"  📺 Yeni içerik: {title}")

                    new_eps = get_episodes(scraper, full_url, known_pages=known_pages)

                    if new_eps:
                        if entry:
                            # Yeni bölümler en üste (sitede en yeni en üstte)
                            entry["bolumler"] = new_eps + entry["bolumler"]
                            if poster:
                                entry["poster"] = poster
                        else:
                            state[title] = {
                                "poster": poster,
                                "type": target['type'],
                                "url": full_url,
                                "bolumler": new_eps
                            }
                        save_state(state)  # Her diziden sonra kaydet (yarıda kesilirse kayıp olmasın)
                        create_m3u(state, quiet=True)

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
