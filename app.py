"""Cal.com → Close (PKV SmartCheck).

Cal.com meldet jede Buchung der Terminart „pkv-beratung“ an /cal/<token>. Der Dienst
sucht den Lead in Close (Link aus dem Lead → E-Mail → Telefon), legt sonst einen neuen an,
ergänzt den Kontakt (Name, E-Mail, Telefon), setzt den Lead-Status und führt eine
Opportunity in der Pipeline „PKV-Beratung“ mit dem Termin. Verschieben und Absagen
werden an derselben Opportunity nachgezogen.

Der Token in der Webhook-Adresse ist zugleich das Signatur-Geheimnis von Cal.com. Im Code
steht nur sein SHA-256, deshalb darf das Repo öffentlich sein. Einziges Geheimnis ist
CLOSE_API_KEY, das liegt nur in den Umgebungsvariablen von Render.

Außerdem liefert der Dienst die WhatsApp-Vorlagenseite aus (siehe whatsapp.py).

Nur Standardbibliothek, damit Render nichts installieren muss.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import whatsapp

CLOSE_API = "https://api.close.com/api/v1"
TOKEN_SHA256 = os.environ.get(
    "WEBHOOK_TOKEN_SHA256", "c31cba1ff1817fbda3e309fc828e8e2e02ddea3a83b2020df1d0e7070e6a53fd"
)
BERLIN = ZoneInfo("Europe/Berlin")

# IDs statt Namen: das Team benennt Views und Status gern um, die IDs bleiben.
LEAD_STATUS_TERMIN = "stat_PgFOp0kfhbsMWmCVX4gPb9qEOr9bGlEyqmagLuddOJo"  # Termin gebucht
LEAD_STATUS_ABGESAGT = "stat_MRS0KbxpF1C7Fdb9M8wsJpBwhuDl9jMTO8EjYtpr3At"  # Termin abgesagt / No-Show
LEAD_STATUS_KUNDE = "stat_jc6JIuYfzL9L89Nips9QRsu3JcV3AgfSS76kAhiTHq8"  # Kunde
OPP_STATUS_TERMIN = "stat_1MKUqp9MRQJcfRzsYP13y2tO9pYBd7okA45K8DFqZ5d"  # Termin vereinbart
OPP_STATUS_ABGESAGT = "stat_k1GukprnNI7uF3MFFfsFK5UbIoEsw8EcT3UywKHJLz9"  # Termin abgesagt (lost)
CF_OPP_TERMIN_AM = "cf_3RRKOpcG5RyOUOpOSPKmcUZ6As1ldgpdBrPLr00flGW"
CF_OPP_BUCHUNG = "cf_h4G9E5sXDN0qkhGvW4MSdNBqIwMhZcPheVHLUvCTHHS"
CF_LEAD_ZIELGRUPPE = "cf_5AfjbAchyCT11DzbDt8JOfx1rQH6ozHruwLOtXcgXET"
CF_LEAD_QUELLE = "cf_B7DNaPtORa5s7EiAsig8uCmBKevksEJos2anRwT1mTx"
ZIELGRUPPEN = {
    "Angestellte über JAEG", "Apotheker", "Beamte", "Berater", "Coach", "Designer", "Dozenten",
    "Elektriker", "Fitnesstrainer", "Fotograf", "Freebie Leads",
    "Freiberufler (Anwälte, Steuerberater, Architekten, Ingenieure)", "Handwerker",
    "IT & Software", "Lackierer", "Marketing Experte", "Selbstständige & Unternehmer",
    "Sonstige", "Tierärzte", "Webdesigner", "Zahnärzte", "Ärzte",
}
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# Cal.com kann dieselbe Buchung doppelt schicken; nacheinander abarbeiten verhindert doppelte Opportunities.
# Die Opportunity-Liste von Close hängt ein paar Sekunden hinterher, darum merkt sich der Dienst erledigte Buchungen.
_sperre = threading.Lock()
_erledigt = {}


def log(*teile):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *teile, flush=True)


# ---------- Close ----------

def close(methode, pfad, body=None):
    schluessel = os.environ["CLOSE_API_KEY"]
    kopf = {
        "Authorization": "Basic " + base64.b64encode((schluessel + ":").encode()).decode(),
        "Content-Type": "application/json",
    }
    daten = json.dumps(body).encode() if body is not None else None
    for versuch in range(4):
        anfrage = urllib.request.Request(CLOSE_API + pfad, data=daten, method=methode, headers=kopf)
        try:
            with urllib.request.urlopen(anfrage, timeout=20) as antwort:
                return json.load(antwort)
        except urllib.error.HTTPError as fehler:
            if fehler.code == 404:
                return None
            if fehler.code in (429, 500, 502, 503, 504) and versuch < 3:
                time.sleep(float(fehler.headers.get("Retry-After") or 2 ** versuch))
                continue
            raise RuntimeError("Close %s %s → %s %s" % (methode, pfad.split("?")[0], fehler.code, fehler.read()[:300]))
        except urllib.error.URLError:
            if versuch < 3:
                time.sleep(2 ** versuch)
                continue
            raise


def lead_suchen(abfrage):
    pfad = "/lead/?_limit=2&_fields=id&query=" + urllib.parse.quote(abfrage)
    treffer = close("GET", pfad)["data"]
    return treffer[0]["id"] if treffer else None


def lead_holen(lead_id):
    return close("GET", "/lead/%s/" % lead_id)


# ---------- Cal.com-Daten ----------

def antwort(responses, feld):
    wert = (responses or {}).get(feld)
    return wert.get("value") if isinstance(wert, dict) else wert


def telefon_normal(nummer):
    if not nummer:
        return ""
    roh = re.sub(r"[^\d+]", "", str(nummer))
    if roh.startswith("00"):
        roh = "+" + roh[2:]
    elif roh.startswith("0"):
        roh = "+49" + roh[1:]
    return roh if len(re.sub(r"\D", "", roh)) >= 7 else ""


def buchung_lesen(p):
    resp = p.get("responses") or {}
    teilnehmer = (p.get("attendees") or [{}])[0]
    name = antwort(resp, "name") or teilnehmer.get("name") or ""
    if isinstance(name, dict):  # neuere Formulare: {firstName, lastName}
        name = " ".join(x for x in (name.get("firstName"), name.get("lastName")) if x)
    ort = antwort(resp, "location")
    telefon, art = "", "Online"
    if isinstance(ort, dict):
        if ort.get("value") in ("phone", "attendeePhone"):  # echte Buchungen schicken „phone“
            telefon, art = ort.get("optionValue") or "", "Telefon"
        elif "meet" in str(ort.get("value", "")):
            art = "Google Meet"
    elif re.fullmatch(r"[\d\s+()/-]{7,}", str(p.get("location") or "")):
        telefon, art = p["location"], "Telefon"
    telefon = telefon_normal(telefon or teilnehmer.get("phoneNumber") or antwort(resp, "attendeePhoneNumber"))
    link = ((p.get("metadata") or {}).get("videoCallUrl")
            or (p.get("videoCallData") or {}).get("url")
            or (p.get("location") if str(p.get("location", "")).startswith("http") else ""))
    lead_link = antwort(resp, "closelead") or ""
    lead_id = re.search(r"lead_[A-Za-z0-9]+", lead_link)
    return {
        "uid": p.get("uid") or "",
        "alt_uid": p.get("rescheduleUid") or "",
        "start": p.get("startTime") or "",
        "ende": p.get("endTime") or "",
        "name": (name or "").strip(),
        "email": (antwort(resp, "email") or teilnehmer.get("email") or "").strip().lower(),
        "telefon": telefon,
        "art": art,
        "link": link or "",
        "zielgruppe": (antwort(resp, "zielgruppe") or "").strip(),
        "lead_id": lead_id.group(0) if lead_id else "",
        "nachricht": (antwort(resp, "notes") or "").strip(),
        "grund": (p.get("cancellationReason") or antwort(resp, "rescheduleReason") or "").strip(),
    }


def zeit_text(start, ende=""):
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(BERLIN)
    except ValueError:
        return start
    text = "%s, %s, %s" % (WOCHENTAGE[a.weekday()], a.strftime("%d.%m.%Y"), a.strftime("%H:%M"))
    if ende:
        try:
            e = datetime.fromisoformat(ende.replace("Z", "+00:00")).astimezone(BERLIN)
            text += "–" + e.strftime("%H:%M")
        except ValueError:
            pass
    return text + " Uhr"


# ---------- Lead + Kontakt ----------

def lead_finden_oder_anlegen(b):
    lead = lead_holen(b["lead_id"]) if b["lead_id"] else None
    weg = "Link aus Close" if lead else ""
    if not lead and b["email"]:
        gefunden = lead_suchen('email_address:"%s"' % b["email"])
        lead, weg = (lead_holen(gefunden), "E-Mail") if gefunden else (None, "")
    if not lead and b["telefon"]:
        gefunden = lead_suchen('phone_number:"%s"' % b["telefon"])
        lead, weg = (lead_holen(gefunden), "Telefon") if gefunden else (None, "")
    if lead:
        return lead, weg
    neu = {
        "name": b["name"] or b["email"] or "Cal.com-Buchung",
        "status_id": LEAD_STATUS_TERMIN,
        "custom." + CF_LEAD_QUELLE: "Cal.com-Buchung",
        "contacts": [kontakt_daten(b)],
    }
    if b["zielgruppe"] in ZIELGRUPPEN:
        neu["custom." + CF_LEAD_ZIELGRUPPE] = b["zielgruppe"]
    return close("POST", "/lead/", neu), "neu angelegt"


def kontakt_daten(b):
    daten = {"name": b["name"]}
    if b["email"]:
        daten["emails"] = [{"email": b["email"], "type": "office"}]
    if b["telefon"]:
        daten["phones"] = [{"phone": b["telefon"], "type": "mobile" if b["telefon"].startswith(("+4915", "+4916", "+4917")) else "office"}]
    return daten


def nur_ziffern(nummer):
    return re.sub(r"\D", "", nummer or "")[-9:]


def kontakt_pflegen(lead, b):
    """Passenden Kontakt ergänzen, sonst den namenlosen Firmenkontakt benennen, sonst neu anlegen."""
    kontakte = lead.get("contacts") or []
    passt = None
    for k in kontakte:
        mails = {e["email"].lower() for e in k.get("emails") or []}
        nummern = {nur_ziffern(t["phone"]) for t in k.get("phones") or []}
        if (b["email"] and b["email"] in mails) or (b["telefon"] and nur_ziffern(b["telefon"]) in nummern):
            passt = k
            break
    if not passt:
        namenlos = [k for k in kontakte if not (k.get("name") or "").strip()]
        passt = namenlos[0] if namenlos else None
    if not passt:
        daten = kontakt_daten(b)
        daten["lead_id"] = lead["id"]
        return close("POST", "/contact/", daten)

    aenderung = {}
    if b["name"] and not (passt.get("name") or "").strip():
        aenderung["name"] = b["name"]
    mails = passt.get("emails") or []
    if b["email"] and b["email"] not in {e["email"].lower() for e in mails}:
        aenderung["emails"] = mails + [{"email": b["email"], "type": "office"}]
    nummern = passt.get("phones") or []
    if b["telefon"] and nur_ziffern(b["telefon"]) not in {nur_ziffern(t["phone"]) for t in nummern}:
        aenderung["phones"] = nummern + kontakt_daten(b)["phones"]
    if aenderung:
        return close("PUT", "/contact/%s/" % passt["id"], aenderung)
    return passt


# ---------- Opportunity ----------

def opportunity_zur_buchung(lead_id, *uids):
    uids = [u for u in uids if u]
    opps = close("GET", "/opportunity/?lead_id=%s&_limit=100" % lead_id)["data"]
    for o in opps:
        if o.get("custom." + CF_OPP_BUCHUNG) in uids or (o.get("custom") or {}).get("Cal.com-Buchung") in uids:
            return o
    return None


def opp_daten(b):
    daten = {
        "note": "Cal.com: %s · %s" % (zeit_text(b["start"], b["ende"]), b["art"]),
        "custom." + CF_OPP_TERMIN_AM: b["start"],
        "custom." + CF_OPP_BUCHUNG: b["uid"],
    }
    return daten


def notiz(lead_id, zeilen):
    close("POST", "/activity/note/", {"lead_id": lead_id, "note": "\n".join(z for z in zeilen if z)})


def detail_zeilen(b):
    wie = b["art"]
    if b["art"] == "Telefon" and b["telefon"]:
        wie += " " + b["telefon"]
    if b["link"]:
        wie += " · " + b["link"]
    return [
        "Wann: " + zeit_text(b["start"], b["ende"]),
        "Wie: " + wie,
        "Gebucht von: " + " · ".join(x for x in (b["name"], b["email"], b["telefon"]) if x),
        "Zielgruppe (aus dem Link): " + b["zielgruppe"] if b["zielgruppe"] else "",
        "Nachricht: " + b["nachricht"] if b["nachricht"] else "",
    ]


# ---------- Ereignisse ----------

def gebucht(b, verschoben=False):
    lead, weg = lead_finden_oder_anlegen(b)
    kontakt = kontakt_pflegen(lead, b) if weg != "neu angelegt" else (lead.get("contacts") or [{}])[0]
    if lead.get("status_id") not in (LEAD_STATUS_TERMIN, LEAD_STATUS_KUNDE):
        close("PUT", "/lead/%s/" % lead["id"], {"status_id": LEAD_STATUS_TERMIN})

    opp = opportunity_zur_buchung(lead["id"], b["uid"], b["alt_uid"])
    if not verschoben and (b["uid"] in _erledigt or (opp and opp.get("custom." + CF_OPP_BUCHUNG) == b["uid"])):
        log("Buchung schon verarbeitet", b["uid"])
        return
    daten = opp_daten(b)
    if opp:
        if opp.get("status_id") == OPP_STATUS_ABGESAGT:
            daten["status_id"] = OPP_STATUS_TERMIN
        close("PUT", "/opportunity/%s/" % opp["id"], daten)
    else:
        daten.update({"lead_id": lead["id"], "status_id": OPP_STATUS_TERMIN})
        if kontakt.get("id"):
            daten["contact_id"] = kontakt["id"]
        opp = close("POST", "/opportunity/", daten)
    _erledigt[b["uid"]] = time.time()

    titel = "📅 Termin verschoben (Cal.com)" if verschoben else "📅 Termin gebucht (Cal.com)"
    notiz(lead["id"], [titel] + detail_zeilen(b) + [
        "Grund: " + b["grund"] if verschoben and b["grund"] else "",
        "Lead gefunden über: " + weg,
    ])
    log(titel, lead["id"], "über", weg, "opp", opp.get("id"))


def abgesagt(b):
    lead_id = b["lead_id"] if b["lead_id"] and lead_holen(b["lead_id"]) else ""
    if not lead_id and b["email"]:
        lead_id = lead_suchen('email_address:"%s"' % b["email"]) or ""
    if not lead_id and b["telefon"]:
        lead_id = lead_suchen('phone_number:"%s"' % b["telefon"]) or ""
    if not lead_id:
        log("Absage ohne passenden Lead, uid", b["uid"])
        return
    opp = opportunity_zur_buchung(lead_id, b["uid"])
    if opp:
        close("PUT", "/opportunity/%s/" % opp["id"], {"status_id": OPP_STATUS_ABGESAGT})
    else:
        # Gehört die Absage zu einem alten, inzwischen verschobenen Termin, läuft schon eine neuere Buchung.
        offen = close("GET", "/opportunity/?lead_id=%s&status_id=%s&_limit=100" % (lead_id, OPP_STATUS_TERMIN))["data"]
        if any(o.get("custom." + CF_OPP_BUCHUNG) for o in offen):
            log("Absage einer älteren Buchung übersprungen", lead_id)
            return
    lead = lead_holen(lead_id)
    if lead.get("status_id") == LEAD_STATUS_TERMIN:
        close("PUT", "/lead/%s/" % lead_id, {"status_id": LEAD_STATUS_ABGESAGT})
    notiz(lead_id, [
        "❌ Termin abgesagt (Cal.com)",
        "War: " + zeit_text(b["start"], b["ende"]),
        "Grund: " + b["grund"] if b["grund"] else "",
    ])
    log("Absage", lead_id, "opp", opp.get("id") if opp else None)


def verarbeiten(ereignis):
    art = ereignis.get("triggerEvent")
    if art in ("PING", None):
        return
    b = buchung_lesen(ereignis.get("payload") or {})
    with _sperre:
        if art == "BOOKING_CREATED":
            gebucht(b)
        elif art == "BOOKING_RESCHEDULED":
            gebucht(b, verschoben=True)
        elif art == "BOOKING_CANCELLED":
            abgesagt(b)
        else:
            log("Ereignis ignoriert:", art)


# ---------- Webserver ----------

def token_ok(token):
    return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), TOKEN_SHA256)


class Webhook(BaseHTTPRequestHandler):
    def _antwort(self, code, text, art="text/plain"):
        roh = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", art + "; charset=utf-8")
        self.send_header("Content-Length", str(len(roh)))
        # Die WhatsApp-Seite zeigt Kontaktdaten: nie zwischenspeichern, nie weiterreichen.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        self.wfile.write(roh)

    def _wa_ziel(self):
        """Lead-ID aus /wa/<lead_id>[/log], wenn der Schlüssel im Link stimmt."""
        pfad, _, abfrage = self.path.partition("?")
        teile = pfad.strip("/").split("/")
        parameter = urllib.parse.parse_qs(abfrage)
        if len(teile) not in (2, 3) or teile[0] != "wa" or not re.fullmatch(r"lead_[A-Za-z0-9]+", teile[1]):
            return None, None, parameter
        if not whatsapp.schluessel_ok((parameter.get("k") or [""])[0]):
            return None, None, parameter
        return teile[1], (teile[2] if len(teile) == 3 else ""), parameter

    def do_GET(self):
        if not self.path.startswith("/wa/"):
            return self._antwort(200, "ok")
        lead_id, rest, parameter = self._wa_ziel()
        if not lead_id or rest:
            return self._antwort(404, whatsapp.fehlerseite("Link ungültig – bitte den Link „💬 WhatsApp-Vorlagen“ in Close nutzen."), "text/html")
        try:
            daten = whatsapp.seiten_daten(close, lead_id, (parameter.get("u") or [""])[0].strip())
        except Exception as fehler:
            log("FEHLER WhatsApp-Seite", type(fehler).__name__, fehler)
            return self._antwort(500, whatsapp.fehlerseite("Close ist gerade nicht erreichbar. Bitte gleich nochmal öffnen."), "text/html")
        if not daten:
            return self._antwort(404, whatsapp.fehlerseite("Lead nicht gefunden."), "text/html")
        self._antwort(200, whatsapp.seite(daten, parameter["k"][0]), "text/html")

    do_HEAD = do_GET

    def do_POST(self):
        if self.path.startswith("/wa/"):
            return self._wa_protokoll()
        teile = self.path.split("?")[0].strip("/").split("/")
        if len(teile) != 2 or teile[0] != "cal" or not token_ok(teile[1]):
            return self._antwort(404, "nicht gefunden")
        roh = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        signatur = self.headers.get("X-Cal-Signature-256") or ""
        erwartet = hmac.new(teile[1].encode(), roh, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signatur, erwartet):
            log("Signatur falsch, abgewiesen")
            return self._antwort(401, "Signatur falsch")
        try:
            verarbeiten(json.loads(roh))
        except Exception as fehler:  # Cal.com wiederholt nicht – Fehler muss im Render-Log stehen
            log("FEHLER", type(fehler).__name__, fehler)
            return self._antwort(500, "Fehler")
        self._antwort(200, "ok")

    def _wa_protokoll(self):
        lead_id, rest, _ = self._wa_ziel()
        if not lead_id or rest != "log":
            return self._antwort(404, "nicht gefunden")
        try:
            roh = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 20000))
            whatsapp.protokollieren(close, lead_id, json.loads(roh))
        except Exception as fehler:
            log("FEHLER WhatsApp-Notiz", type(fehler).__name__, fehler)
            return self._antwort(500, "Fehler")
        log("WhatsApp-Vorlage geöffnet", lead_id)
        self._antwort(200, "ok")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if not os.environ.get("CLOSE_API_KEY"):
        sys.exit("CLOSE_API_KEY fehlt")
    port = int(os.environ.get("PORT", "10000"))
    log("Cal.com → Close läuft auf Port", port)
    ThreadingHTTPServer(("0.0.0.0", port), Webhook).serve_forever()
