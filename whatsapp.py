"""WhatsApp-Vorlagen aus Close heraus (Olivers Texte).

Der Lead-Link „💬 WhatsApp-Vorlagen“ in Close öffnet /wa/<lead_id>?k=<schlüssel>&u=<Close-Nutzer>.
Die Seite füllt Anrede, Nachname, Terminzeit (aus der Opportunity „Termin am“) und Absender ein
und öffnet WhatsApp mit Nummer und fertigem Text – gesendet wird aus dem eigenen WhatsApp.
Beim Öffnen schreibt der Dienst eine Notiz an den Lead und merkt sich die Anrede am Kontakt.

Close kann WhatsApp nicht selbst verschicken, darum dieser Weg über wa.me.
"""

import hashlib
import hmac
import html
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LINK_SHA256 = os.environ.get(
    "WA_LINK_SHA256", "736cf92f4189bd342bd8824d14b8e53559715216401179653d79fc16cd02143f"
)
CF_KONTAKT_ANREDE = "cf_JGcrnz7zuuObJuimeLC03LlfC6Nh3AqVNrZ4ICxdiE5"
CF_OPP_TERMIN_AM = "cf_3RRKOpcG5RyOUOpOSPKmcUZ6As1ldgpdBrPLr00flGW"
OPP_STATUS_TERMIN = "stat_1MKUqp9MRQJcfRzsYP13y2tO9pYBd7okA45K8DFqZ5d"
BERLIN = ZoneInfo("Europe/Berlin")
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
TITEL = ("Dr.", "Prof.")

# Wortlaut von Oliver Slawik (WhatsApp, 02.11.2026). {gruss} = „Herr Müller“, {tag} = „morgen“,
# {uhrzeit} = „17 Uhr“, {absender} = Name des Senders.
VORLAGEN = [
    ("pkv-unterlagen", "PKV-Unterlagen",
     "Hallo {gruss},\n\ndie gewünschten Unterlagen zur Privaten Krankenversicherung, wurden gerade per Mail an Sie versendet.\n\n"
     "Vielen Dank vorab für eine kurze Rückmeldung, ob die Unterlagen bei Ihnen eingegangen sind.\n\n"
     "Viele Grüße und einen schönen Abend.\n\n{absender}"),
    ("reminder-1", "Reminder 1 (10 Min. später)",
     "Hallo {gruss},\n\nwäre es Ihnen möglich, dass wir unser Telefonat {tag} {uhrzeit}, um 10 Minuten nach hinten verschieben könnten!? "
     "Ich komme von einem Auswärtstermin und weiß nicht ob ich es pünktlich {uhrzeit} schaffe.\n\n"
     "Vielen Dank vorab für eine kurze Rückmeldung und einen schönen Abend.\n\nViele Grüße\n\n{absender}"),
    ("reminder-2", "Reminder 2 (bleibt es dabei?)",
     "Hallo {gruss},\n\nich wollte mich kurz erkundigen, ob es {tag} bei {uhrzeit} bleibt!?\n\n"
     "Vielen Dank vorab für eine kurze Rückmeldung und einen schönen Abend.\n\nViele Grüße.\n\n{absender}"),
    ("weitere-unterlagen", "Weitere Unterlagen",
     "Hallo {gruss},\n\nich habe Ihnen gerade die aktualisierten Unterlagen per Mail geschickt.\n\n"
     "Vielen Dank vorab für eine kurze Rückmeldung, ob die Unterlagen bei Ihnen eingegangen sind und einen schönen Tag.\n\n"
     "Viele Grüße\n\n{absender}"),
    ("nicht-erreichbar", "Nicht erreichbar",
     "Hallo {gruss},\n\nich kann Sie telefonisch leider nicht erreichen!?\n\nRufen Sie mich zurück?\n\n"
     "Viele Grüße und einen schönen Tag.\n\n{absender}"),
    ("antragsentwurf", "Antragsentwurf",
     "Hallo {gruss},\n\nich habe Ihnen gerade die Antragsunterlagen per Mail geschickt.\n\n"
     "Vielen Dank vorab für eine kurze Rückmeldung, ob die Unterlagen bei Ihnen eingegangen sind und einen schönen Tag.\n\n"
     "Viele Grüße\n\n{absender}"),
    ("erstkontakt-nicht-erreicht", "Erstkontakt nicht erreicht",
     "Hallo {gruss},\n\nich hatte versucht, Sie zu erreichen, nur leider hat es nicht geklappt.\n\n"
     "Es geht bloß kurz um Ihre Anfrage zur privaten Krankenversicherung. Ich war dabei, das Angebot zu erstellen, "
     "und hätte dazu nur eine kurze Rückfrage. Soll das Angebot nur für Sie alleine gerechnet werden oder sollen auch "
     "weitere Personen mitversichert werden, wie zum Beispiel Ehepartner oder Kinder?\n"
     "Falls ja, bräuchte ich bitte noch die Geburtsjahre der jeweiligen Personen. Wenn Sie mir das kurz schreiben, "
     "kann ich das Angebot direkt fertigstellen.\nAnsonsten können Sie mich auch gerne jederzeit zurückrufen. \n"
     "Ich wünsche Ihnen schon mal einen schönen Tag.\n\nVielen Dank und beste Grüße, \n{absender}"),
]
TITEL_JE_VORLAGE = {v[0]: v[1] for v in VORLAGEN}


def schluessel_ok(k):
    return bool(k) and hmac.compare_digest(hashlib.sha256(k.encode()).hexdigest(), LINK_SHA256)


def nachname(name):
    teile = (name or "").split()
    if not teile:
        return ""
    titel = [t for t in teile[:-1] if t in TITEL]
    return " ".join(titel + [teile[-1]])


def ist_mobil(nummer):
    return bool(re.match(r"^\+491[567]", nummer or ""))


def termin_daten(close, lead_id):
    """Nächster offener Cal.com-Termin → {"tag": "morgen", "uhrzeit": "17 Uhr"}."""
    jetzt = datetime.now(BERLIN)
    opps = close("GET", "/opportunity/?lead_id=%s&status_id=%s&_limit=50" % (lead_id, OPP_STATUS_TERMIN))["data"]
    termine = []
    for o in opps:
        wert = o.get("custom." + CF_OPP_TERMIN_AM)
        if not wert:
            continue
        try:
            zeit = datetime.fromisoformat(wert.replace("Z", "+00:00")).astimezone(BERLIN)
        except ValueError:
            continue
        if zeit > jetzt - timedelta(hours=2):
            termine.append(zeit)
    if not termine:
        return None
    zeit = min(termine)
    abstand = (zeit.date() - jetzt.date()).days
    tag = {0: "heute", 1: "morgen"}.get(abstand) or "am %s, %s" % (WOCHENTAGE[zeit.weekday()], zeit.strftime("%d.%m."))
    uhrzeit = "%d Uhr" % zeit.hour if zeit.minute == 0 else "%d:%02d Uhr" % (zeit.hour, zeit.minute)
    return {"tag": tag, "uhrzeit": uhrzeit, "label": "%s, %s" % (tag, uhrzeit)}


def seiten_daten(close, lead_id, absender):
    lead = close("GET", "/lead/%s/" % lead_id)
    if not lead:
        return None
    empfaenger = []
    for k in lead.get("contacts") or []:
        for t in k.get("phones") or []:
            empfaenger.append({
                "kontakt_id": k["id"],
                "name": (k.get("name") or "").strip(),
                "nachname": nachname(k.get("name")),
                "anrede": k.get("custom." + CF_KONTAKT_ANREDE) or "",
                "telefon": t["phone"],
                "anzeige": t.get("phone_formatted") or t["phone"],
                "mobil": ist_mobil(t["phone"]),
            })
    # Benannte Kontakte mit Handynummer zuerst – WhatsApp geht nur auf Handynummern.
    empfaenger.sort(key=lambda e: (not e["mobil"], not e["name"]))
    return {
        "lead": {"id": lead["id"], "name": lead.get("display_name") or ""},
        "empfaenger": empfaenger,
        "termin": termin_daten(close, lead_id),
        "absender": absender,
        "vorlagen": [{"id": v[0], "titel": v[1], "text": v[2]} for v in VORLAGEN],
    }


def protokollieren(close, lead_id, d):
    """Notiz an den Lead, Anrede am Kontakt merken."""
    titel = TITEL_JE_VORLAGE.get(d.get("vorlage"), "WhatsApp")
    text = str(d.get("text") or "")[:4000]
    an = " · ".join(x for x in (str(d.get("gruss") or ""), str(d.get("telefon") or "")) if x)
    close("POST", "/activity/note/", {"lead_id": lead_id, "note": "\n".join([
        "💬 WhatsApp-Vorlage „%s“ geöffnet" % titel,
        "An: " + an,
        "Von: " + str(d.get("absender") or ""),
        "",
        text,
    ])})
    kontakt_id, anrede = str(d.get("kontakt_id") or ""), d.get("anrede")
    if kontakt_id.startswith("cont_") and anrede in ("Herr", "Frau"):
        kontakt = close("GET", "/contact/%s/" % kontakt_id)
        if kontakt and kontakt.get("lead_id") == lead_id and kontakt.get("custom." + CF_KONTAKT_ANREDE) != anrede:
            close("PUT", "/contact/%s/" % kontakt_id, {"custom." + CF_KONTAKT_ANREDE: anrede})


def seite(daten, schluessel):
    roh = json.dumps(daten, ensure_ascii=False).replace("<", "\\u003c")
    titel = html.escape(daten["lead"]["name"])
    return SEITE.replace("{{TITEL}}", titel).replace("{{DATEN}}", roh).replace(
        "{{SCHLUESSEL}}", json.dumps(schluessel))


def fehlerseite(text):
    return SEITE_FEHLER.replace("{{TEXT}}", html.escape(text))


SEITE_FEHLER = """<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhatsApp-Vorlagen</title><body style="font-family:system-ui,sans-serif;padding:24px;color:#1f2328">
<h2>WhatsApp-Vorlagen</h2><p>{{TEXT}}</p></body></html>"""

SEITE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>WhatsApp · {{TITEL}}</title>
<style>
:root{--bg:#f6f7f9;--karte:#fff;--text:#1f2328;--leise:#6b7280;--rand:#e3e6ea;--gruen:#1fa855;--gruen-dunkel:#178a45;--warn:#b45309;--warn-bg:#fff7ed;--wahl:#e8f7ee}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--karte:#1a1f25;--text:#e8eaed;--leise:#9aa3ad;--rand:#2c333b;--gruen:#25c063;--gruen-dunkel:#1fa855;--warn:#fbbf24;--warn-bg:#2a2112;--wahl:#163323}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:20px;margin:0 0 2px}
.unter{color:var(--leise);margin:0 0 18px}
.karte{background:var(--karte);border:1px solid var(--rand);border-radius:12px;padding:16px;margin-bottom:14px}
.karte h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--leise);margin:0 0 10px}
label{display:block;font-size:13px;color:var(--leise);margin:10px 0 4px}
input,select,textarea{width:100%;font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--rand);border-radius:8px;padding:9px 10px}
textarea{min-height:230px;resize:vertical;white-space:pre-wrap}
.zeile{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media (max-width:560px){.zeile{grid-template-columns:1fr}}
.knoepfe{display:flex;gap:6px;flex-wrap:wrap}
.knopf{border:1px solid var(--rand);background:var(--bg);color:var(--text);border-radius:8px;padding:8px 14px;font:inherit;cursor:pointer}
.knopf.an{background:var(--wahl);border-color:var(--gruen);font-weight:600}
.vorlagen{display:grid;grid-template-columns:1fr 1fr;gap:8px}
@media (max-width:560px){.vorlagen{grid-template-columns:1fr}}
.vorlage{text-align:left}
.hinweis{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:8px}
.aktion{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
.senden{background:var(--gruen);color:#fff;border:0;border-radius:10px;padding:12px 18px;font:600 16px system-ui,sans-serif;cursor:pointer}
.senden:hover{background:var(--gruen-dunkel)}
.senden:disabled{opacity:.45;cursor:not-allowed}
.status{color:var(--leise);font-size:13px;margin-top:8px;min-height:18px}
</style>
</head>
<body>
<main>
  <h1>💬 WhatsApp-Vorlagen</h1>
  <p class="unter" id="leadname"></p>

  <div class="karte">
    <h2>Empfänger</h2>
    <select id="empfaenger"></select>
    <div id="festnetz" class="hinweis" hidden>Das ist keine Handynummer – WhatsApp klappt damit meist nicht.</div>
    <label>Anrede</label>
    <div class="knoepfe" id="anrede">
      <button class="knopf" data-wert="Herr">Herr</button>
      <button class="knopf" data-wert="Frau">Frau</button>
    </div>
    <div class="zeile">
      <div><label for="name">Nachname</label><input id="name" autocomplete="off"></div>
      <div><label for="absender">Absender (dein Name)</label><input id="absender" autocomplete="off"></div>
    </div>
    <div class="zeile">
      <div><label for="tag">Termin – Tag</label><input id="tag" placeholder="morgen"></div>
      <div><label for="uhrzeit">Termin – Uhrzeit</label><input id="uhrzeit" placeholder="17 Uhr"></div>
    </div>
    <div class="status" id="terminquelle"></div>
  </div>

  <div class="karte">
    <h2>Vorlage</h2>
    <div class="vorlagen" id="vorlagen"></div>
  </div>

  <div class="karte">
    <h2>Text (vor dem Senden noch änderbar)</h2>
    <textarea id="text"></textarea>
    <div class="aktion">
      <button class="senden" id="senden">In WhatsApp öffnen</button>
      <button class="knopf" id="kopieren">Text kopieren</button>
    </div>
    <div class="status" id="status"></div>
  </div>
</main>
<script id="daten" type="application/json">{{DATEN}}</script>
<script>
const D = JSON.parse(document.getElementById("daten").textContent);
const KEY = {{SCHLUESSEL}};
const $ = (id) => document.getElementById(id);
let vorlage = null, anrede = "", vonHand = false;

function lesen(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } }
function merken(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

$("leadname").textContent = D.lead.name;
if (!D.empfaenger.length) {
  $("empfaenger").innerHTML = "<option>Keine Telefonnummer am Lead</option>";
  $("senden").disabled = true;
}
D.empfaenger.forEach((e, i) => {
  const o = document.createElement("option");
  o.value = i;
  o.textContent = (e.name || "Ohne Namen") + " – " + e.anzeige + (e.mobil ? "" : " (Festnetz)");
  $("empfaenger").appendChild(o);
});

function empfaengerWahl() {
  const e = D.empfaenger[$("empfaenger").value];
  if (!e) return;
  $("festnetz").hidden = e.mobil;
  $("name").value = e.nachname;
  setzeAnrede(e.anrede);
  neu();
}
function setzeAnrede(w) {
  anrede = w || "";
  document.querySelectorAll("#anrede .knopf").forEach((b) => b.classList.toggle("an", b.dataset.wert === anrede));
}
document.querySelectorAll("#anrede .knopf").forEach((b) => b.onclick = () => { setzeAnrede(b.dataset.wert); neu(); });

$("absender").value = lesen("wa_absender") || D.absender || "";
if (D.termin) {
  $("tag").value = D.termin.tag;
  $("uhrzeit").value = D.termin.uhrzeit;
  $("terminquelle").textContent = "Termin aus Cal.com: " + D.termin.label;
} else {
  $("tag").value = "morgen";
  $("terminquelle").textContent = "Kein offener Cal.com-Termin gefunden – Tag und Uhrzeit bei Reminder 1/2 bitte eintragen.";
}

D.vorlagen.forEach((v) => {
  const b = document.createElement("button");
  b.className = "knopf vorlage";
  b.textContent = v.titel;
  b.onclick = () => { vorlage = v; vonHand = false; document.querySelectorAll(".vorlage").forEach((x) => x.classList.toggle("an", x === b)); neu(); };
  $("vorlagen").appendChild(b);
});

function gruss() { return [anrede, $("name").value.trim()].filter(Boolean).join(" "); }
function neu() {
  if (!vorlage || vonHand) return pruefen();
  $("text").value = vorlage.text
    .replaceAll("{gruss}", gruss() || "…")
    .replaceAll("{tag}", $("tag").value.trim() || "…")
    .replaceAll("{uhrzeit}", $("uhrzeit").value.trim() || "…")
    .replaceAll("{absender}", $("absender").value.trim() || "…");
  pruefen();
}
function pruefen() {
  const fehlt = !vorlage ? "Bitte eine Vorlage wählen." : !anrede ? "Bitte Herr oder Frau wählen." : $("text").value.includes("…") ? "Es fehlt noch eine Angabe (…)." : "";
  $("senden").disabled = !!fehlt || !D.empfaenger.length;
  $("status").textContent = fehlt;
}
["name", "tag", "uhrzeit"].forEach((id) => $(id).oninput = neu);
$("absender").oninput = () => { merken("wa_absender", $("absender").value.trim()); neu(); };
$("empfaenger").onchange = empfaengerWahl;
$("text").oninput = () => { vonHand = true; pruefen(); };

$("senden").onclick = () => {
  const e = D.empfaenger[$("empfaenger").value];
  const nummer = e.telefon.replace(/\\D/g, "");
  window.open("https://wa.me/" + nummer + "?text=" + encodeURIComponent($("text").value), "_blank", "noopener");
  fetch("/wa/" + D.lead.id + "/log?k=" + encodeURIComponent(KEY), {
    method: "POST", keepalive: true, headers: {"Content-Type": "application/json"},
    body: JSON.stringify({vorlage: vorlage.id, text: $("text").value, gruss: gruss(), anrede, telefon: e.telefon, kontakt_id: e.kontakt_id, absender: $("absender").value.trim()}),
  }).then((r) => $("status").textContent = r.ok ? "WhatsApp geöffnet · Notiz in Close gespeichert." : "WhatsApp geöffnet · Notiz konnte nicht gespeichert werden.")
    .catch(() => $("status").textContent = "WhatsApp geöffnet · Notiz konnte nicht gespeichert werden.");
  e.anrede = anrede;
};
$("kopieren").onclick = async () => {
  try { await navigator.clipboard.writeText($("text").value); $("status").textContent = "Text kopiert."; }
  catch (e) { $("text").select(); document.execCommand("copy"); $("status").textContent = "Text kopiert."; }
};

empfaengerWahl();
pruefen();
</script>
</body>
</html>
"""
