"""M3U dosyalarını SABİT bir Gist'e yükler (aynı Gist ID, aynı dosya adı -> link hiç değişmez).

Kullanım:  python gist_upload.py kanald.m3u [startv.m3u ...]
Ortam değişkenleri: GIST_TOKEN (gist yetkili PAT), GIST_ID
"""
import os
import sys
import requests

API = "https://api.github.com/gists"


def main(paths):
    token = os.environ.get("GIST_TOKEN")
    gist_id = os.environ.get("GIST_ID")
    if not token or not gist_id:
        print("⚠️ GIST_TOKEN / GIST_ID tanımlı değil, Gist yükleme atlandı.")
        return 0

    files = {}
    for path in paths:
        if not os.path.exists(path):
            print(f"⚠️ {path} bulunamadı, atlandı.")
            continue
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        # Sadece "#EXTM3U" varsa Gist'teki dolu listenin üzerine boş yazma
        if len(content.strip().splitlines()) <= 1:
            print(f"⚠️ {path} boş görünüyor, yüklenmedi.")
            continue
        files[os.path.basename(path)] = {"content": content}

    if not files:
        print("Yüklenecek dosya yok.")
        return 0

    r = requests.patch(
        f"{API}/{gist_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"files": files},
        timeout=120,
    )
    if r.status_code != 200:
        print(f"❌ Gist güncellenemedi: {r.status_code} {r.text[:300]}")
        return 1

    owner = r.json().get("owner", {}).get("login", "USER")
    for name in files:
        print(f"✅ {name} -> https://gist.githubusercontent.com/{owner}/{gist_id}/raw/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
