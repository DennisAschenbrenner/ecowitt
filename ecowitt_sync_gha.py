from datetime import datetime, timedelta
import os
import numpy as np
import pandas as pd
import requests

# ==========================================
# 1. DEINE ZUGANGSDATEN & PARAMETER EINTRAGEN
# ==========================================
APPLICATION_KEY = os.environ.get("APPLICATION_KEY")
API_KEY = os.environ.get("API_KEY")
MAC_ADRESSE = os.environ.get("MAC_ADRESSE")
TOKEN = os.environ.get("TOKEN")
URL = os.environ.get("URL")
url = os.environ.get("URL_ECOWITT")

CSV_DATEINAME = "ecowitt_wind_10min.csv"

# Ordner der .py-Datei automatisch ermitteln
ORDNER = os.path.dirname(os.path.abspath(__file__))
CSV_DATEINAME = os.path.join(ORDNER, "ecowitt_wind_10min.csv")
STATUS_DATEI = os.path.join(ORDNER, "letzter_upload.txt")
LOG_DATEI = os.path.join(ORDNER, "sync.log")


def log(nachricht):
  zeit = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  zeile = f"[{zeit}] {nachricht}"
  print(zeile)
  with open(LOG_DATEI, "a", encoding="utf-8") as lf:
    lf.write(zeile + "\n")


# ==========================================
# 2. API-ABRUF (LETZTE 24 STUNDEN IN m/s)
# ==========================================
jetzt = datetime.now()
vor_24h = jetzt - timedelta(hours=24)

params = {
    "application_key": APPLICATION_KEY,
    "api_key": API_KEY,
    "mac": MAC_ADRESSE,
    "start_date": vor_24h.strftime("%Y-%m-%d %H:%M:%S"),
    "end_date": jetzt.strftime("%Y-%m-%d %H:%M:%S"),
    "cycle_type": "5min",
    "call_back": "wind",
    "wind_speed_unitid": 6,  # 6 = m/s
}

try:
  response = requests.get(
      url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=20
  )
  daten = response.json()
except Exception as e:
  log(f"Fehler beim Ecowitt-Abruf: {e}")
  exit()

if daten.get("code") != 0:
  log(f"API-Fehler ({daten.get('code')}): {daten.get('msg')}")
  exit()

wind_data = daten.get("data", {}).get("wind", {})
if not wind_data:
  log("Keine Winddaten für diesen Zeitraum gefunden.")
  exit()

# ==========================================
# 3. IN DATAFRAME LADEN & 10-MIN-AGGREGATION
# ==========================================
df_dict = {}
for sensor_name, sensor_info in wind_data.items():
  if isinstance(sensor_info, dict) and "list" in sensor_info:
    serie = pd.Series(
        {int(ts): float(val) for ts, val in sensor_info["list"].items()}
    )
    df_dict[sensor_name] = serie

df = pd.DataFrame(df_dict)
df.index = pd.to_datetime(df.index, unit="s", utc=True).tz_convert(
    "Europe/Berlin"
)
df = df.sort_index()


def circular_mean_deg(angles):
  angles = angles.dropna()
  if len(angles) == 0:
    return np.nan
  rad = np.deg2rad(angles)
  mean_angle = np.rad2deg(np.arctan2(np.mean(np.sin(rad)), np.mean(np.cos(rad))))
  return round(mean_angle % 360, 1)


agg_regeln = {}
for col in df.columns:
  if "gust" in col or "max" in col:
    agg_regeln[col] = "max"
  elif "direction" in col:
    agg_regeln[col] = circular_mean_deg
  else:
    agg_regeln[col] = "mean"

df_10min = df.resample("10min", label="right", closed="left").agg(agg_regeln)

for col in df_10min.columns:
  if "direction" not in col:
    df_10min[col] = df_10min[col].round(2)

df_10min.index = df_10min.index.strftime("%Y-%m-%d %H:%M:%S")
df_10min.index.name = "timestamp"
df_neu = df_10min.reset_index()

# ==========================================
# 4. LOKALE CSV ERWEITERN & SPEICHERN
# ==========================================
if os.path.exists(CSV_DATEINAME):
  df_alt = pd.read_csv(CSV_DATEINAME, sep=";", encoding="utf-8-sig")
  df_gesamt = pd.concat([df_alt, df_neu], ignore_index=True)
  df_gesamt = df_gesamt.drop_duplicates(subset=["timestamp"], keep="last")
  df_gesamt = df_gesamt.sort_values(by="timestamp").reset_index(drop=True)
else:
  df_gesamt = df_neu

df_gesamt.to_csv(CSV_DATEINAME, sep=";", index=False, encoding="utf-8-sig")

# ==========================================
# 5. PER HTTP POST AUF DEN SERVER HOCHLADEN
# ==========================================
letzter_ts = ""
if os.path.exists(STATUS_DATEI):
  with open(STATUS_DATEI, "r", encoding="utf-8") as sf:
    letzter_ts = sf.read().strip()

hochgeladen = 0
with requests.Session() as session:
  with open(CSV_DATEINAME, encoding="utf-8-sig") as f:
    next(f)  # Kopfzeile überspringen
    for zeile in f:
      if not zeile.strip():
        continue

      teile = zeile.strip().split(";")
      ts = teile[0]

      if letzter_ts and ts <= letzter_ts:
        continue

      payload = {
          "api_key": TOKEN,
          "timestamp": teile[0],
          "wind_mean": teile[1],
          "wind_max": teile[2],
          "wind_dir_mean": teile[3],
          "wind_dir_mean10": teile[4],
      }

      try:
        res = session.post(URL, data=payload, timeout=10)
        if res.status_code == 200 and not res.text.strip().startswith(
            "<!DOCTYPE"
        ):
          letzter_ts = ts
          hochgeladen += 1
        else:
          log(
              f"Upload-Fehler bei {ts} -> HTTP {res.status_code}:"
              f" {res.text.strip()[:100]}"
          )
          break
      except Exception as e:
        log(f"Verbindungsfehler beim Upload: {e}")
        break

if hochgeladen > 0:
  with open(STATUS_DATEI, "w", encoding="utf-8") as sf:
    sf.write(letzter_ts)
  log(
      f"Erfolgreich {hochgeladen} neue Einträge hochgeladen (Stand:"
      f" {letzter_ts})."
  )
else:
  log("Lauf OK – keine neuen Daten zum Hochladen vorhanden.")