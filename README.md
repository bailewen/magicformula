# Magic Formula Stock Screener

A Streamlit web application implementing Joel Greenblatt's Magic Formula investing strategy using Financial Modeling Prep API data.

## Features

- Screen stocks from NASDAQ, NYSE, AMEX, and other exchanges
- Filter by market capitalization
- Rank stocks by Earnings Yield (EY) and Return on Capital (ROC)
- Export results to CSV
- Parallel processing with caching for faster results
- Excludes financial services, utilities, and real estate sectors

## Magic Formula Metrics

- **Earnings Yield (EY)** = EBIT / Enterprise Value
- **Return on Capital (ROC)** = EBIT / (Net Working Capital + Net Fixed Assets)
- **Magic Formula Score** = Combined rank of EY and ROC (lower is better)

## Setup

### Prerequisites

- Python 3.8 or higher
- Financial Modeling Prep API key (get one at https://site.financialmodelingprep.com/)

### Installation

1. Clone this repository:
```bash
git clone https://github.com/YOUR_USERNAME/magic-formula-screener.git
cd magic-formula-screener
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set your API key:
```bash
export FMP_API_KEY="your_api_key_here"
```

Or on Windows:
```cmd
set FMP_API_KEY=your_api_key_here
```

### Running Locally

```bash
streamlit run app.py
```

The app will open in your browser at `http://localhost:8501`

## Usage

1. **Configure Settings** in the sidebar:
   - Select exchanges to scan
   - Set minimum market cap filter
   - Choose how many stocks to analyze (10-4500)
   - Set number of top results to display

2. **Run Scan**: Click the "Run Magic Formula Scan" button

3. **View Results**: Browse the ranked stocks and metrics

4. **Download**: Export results as CSV for further analysis

## Command Line Usage

You can also run the screener from the command line:

```bash
python magicformula.py --ex NASDAQ,NYSE --top 30 --min-mcap 5e7 --limit 400
```

**Parameters:**
- `--limit` - Controls how many stocks to screen. 400 runs fast, but the market is about 4500
- `--ex` - Controls which exchanges (comma-separated)
- `--top` - Controls how many picks to deliver in the results
- `--min-mcap` - Sets a minimum market cap (use scientific notation like 5e7 for 50 million)

## Methodology

Based on Joel Greenblatt's investment strategy from "The Little Book That Beats the Market":

1. Screens for US stocks above minimum market cap
2. Excludes financial services, utilities, and real estate sectors
3. Calculates trailing twelve month (TTM) EBIT
4. Ranks stocks by Earnings Yield and Return on Capital
5. Combines rankings to identify best opportunities

## Technical Details

- Uses Financial Modeling Prep API for fundamental data
- Implements rate limiting (300 calls/minute)
- Caches company data for 7 days to reduce API calls
- Parallel processing with ThreadPoolExecutor for efficiency
- Handles negative working capital and edge cases

## License

MIT License - Feel free to use and modify

## Disclaimer

This tool is for educational and research purposes only. It is not financial advice. Always do your own research before making investment decisions.
