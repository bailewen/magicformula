# Magic Formula Stock Screener

A global stock screener implementing Joel Greenblatt's Magic Formula strategy, supporting 12 international markets.

## Live Demo
Try it now: [magic.omarbelove.com](https://magic.omarbelove.com)


## Features
- Magic Formula ranking (Earnings Yield + Return on Capital)
- 12 global markets with quality tier ratings
- Parallel processing (analyze 400+ stocks in less than a minute)
- SQLite caching (uncached full market run ~30 min; repeated runs ~5-6 min)
- Real-time progress streaming via Server-Sent Events (SSE)
- Interactive scatter plot (Quality vs. Value)
- CSV export with full metrics including goodwill and intangibles
- TTM (trailing twelve month) calculations (default) with option to use annual reports instead
- Excludes financials, utilities, REITs per Greenblatt methodology
- Data sanity filters: negative EBIT exclusion, EV/market cap plausibility check
- Optional health checks for extra scrutiny
- Cancellable scans via Stop button

## Market Coverage

**Tier 1** (Highest Data Quality):
- USA - NASDAQ, NYSE, AMEX
- Singapore - SGX
- United Kingdom - LSE
- Canada - TSX

**Tier 2** (Good Quality):
- Australia - ASX
- Germany - XETRA
- France - Euronext Paris
- Japan - TSE

**Tier 3** (Emerging Markets):
- Hong Kong - HKEX
- South Korea - KRX
- India - NSE/BSE
- China - SSE/SZSE

## Quick Start

### Option 1: Use Hosted Version (Easiest)
1. Visit [magic.omarbelove.com](https://magic.omarbelove.com)
2. Run your scan — no API key needed

### Option 2: Run Locally

**Requirements:**
- Python 3.8+
- FMP API key (Starter plan or higher - $19/mo) — get one at [financialmodelingprep.com](https://financialmodelingprep.com/developer/docs/pricing)

**Installation:**
```bash
git clone https://github.com/bailewen/magicformula.git
cd magicformula
pip install -r requirements.txt
```

**Run Flask UI:**
```bash
export FMP_API_KEY="your_key_here"
python app.py
```

Then open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser. If you haven't set the environment variable, you can paste your API key directly in the sidebar.

**Run CLI:**
```bash
python magicformula.py --ex NASDAQ,NYSE --top 30 --limit 400
```

## Magic Formula Methodology

**Earnings Yield (EY)** = EBIT / Enterprise Value
- Measures how cheap the stock is relative to earnings
- Higher is better (more earnings per dollar invested)

**Return on Capital (ROC)** = EBIT / (Net Working Capital + Net Fixed Assets)
- Measures capital efficiency
- Higher is better (more earnings per dollar of capital)

**Magic Formula Score** = EY Rank + ROC Rank
- Lower score is better
- Finds stocks that are both cheap AND high quality

### Exclusions
Automatically excludes sectors that don't work well with the formula:
- Financial Services (banks, insurance)
- Utilities
- Real Estate / REITs

Additionally excludes individual stocks where:
- EBIT is negative
- Enterprise Value is implausibly small relative to market cap (likely data error)
- ROC exceeds 1000% (likely data error)
- Capital base is below $10M

## CLI Options
```bash
python magicformula.py \
  --ex NASDAQ,NYSE,AMEX \  # Exchanges to scan
  --top 30 \               # Number of results
  --limit 400 \            # Max stocks to analyze
  --min-mcap 50000000 \    # Minimum market cap ($50M)
  --annual \               # Use annual reports instead of TTM
  --no-intangibles \       # Exclude goodwill/intangibles from capital calculation
  --health-checks \        # Run D/E and cash flow quality checks on top candidates
  --random                 # Randomize symbol selection (avoids alphabetical bias with small samples)
```

## Output

Results include:
- **Basic Info**: Ticker, Name, Exchange, Country, Sector, Industry
- **Fundamentals**: Market Cap, Enterprise Value, EBIT, Cash, Total Debt
- **Balance Sheet**: Net Working Capital, Net Fixed Assets, Capital Base, Goodwill, Intangibles
- **Magic Formula Metrics**: Earnings Yield, Return on Capital
- **Rankings**: Individual ranks and combined Magic Formula score

Export to CSV for further analysis or portfolio tracking.

## Configuration

**For local use:**
```bash
export FMP_API_KEY="your_key_here"
```

Or add to `~/.bashrc` for persistence:
```bash
export FMP_API_KEY="your_key_here"
```

**For the hosted droplet:**
Add to the environment in your systemd service file:
```ini
Environment="FMP_API_KEY=your_key_here"
```

## Deployment (DigitalOcean / Production)

**Important:** Do not use the Flask development server in production. Use Gunicorn with a single worker to ensure scan state is shared correctly across all requests:

```bash
gunicorn -w 1 --threads 4 app:app
```

**Why `-w 1`:** Scan state is held in memory (`_scans` dict). Multiple Gunicorn workers would each have their own copy, causing SSE progress streams to fail. A single worker with multiple threads handles concurrent requests correctly without this issue.

**Example systemd service:**
```ini
[Unit]
Description=Magic Formula Screener
After=network.target

[Service]
User=streamlit
WorkingDirectory=/home/streamlit/magicformula
Environment="FMP_API_KEY=your_key_here"
ExecStart=/home/streamlit/magicformula/venv/bin/gunicorn -w 1 --threads 4 -b 0.0.0.0:5000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Main UI |
| `/scan` | POST | Start a new scan, returns `scan_id` |
| `/progress/<scan_id>` | GET | SSE stream of real-time progress |
| `/stop/<scan_id>` | POST | Cancel a running scan |
| `/results/<scan_id>` | GET | Fetch final ranked results as JSON |
| `/summary/<scan_id>` | GET | Fetch scan summary stats as JSON |
| `/download/<scan_id>` | GET | Download results as CSV |
| `/chart/<scan_id>` | GET | Standalone scatter plot and stats report |

## Contributing

Contributions welcome. Areas for improvement:
- Backtesting framework
- Portfolio tracking features
- Additional screening criteria

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Learn More

- [Joel Greenblatt's Magic Formula](https://www.magicformulainvesting.com/)
- [The Little Book That Beats the Market](https://www.amazon.com/Little-Book-That-Beats-Market/dp/0471733067)
- [FMP API Documentation](https://site.financialmodelingprep.com/developer/docs)

## Disclaimer

This tool is for educational and research purposes only. Not financial advice.
Past performance does not guarantee future results. Always do your own research
and consult with a qualified financial advisor before making investment decisions.

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Acknowledgments

- Joel Greenblatt for the Magic Formula methodology
- Financial Modeling Prep for comprehensive market data

---

**Built for value investors worldwide**

Questions? Open an issue on GitHub
