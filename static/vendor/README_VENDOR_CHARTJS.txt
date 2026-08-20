This folder should contain chart.umd.min.js (Chart.js v4.4.4), served locally
so the dashboard's Weekly Attendance Trend chart works with no internet
connection.

ONE-TIME SETUP (do this once, on any machine with internet):

  Option A - browser:
    1. Open this URL:
       https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.4/chart.umd.min.js
    2. Save the page as "chart.umd.min.js"
    3. Put that file in this folder (static/vendor/chart.umd.min.js)

  Option B - command line (on the machine that will run the app, if it has
  internet access at least once):
    curl -o static/vendor/chart.umd.min.js \
      https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.4/chart.umd.min.js

Once chart.umd.min.js is in this folder, delete this README (optional) and
restart app.py. dashboard.html now loads the chart library from
/static/vendor/chart.umd.min.js instead of the CDN, so no internet
connection is needed after this one-time download.
