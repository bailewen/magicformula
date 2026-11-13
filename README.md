# Magic Formula Stock Screener

## What it does

There is a stock screener based on Joel Greenblatt's "magic formula" and a single ticker lookup for a deeper dive into the metrics of individual stocks.

## How it works

There are 3 main parts:

- A screener that scans US markets for value stocks
- A deep scan that saves JSON blobs for single ticker lookup
- A webpage hosted at [magic.omarbelove.com](https://magic.omarbelove.com)

### The screener

A Python script (`magicformula.py`) pulls data from a Financial Modeling Prep (FMP) endpoint, and data is cached to a SQLite database, `cache.db`, for 7 days. This is not intended for active trading, so data doesn't need to be any fresher than that.

It uses 10 threads at a time to reduce wait times and maximize my rate limit, and WAL mode reduces lock contention.

Results are displayed on the "Screener" tab found at [magic.omarbelove.com](https://magic.omarbelove.com) (hosted on a DigitalOcean droplet). But you can deploy it locally as well and launch it using <http://127.0.0.1:5000> from your browser. Note that if you decide to host it yourself, you will need your own FMP API key.

### The Single Ticker

`deep_scan.py` performs a deep scan of all US markets and then saves the data as JSON blobs in a separate table. Its main purpose is to create an on-demand database for fast lookup of any single stock on the Single Ticker tab. It also provides data for a deeper drill down of any stock returned on the Screener tab.

You can run the scan manually, but I prefer a weekly cron job.

### The webpage

The website, [magic.omarbelove.com](https://magic.omarbelove.com), is deployed on a DigitalOcean droplet and served up by a Flask app. Scan results are displayed on one tab and are exportable to CSV. If the cache is warm a full scan can be as quick as 5 or 6 minutes, but if it's cold, a scan takes about 20 minutes or so. The single ticker lookup, displayed on a second tab, is almost instant.

## How to deploy it locally

These instructions assume a Linux environment.

### Requirements

- Python 3.10 or later
- All packages listed in `requirements.txt`
- An [FMP API key](https://financialmodelingprep.com/developer/docs/pricing). The default rate limiter assumes a Starter plan.
- Approximately half a GB of space for the database

### Steps

1. Clone the repository and enter its directory:

   ```bash
   git clone https://github.com/bailewen/magicformula.git
   cd magicformula
   ```

2. Create a virtual environment:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

   > **Note:** Ubuntu/Debian users may need to install `python3-venv`.

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Set your API key:

   ```bash
   export FMP_API_KEY="your_key_here"
   ```

5. Populate the database for the single ticker lookup. The first run takes several hours, so run it detached:

   ```bash
   nohup python deep_scan.py > deep_scan.log 2>&1 &
   ```

6. Start the app:

   ```bash
   python app.py
   ```

Open <http://127.0.0.1:5000> in your browser.

### Keeping the data fresh

If you decide to set up a cron job to keep the data fresh, note that the cron job needs to be run as the same user who owns the app. The crontab I use to run the scan at 2 AM (server time) on Sundays looks like this:

```
FMP_API_KEY=my_key_here
0 2 * * 0 /path/to/magicformula/venv/bin/python /path/to/magicformula/deep_scan.py
```

## Disclaimer

This is a research tool, not financial advice. The data comes from FMP and can contain errors, so verify anything you plan to act on.
