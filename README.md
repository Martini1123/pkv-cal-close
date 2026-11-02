# Cal.com → Close (PKV SmartCheck)

Kleiner Webhook-Dienst: Jede Buchung der Cal.com-Terminart `pkv-beratung` landet automatisch in Close.

| Cal.com meldet | Was in Close passiert |
|---|---|
| Termin gebucht | Lead suchen (Link aus Close → E-Mail → Telefon), sonst neu anlegen · Kontakt mit Name/E-Mail/Telefon ergänzen · Lead-Status „Termin gebucht“ · Opportunity „Termin vereinbart“ in „PKV-Beratung“ mit Feld „Termin am“ · Notiz mit allen Angaben |
| Termin verschoben | dieselbe Opportunity bekommt die neue Zeit · Notiz |
| Termin abgesagt | Opportunity → „Termin abgesagt“ · Lead-Status „Termin abgesagt / No-Show“ (nur wenn er auf „Termin gebucht“ stand) · Notiz mit Grund |

## Betrieb

- Läuft auf Render (Web Service, Starter, Frankfurt), nur Python-Standardbibliothek, kein Build.
- Start: `python app.py`, Port aus `PORT`.
- Einzige Umgebungsvariable: `CLOSE_API_KEY` (Close → Settings → Developer → API Keys). Steht nur in Render, nie im Repo.
- Cal.com ruft `/cal/<token>` auf; der Token ist zugleich das Signatur-Geheimnis (`X-Cal-Signature-256`). Im Code steht nur sein SHA-256.
- Fehler stehen im Render-Log (Cal.com wiederholt fehlgeschlagene Aufrufe nicht).
