"""
MAKLER WEEKLY CRAWLER
Läuft auf GitHub Actions Server – scannt alle Makler-Websites.
"""

import os, re, json, time, hashlib, sqlite3, requests
from datetime import datetime
from anthropic import Anthropic

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DB_PATH           = "weekly.db"
DASHBOARD_PATH    = "dashboard.html"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS websites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain TEXT UNIQUE NOT NULL,
        url TEXT NOT NULL,
        name TEXT, email TEXT,
        aktiv INTEGER DEFAULT 1,
        letzter_hash TEXT, letzter_scan TEXT,
        erstellt_am TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS funde (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        website_id INTEGER, datum TEXT,
        zusammenfassung TEXT, objekte_json TEXT,
        FOREIGN KEY(website_id) REFERENCES websites(id)
    )""")
    conn.commit()
    conn.close()

def importiere_websites():
    """Liest websites.json und importiert in DB."""
    if not os.path.exists("websites.json"):
        print("❌ websites.json nicht gefunden!")
        return 0
    with open("websites.json", encoding="utf-8") as f:
        kontakte = json.load(f)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    neu = 0
    for domain, info in kontakte.items():
        c.execute("""INSERT OR IGNORE INTO websites
                     (domain, url, name, email, erstellt_am)
                     VALUES (?,?,?,?,?)""",
                  (domain, info["url"], info.get("name",""),
                   info.get("email",""), datetime.now().isoformat()))
        if c.rowcount > 0:
            neu += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM websites").fetchone()[0]
    conn.close()
    print(f"✅ {neu} neue Websites importiert | Total: {total}")
    return total

def seite_abrufen(url):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        r.raise_for_status()
        sauber = re.sub(r'\d{10,}', '', r.text)
        sauber = re.sub(r'[a-f0-9]{32,}', '', sauber)
        return r.text, hashlib.md5(sauber.encode()).hexdigest()
    except:
        return "", ""

def analysiere(name, url, inhalt):
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        r = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": f"""Analysiere diese Immobilien-Makler-Website.

Firma: {name}
URL: {url}
Inhalt: {inhalt[:3000]}

Antworte NUR mit JSON:
{{
  "hat_angebote": true/false,
  "objekte": [
    {{"typ": "EFH/MFH/Wohnung/etc", "ort": "...", "preis": "...", "details": "..."}}
  ],
  "off_market": true/false,
  "zusammenfassung": "1-2 Sätze auf Deutsch"
}}"""}])
        text = r.content[0].text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        return {"hat_angebote": False, "objekte": [], "off_market": False,
                "zusammenfassung": f"Analyse nicht möglich: {e}"}

def websites_laden():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT id, url, name, email, letzter_hash, letzter_scan
                 FROM websites WHERE aktiv=1
                 ORDER BY letzter_scan ASC NULLS FIRST""")
    rows = c.fetchall()
    total = conn.execute("SELECT COUNT(*) FROM websites WHERE aktiv=1").fetchone()[0]
    conn.close()
    return rows, total

def update_website(wid, neuer_hash, analyse=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE websites SET letzter_hash=?, letzter_scan=? WHERE id=?",
              (neuer_hash, datetime.now().isoformat(), wid))
    if analyse and analyse.get("hat_angebote"):
        c.execute("""INSERT INTO funde (website_id, datum, zusammenfassung, objekte_json)
                     VALUES (?,?,?,?)""",
                  (wid, datetime.now().isoformat(),
                   analyse.get("zusammenfassung",""),
                   json.dumps(analyse.get("objekte",[]), ensure_ascii=False)))
    conn.commit()
    conn.close()

def generiere_dashboard(neue_funde):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT f.datum, w.name, w.url, w.email, f.zusammenfassung, f.objekte_json
                 FROM funde f JOIN websites w ON f.website_id=w.id
                 ORDER BY f.datum DESC LIMIT 500""")
    funde = c.fetchall()
    total_sites = conn.execute("SELECT COUNT(*) FROM websites WHERE aktiv=1").fetchone()[0]
    gescannt    = conn.execute("SELECT COUNT(*) FROM websites WHERE letzter_scan IS NOT NULL").fetchone()[0]
    total_funde = conn.execute("SELECT COUNT(*) FROM funde").fetchone()[0]
    conn.close()

    gruppen = {}
    for datum, name, url, email, zusammenfassung, objekte_json in funde:
        tag = datum[:10] if datum else "?"
        if tag not in gruppen:
            gruppen[tag] = []
        try:
            objekte = json.loads(objekte_json) if objekte_json else []
        except:
            objekte = []
        gruppen[tag].append({
            "datum": datum, "name": name or url, "url": url,
            "email": email or "", "zusammenfassung": zusammenfassung or "",
            "objekte": objekte
        })

    karten_html = ""
    if not gruppen:
        karten_html = """
        <div style="text-align:center;padding:80px 20px;color:#888">
          <div style="font-size:56px;margin-bottom:20px">🔍</div>
          <p style="font-size:17px;font-weight:500;color:#555">Noch keine neuen Angebote gefunden.</p>
          <p style="margin-top:10px;color:#aaa">
            Beim nächsten Scan werden Änderungen hier angezeigt.
          </p>
        </div>"""
    else:
        for tag in sorted(gruppen.keys(), reverse=True):
            try:
                label = datetime.strptime(tag, "%Y-%m-%d").strftime("%A, %d. %B %Y")
            except:
                label = tag
            eintraege = gruppen[tag]
            karten = ""
            for e in eintraege:
                uhr = e["datum"][11:16] if len(e["datum"]) > 10 else ""
                obj_html = ""
                for obj in e["objekte"][:3]:
                    preis = f" · {obj['preis']}" if obj.get("preis") else ""
                    obj_html += f"""
                    <div style="background:#f8f9fa;border-left:3px solid #c0392b;
                                padding:8px 12px;margin:6px 0;border-radius:0 4px 4px 0;font-size:12px">
                      <strong style="color:#2c3e50">{obj.get('typ','')} – {obj.get('ort','')}{preis}</strong>
                      {f'<br><span style="color:#666">{obj["details"]}</span>' if obj.get("details") else ""}
                    </div>"""
                email_btn = ""
                if e["email"]:
                    email_btn = f"""<a href="mailto:{e['email']}?subject=Immobilienanfrage"
                       style="font-size:12px;color:white;background:#2980b9;padding:5px 12px;
                              border-radius:4px;text-decoration:none;margin-right:8px">
                      ✉️ E-Mail</a>"""
                karten += f"""
                <div style="background:white;border-radius:10px;border:1px solid #e8e8e8;
                            box-shadow:0 1px 4px rgba(0,0,0,.07);overflow:hidden">
                  <div style="padding:14px 16px 10px;border-bottom:1px solid #f0f0f0">
                    <div style="display:flex;justify-content:space-between;align-items:flex-start">
                      <a href="{e['url']}" target="_blank"
                         style="font-size:15px;font-weight:600;color:#2c3e50;text-decoration:none">{e['name']}</a>
                      <span style="font-size:11px;color:#bbb">{uhr}</span>
                    </div>
                    {f'<div style="font-size:12px;color:#888;margin-top:4px">📧 {e["email"]}</div>' if e["email"] else ""}
                  </div>
                  <div style="padding:12px 16px">
                    <p style="font-size:13px;line-height:1.7;color:#444;margin:0 0 8px 0">{e['zusammenfassung']}</p>
                    {obj_html}
                  </div>
                  <div style="padding:10px 16px;background:#fafafa;border-top:1px solid #f0f0f0">
                    {email_btn}
                    <a href="{e['url']}" target="_blank"
                       style="font-size:12px;color:#2980b9;text-decoration:none;font-weight:500">🔗 Website →</a>
                  </div>
                </div>"""
            karten_html += f"""
            <div style="margin-bottom:36px">
              <div style="display:flex;justify-content:space-between;align-items:center;
                          padding-bottom:10px;border-bottom:2px solid #e0e0e0;margin-bottom:16px">
                <span style="font-size:17px;font-weight:600;color:#2c3e50">{label}</span>
                <span style="background:#c0392b;color:white;font-size:12px;font-weight:500;
                             padding:4px 14px;border-radius:20px">
                  {len(eintraege)} neue Angebot{"e" if len(eintraege)!=1 else ""}</span>
              </div>
              <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px">
                {karten}
              </div>
            </div>"""

    datum_str   = datetime.now().strftime("%d.%m.%Y um %H:%M Uhr")
    fortschritt = round(gescannt / total_sites * 100) if total_sites > 0 else 0

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Makler Weekly Scout – Orysinvest</title>
</head>
<body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#f0f2f5">
<div style="background:#1a252f;color:white;padding:22px 36px;
            display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
  <div>
    <div style="font-size:21px;font-weight:700">🏠 Makler Weekly Scout</div>
    <div style="font-size:13px;color:#8fa3b1;margin-top:4px">Orysinvest · Wöchentlicher Crawler</div>
    <div style="font-size:12px;color:#566573;margin-top:3px">Letzter Scan: {datum_str}</div>
  </div>
  <button onclick="location.reload()"
          style="background:#27ae60;color:white;border:none;padding:10px 20px;
                 border-radius:6px;cursor:pointer;font-size:13px;font-weight:500">↻ Aktualisieren</button>
</div>
<div style="background:white;padding:18px 36px;border-bottom:1px solid #e0e0e0;
            display:flex;gap:48px;flex-wrap:wrap;align-items:center">
  <div>
    <div style="font-size:26px;font-weight:700;color:#c0392b">{total_sites}</div>
    <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-top:2px">Makler überwacht</div>
  </div>
  <div>
    <div style="font-size:26px;font-weight:700;color:#c0392b">{len(neue_funde)}</div>
    <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-top:2px">Neue Angebote heute</div>
  </div>
  <div>
    <div style="font-size:26px;font-weight:700;color:#c0392b">{total_funde}</div>
    <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-top:2px">Total Angebote erkannt</div>
  </div>
  <div style="flex:1;min-width:200px">
    <div style="font-size:12px;color:#888;margin-bottom:6px">Scan-Fortschritt: {gescannt}/{total_sites} ({fortschritt}%)</div>
    <div style="background:#f0f0f0;border-radius:4px;height:8px;overflow:hidden">
      <div style="background:#27ae60;height:100%;width:{fortschritt}%"></div>
    </div>
  </div>
</div>
<div style="max-width:1200px;margin:0 auto;padding:32px 24px">{karten_html}</div>
<div style="text-align:center;padding:24px;font-size:12px;color:#aaa">
  Makler Weekly Scout · jovic@orysinvest.ch · jovicorys/makler-weekly-scout
</div>
</body></html>"""

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ Dashboard gespeichert → {DASHBOARD_PATH}")

def run():
    print(f"🏠 MAKLER WEEKLY CRAWLER – {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print("="*55)

    if not ANTHROPIC_API_KEY:
        print("❌ ANTHROPIC_API_KEY fehlt!")
        return

    init_db()
    importiere_websites()

    websites, total = websites_laden()
    if not websites:
        print("⚠️  Keine Websites gefunden!")
        return

    print(f"📋 {total} Websites werden gescannt...\n")
    neue_funde = []
    nicht_erreichbar = 0

    for i, (wid, url, name, email, letzter_hash, letzter_scan) in enumerate(websites):
        print(f"[{i+1}/{total}] {(name or url)[:55]}")
        inhalt, neuer_hash = seite_abrufen(url)

        if not inhalt:
            print("   ⚠️  Nicht erreichbar")
            nicht_erreichbar += 1
            time.sleep(1)
            continue

        if letzter_hash and neuer_hash != letzter_hash:
            print("   🆕 Änderung! Analysiere...")
            analyse = analysiere(name or "", url, inhalt)
            if analyse.get("hat_angebote"):
                off = " 🔒 OFF-MARKET!" if analyse.get("off_market") else ""
                print(f"   🏠 Angebot!{off} {analyse.get('zusammenfassung','')[:60]}")
                neue_funde.append({"name": name, "url": url, "email": email})
            else:
                print("   ↩️  Keine neuen Objekte")
            update_website(wid, neuer_hash, analyse)
        elif not letzter_hash:
            print("   📸 Erster Snapshot")
            update_website(wid, neuer_hash)
        else:
            print("   ✓ Keine Änderung")
            update_website(wid, neuer_hash)

        time.sleep(1.5)

    print(f"\n{'='*55}")
    print(f"✅ Fertig!")
    print(f"   Neue Angebote:     {len(neue_funde)}")
    print(f"   Nicht erreichbar:  {nicht_erreichbar}")
    print(f"   Total gescannt:    {total - nicht_erreichbar}")
    print("📊 Dashboard wird generiert...")
    generiere_dashboard(neue_funde)

if __name__ == "__main__":
    run()
