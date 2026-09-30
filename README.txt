WEEKLY AIR QUALITY INDEX REPORT OF PUNJAB - Python generator
=============================================================

FOLDER
  weekly_aqi_report.py   the script (all settings are in the CONFIG block at the top)
  requirements.txt       libraries to install
  assets/                logos, QR code, 1373 helpline image (replace the PNGs to update them)
  raw_data/              put hourly AQMS station exports or district-average dashboard CSVs here
  shapefiles/            put the 41-district shapefile and the AQMS shapefile here
  output/                created automatically: the PDF report + an Excel file with every number

ONE-TIME SETUP (VS Code terminal)
  pip install -r requirements.txt

WEB APP (upload a CSV and download the reports)
  1. Run:  python app.py
  2. Open: http://127.0.0.1:5000
  3. Upload one CSV and select Generate report.
  Each upload is processed in its own folder under output/web_jobs.

FREE HOSTING (PythonAnywhere Beginner; no card)
  1. Create a free Beginner account at pythonanywhere.com. It provides one public
    web app at <username>.pythonanywhere.com and does not ask for a payment card.
  2. In a Bash console, clone this public repository and install the dependencies:
       git clone https://github.com/rana42ehsan-RS/Weekly-Report.git
       cd Weekly-Report
       mkvirtualenv --python=python3.13 aqi-report
       pip install --no-cache-dir -r requirements.txt
  3. In the Web tab, add a web app using Manual Configuration and Python 3.13.
     Set its virtualenv to /home/<username>/.virtualenvs/aqi-report.
  4. Open the generated WSGI configuration file and replace its Flask section with:
       import sys
       path = "/home/<username>/Weekly-Report"
       if path not in sys.path:
           sys.path.insert(0, path)
       from app import app as application
     Replace <username> with the account username, then save and reload the web app.
  The site is public. The free account has limited storage/resources and restricted
  outbound internet; this app processes uploads locally and does not need outbound
  internet. Uploads are limited to 100 MB, and generated downloads are cleaned up
  after 24 hours when another report is generated. Do not use it for sensitive data.

EVERY WEEK
    1. Copy the hourly station export(s) or district-average dashboard CSV into raw_data/
      (or into the main folder). Wide dashboard CSVs with a Time column and district • AQI
      columns are detected automatically.
  2. Run:  python weekly_aqi_report.py
     The week is the last 7 days found in the data. For a specific week:
          python weekly_aqi_report.py --start 2026-09-19
  3. Open output/Weekly_AQI_Report_<dates>.pdf
    For station-level input, check the "Station matching" sheet. For district-average input,
    check the "District mapping" sheet.

WHAT THE SCRIPT CALCULATES (same as the Method note in the report)
  - Hours with AQI but no valid PM2.5/PM10 are dropped; AQI above 500 is capped at 500.
  - Station-level input: district hourly AQI = average of all stations reporting in that hour.
  - District-average input: the supplied district hourly AQI is used directly; station readings
    and station counts are not inferred from aggregates.
  - Morning 08:00-15:59, Evening 16:00-23:59, Night 00:00-07:59: average if >= 4 of 8 hours.
  - Daily value = 24-hour average if >= 12 hours. Weekly value = average of the daily values.
  - Districts with fewer than 4 valid days get a * and are not ranked (MIN_DAYS_FOR_RANKING).
  - Punjab average = average of the ranked districts' weekly values.
  - Colours: 0-50 Good, 51-100 Satisfactory, 101-150 Moderate, 151-200 USG, 201-300 Unhealthy,
    301-400 Very Unhealthy, 400+ Hazardous.

MAPS
  - Cover map: every district coloured by its weekly AQI category; grey = no data.
  - Page 3 map: AQMS pins from the AQMS shapefile, with a blue badge showing the count per district.
      AQMS_MAP_MODE = "stations"  -> a pin at each real station location (default)
      AQMS_MAP_MODE = "district"  -> one pin per district, like the old report
  - Stations are placed in districts by point-in-polygon on the 41-district layer, so Murree,
    Talagang, Wazirabad, Kot Addu and Taunsa are handled correctly even if the dashboard
    still lists the old parent district.
  - If two labels overlap, nudge them with LABEL_OFFSETS, e.g. {"Lahore": (8, -4)}.

IF SOMETHING DOES NOT MATCH
  - station-level columns are not detected -> set COLUMN_MAP in CONFIG to the exact headings.
  - district-average dashboard CSV      -> supported automatically with Time and district • AQI columns.
  - a station "could not be placed"  -> add it to STATION_NAME_OVERRIDES (raw name -> AQMS shapefile
                                         name) or STATION_DISTRICT_OVERRIDES (raw name -> district).
  - dates read wrongly (month/day)   -> DAYFIRST = False.
  - hours shifted by one             -> TIMESTAMP_IS_HOUR_END = True (dashboard stamps 01:00 for 00-01).
  - district name shown differently  -> DISPLAY_NAMES, e.g. {"Dera Ghazi Khan": "DG Khan"}.
