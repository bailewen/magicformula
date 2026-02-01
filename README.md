# Magic Formula Stock Screener

A global stock screener implementing Joel Greenblatt's Magic Formula strategy, supporting 12 international markets.

## Live Demo
Try it now: [magicformula.streamlit.app](https://magicformula-8gnumsrdgvqn2bsz96ym9d.streamlit.app/)


## Features
- Magic Formula ranking (Earnings Yield + Return on Capital)
- 12 global markets with quality tier ratings
- Parallel processing (analyze 400+ stocks in les than a minute)
- Cached data (uncached run of full marker ~ 30 min; repeated runs ~ 5-6 min)
- CSV export with full metrics
- TTM (trailing twelve month) calculations (defaiult) with option to use annual reporst instead
- Excludes financials, utilities, REITs per Greenblatt methodology
- Optional health checks for extra scrutiny

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
1. Visit [magicformula.streamlit.app](https://your-url-here.streamlit.app)
2. Get API key from [Financial Modeling Prep](https://financialmodelingprep.com/developer/docs/pricing) (Starter plan: $19/mo)
3. Paste key in sidebar
4. Run your scan

### Option 2: Run Locally

**Requirements:**
- Python 3.8+
- FMP API key (Starter plan or higher - $19/mo)

**Installation:**
```bash
git clone https://github.com/bailewen/magicformula.git
cd magicformula
pip install -r requirements.txt
```

**Run Streamlit UI:**
```bash
export FMP_API_KEY="your_key_here"
streamlit run app.py
```

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

## CLI Options
```bash
python magicformula.py \
  --ex NASDAQ,NYSE,AMEX \  # Exchanges to scan
  --top 30 \               # Number of results
  --limit 400 \            # Max stocks to analyze
  --min-mcap 50000000 \    # Minimum market cap ($50M)
  --random                 # Randomizes symbol selection (avoids alphabetical bias with small samples)
```

## Output Example

Results include:
- **Basic Info**: Ticker, Name, Exchange, Country
- **Fundamentals**: Market Cap, Enterprise Value, EBIT
- **Magic Formula Metrics**: Earnings Yield, Return on Capital
- **Rankings**: Individual ranks and combined Magic Formula score

Export to CSV for further analysis or portfolio tracking.

## Configuration

**For local use:**
```bash
export FMP_API_KEY="your_key_here"
```

**For Streamlit Cloud:**
Add to app settings > Secrets:
```toml
FMP_API_KEY = "your_key_here"
```

## Contributing

Contributions welcome! Areas for improvement:
- Backtesting framework
- Portfolio tracking features
- Better error handling
- Additional screening criteria

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Learn More

- [Joel Greenblatt's Magic Formula](https://www.magicformulainvesting.com/)
- [The Little Book That Beats the Market](https://www.amazon.com/Little-Book-That-Beats-Market/dp/0471733067)
- [FMP API Documentation](https://site.financialmodelingprep.com/developer/docs)
- [Streamlit Documentation](https://docs.streamlit.io/)

## Disclaimer

This tool is for educational and research purposes only. Not financial advice. 
Past performance does not guarantee future results. Always do your own research 
and consult with a qualified financial advisor before making investment decisions.

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Acknowledgments

- Joel Greenblatt for the Magic Formula methodology
- Financial Modeling Prep for comprehensive market data
- Streamlit for the excellent web framework
- The open source community

---

**Built for value investors worldwide**

Questions? Open an issue on GitHub
