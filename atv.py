import requests
import re
import time
import os
import json

# --- AYARLAR ---
BASE_URL = "https://www.atv.com.tr"

# Kalıcı kayıt (daha önce çekilenler tekrar çekilmez)
STATE_FILE = "atv_state.json"
M3U_FILE = "atv.m3u"
FULL_REFRESH = os.environ.get("FULL_REFRESH") == "1"  # 1 ise kayıtlar yok sayılır

# 1. Taranacak Kategori Sayfaları
DIRECTORIES = [
    {"name": "Güncel Diziler", "url": "/diziler", "type": "DIZI"},
    {"name": "Eski Diziler", "url": "/eski-diziler", "type": "DIZI"},
    {"name": "Programlar", "url": "/programlar", "type": "PROGRAM"}
]

# 2. Manuel Eklenecek Özel Haber/Program Linkleri (Slug'ları)
# Bu programlar genel listelerde çıkmasa bile zorla eklenir.
MANUAL_SHOWS = [
    {"slug": "atv-ana-haber", "name": "ATV Ana Haber", "type": "HABER"},
    {"slug": "kahvalti-haberleri", "name": "Kahvaltı Haberleri", "type": "HABER"},
    {"slug": "gun-ortasi-bulteni", "name": "Gün Ortası Bülteni", "type": "HABER"},
    {"slug": "atvde-hafta-sonu", "name": "ATV'de Hafta Sonu", "type": "HABER"}
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Referer': 'https://www.atv.com.tr/'
}

def get_all_content():
    """Tüm dizileri, programları ve haberleri toplar"""
    content_dict = {}

    # 1. Kategori Sayfalarını Tara
    for directory in DIRECTORIES:
        try:
            print(f"[{directory['name']}] Sayfası taranıyor...")
            r = requests.get(f"{BASE_URL}{directory['url']}", headers=HEADERS, timeout=15)
            
            # Regex ile linkleri ve resimleri bul
            pattern = r'<a href="/([^"]+)"[^>]*?class="[^"]*blankpage[^"]*"[^>]*?>.*?<img[^>]*?src="([^"]+)"[^>]*?alt="([^"]+)"'
            matches = re.findall(pattern, r.text, re.DOTALL)

            for slug, logo, name in matches:
                # Gereksizleri atla
                if any(x in slug.lower() for x in ['canli-yayin', 'fragman', 'yayin-akisi']):
                    continue
                
                # Resim URL temizle
                clean_logo = logo.split('?')[0]
                
                if slug not in content_dict:
                    content_dict[slug] = {
                        'name': name.strip(),
                        'slug': slug,
                        'logo': clean_logo,
                        'group': directory['type']
                    }
            print(f"  -> {len(matches)} içerik bulundu.")
            
        except Exception as e:
            print(f"  Hata: {e}")

    # 2. Manuel Haber Bültenlerini Ekle
    print("[Özel Haber Bültenleri] Kontrol ediliyor...")
    for show in MANUAL_SHOWS:
        if show['slug'] not in content_dict:
            # Haberlerin logosunu manuel bulamayacağımız için varsayılan atv logosu veya boş bırakıyoruz
            # Veya o sayfanın meta taglerinden çekilebilir ama şimdilik basit tutalım.
            content_dict[show['slug']] = {
                'name': show['name'],
                'slug': show['slug'],
                'logo': "https://www.atv.com.tr/assets/img/atv-logo-meta.jpg", # Varsayılan logo
                'group': show['type']
            }
            print(f"  -> {show['name']} listeye eklendi.")

    return list(content_dict.values())

def get_episodes(series_slug, series_name):
    """İçeriğin bölümlerini çeker"""
    episodes = []
    
    # Haber bültenleri için /bolumler sayfası genelde çalışır
    bolumler_url = f"{BASE_URL}/{series_slug}/bolumler"
    
    try:
        r = requests.get(bolumler_url, headers=HEADERS, timeout=10)
        
        # 1. Dropdown Yöntemi (En sağlıklısı)
        # <option value="/avrupa-yakasi/189-bolum/izle">
        dropdown_pattern = r'<option[^>]*value="/([^/]+)/([^"]+)"[^>]*>'
        matches = re.findall(dropdown_pattern, r.text)
        
        if matches:
            for slug, path in matches:
                if slug == series_slug and 'izle' in path:
                    full_url = f"{BASE_URL}/{slug}/{path}"
                    
                    # Bölüm adını/numarasını path'den çıkar
                    # örn: 189-bolum-izle -> 189. Bölüm
                    # örn: 2023-10-10-bolum-izle -> 2023-10-10
                    ep_name = path.replace('-izle', '').replace('-bolum', '').replace('-', ' ').title()
                    
                    # Sıralama için numara bulmaya çalış
                    ep_num = 0
                    num_match = re.search(r'^(\d+)', ep_name)
                    if num_match:
                        ep_num = int(num_match.group(1))
                    
                    episodes.append({
                        'url': full_url,
                        'name': f"{ep_name}. Bölüm" if ep_num > 0 and len(str(ep_num)) < 5 else ep_name, # Tarih ise "Bölüm" yazma
                        'order': ep_num
                    })

        # 2. Eğer dropdown yoksa ve bu bir DİZİ ise brute-force dene (Haberlerde brute-force yapılmaz)
        if not episodes and 'haber' not in series_slug:
            # (Buradaki eski brute-force mantığını kısalttım, genelde dropdown çalışır)
            pass
            
    except Exception as e:
        print(f"    Bölüm hatası: {e}")

    # Sırala (Ters sırala ki en yeni en üstte olsun veya M3U için düz)
    # Genelde diziler 1'den başlar, haberler tarihe göre.
    # Biz gelen sırayı koruyalım veya numaraya göre sıralayalım.
    episodes.sort(key=lambda x: x['order'])
    return episodes

def fix_fake_url(video_url):
    """Karmaşık ATV url'lerini düzeltir"""
    if not video_url: return None
    
    # Pattern: i.tmgrup.com.trvideo/dizi_001_...
    if 'i.tmgrup.com.trvideo/' in video_url:
        try:
            filename = video_url.split('/')[-1]
            # karadayi_008_0150.mp4 -> dizi: karadayi, bolum: 008
            match = re.match(r'([a-zA-Z0-9-]+)_(\d+)_', filename)
            if match:
                dizi = match.group(1)
                bolum = int(match.group(2))
                # Gerçek CDN adresi
                real = f"https://atv-vod.ercdn.net/{dizi}/{bolum:03d}/{dizi}_{bolum:03d}.smil/playlist.m3u8"
                return real
        except:
            pass
            
    # Diğer tmgrup redirectleri
    if '//i.tmgrup.com.tr/' in video_url:
        # Basit bir replace deneyelim, genelde atv-vod.ercdn.net ile değişir
        # Ancak regex daha güvenli yukarıdaki gibi.
        pass

    return video_url

def extract_video_url(episode_url):
    """Sayfaya gidip video linkini cımbızlar"""
    try:
        r = requests.get(episode_url, headers=HEADERS, timeout=10)
        
        # 1. JSON-LD içindeki contentUrl
        match = re.search(r'"contentUrl"\s*:\s*"([^"]+)"', r.text)
        if match:
            url = fix_fake_url(match.group(1))
            if url: return url
            
        # 2. Direkt mp4/m3u8
        patterns = [
            r'(https?://atv-vod\.ercdn\.net/[^\s"\']+\.m3u8[^\s"\']*)',
            r'src="(https?://[^"]+\.(?:mp4|m3u8)[^"]*)"',
            r'video-src="([^"]+)"'
        ]
        
        for p in patterns:
            m = re.findall(p, r.text)
            for url in m:
                if 'fragman' not in url and 'reklam' not in url:
                    fixed = fix_fake_url(url)
                    if fixed: return fixed
                    
    except:
        pass
    return None

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

def create_m3u(data, quiet=False):
    """M3U Dosyası Oluşturur"""
    if not quiet:
        print(f"\n📝 {M3U_FILE} dosyası yazılıyor...")

    with open(M3U_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")

        for slug, item in data.items():
            group = item['group']
            title = item['name']
            logo = item['logo']

            for ep in item['episodes']:
                full_title = f"{title} - {ep['name']}"
                f.write(f'#EXTINF:-1 group-title="{group}" tvg-logo="{logo}",{full_title}\n')
                f.write(f"{ep['url']}\n")

    if not quiet:
        print("✅ M3U Tamamlandı!")

def main():
    print("🚀 ATV VOD Scraper Başlatıldı (M3U Modu)...")

    state = load_state()  # { slug: {name, group, logo, episodes: [{name,url,page,order}]} }
    print(f"📦 Kayıtlı içerik: {len(state)} dizi/program")

    all_content = get_all_content()
    total = len(all_content)

    for i, item in enumerate(all_content, 1):
        print(f"\n[{i}/{total}] İşleniyor: {item['name']} ({item['group']})")

        episodes = get_episodes(item['slug'], item['name'])
        if not episodes:
            print("  -> Bölüm bulunamadı.")
            continue

        entry = state.get(item['slug'])
        known = {e['page'] for e in entry['episodes']} if entry else set()

        new_eps = []
        for ep in episodes:
            if ep['url'] not in known:
                known.add(ep['url'])
                new_eps.append(ep)

        if not new_eps:
            print(f"  ✓ Yeni bölüm yok ({len(entry['episodes']) if entry else 0} kayıtlı).")
            continue

        print(f"  -> {len(new_eps)} yeni bölüm, linkler çözülüyor...")
        resolved = []
        for ep in new_eps:
            video_url = extract_video_url(ep['url'])
            if video_url:
                resolved.append({
                    'name': ep['name'],
                    'url': video_url,
                    'page': ep['url'],
                    'order': ep['order']
                })
                print(f"    + {ep['name']} eklendi.")
            else:
                print(f"    - {ep['name']} video bulunamadı.")

        if not resolved:
            continue

        if entry:
            entry['episodes'] = sorted(entry['episodes'] + resolved, key=lambda x: x.get('order', 0))
            entry['name'] = item['name']
            entry['logo'] = item['logo']
        else:
            state[item['slug']] = {
                'name': item['name'],
                'group': item['group'],
                'logo': item['logo'],
                'episodes': sorted(resolved, key=lambda x: x['order'])
            }

        save_state(state)  # Her içerikten sonra kaydet
        create_m3u(state, quiet=True)

    save_state(state)
    create_m3u(state)

if __name__ == "__main__":
    main()
