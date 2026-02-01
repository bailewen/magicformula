import streamlit as st
import pandas as pd
import sys
import os
import plotly.express as px
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add current directory to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# Import your magic formula module
import magicformula as mf

# Page Config
st.set_page_config(
    page_title="Magic Formula Screener", 
    layout="wide",
    initial_sidebar_state="expanded"
)

st.title("📈 Magic Formula Stock Picker")
st.write("Based on Joel Greenblatt's strategy using Financial Modeling Prep data.")

# Sidebar for settings 

# add toggle (checkbox)
def toggle_tier1(): 
    if "all_t1" in st.session_state:
        for key in ["usa", "sgp", "gbr", "can"]: 
            st.session_state[key] = st.session_state.all_t1
            
with st.sidebar:
   
    st.header("⚙️ Settings")
    
    # API Key Input
    st.subheader("🔑 API Configuration")
    
    # Check if running with environment variable already set
    if os.getenv("FMP_API_KEY"):
        api_key_input = os.getenv("FMP_API_KEY")
        st.success("✅ API key loaded from environment")
    else:
        # Show input field for users without env var
        api_key_input = st.text_input(
            "FMP API Key",
            type="password",
            help="Get your API key at financialmodelingprep.com"
        )
        
        if not api_key_input:
            st.warning("⚠️ Please enter your FMP API key to use the screener")
            st.info("👉 Get an API key at [financialmodelingprep.com](https://financialmodelingprep.com/developer/docs/pricing)")
            st.markdown("**Required:** Starter plan or higher ($19/mo)")
            st.stop()
    
    # Set the API key as environment variable so magicformula.py can use it
    # Set the API key as environment variable so magicformula.py can use it
    os.environ["FMP_API_KEY"] = api_key_input
    
    run_button = st.button("🚀 Run Magic Formula Scan", type="primary", use_container_width=True)
    
    st.divider()
    
    
    # Exchange Selection
    exchanges = st.text_input(
            "Exchanges (comma-separated)", 
            value="NASDAQ,NYSE,AMEX",
            help="FMP exchange codes like NASDAQ, NYSE, AMEX, LSE"
)

    st.subheader("Markets")
    
    filter_by_country = st.checkbox(
        "Filter by company domicile",
        value=False,
        help="Off = all countries on US exchanges (Greenblatt default). On = filter by country."
    )
    
    if filter_by_country:
        st.checkbox( 
            "Select all Tier 1", 
            key="all_t1", 
            on_change=toggle_tier1 
        )
    
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.markdown("**Tier 1**")
            us = st.checkbox("USA", value=True, key="usa")
            sg = st.checkbox("SGP", value=False, key="sgp")
            uk = st.checkbox("GBR", value=False, key="gbr")
            ca = st.checkbox("CAN", value=False, key="can")
            
        with col2:
            st.markdown("**Tier 2**")
            au = st.checkbox("AUS", value=False)
            de = st.checkbox("DEU", value=False)
            fr = st.checkbox("FRA", value=False)
            jp = st.checkbox("JPN", value=False)
            
        with col3:
            st.markdown("**Tier 3**")
            hk = st.checkbox("HKG", value=False)
            kr = st.checkbox("KOR", value=False)
            in_market = st.checkbox("IND", value=False)
            cn = st.checkbox("CHN", value=False)
        
    # Build countries list
        selected_countries = []
        if us: selected_countries.append("US")
        if sg: selected_countries.append("SG")
        if uk: selected_countries.append("GB")
        if ca: selected_countries.append("CA")
        if au: selected_countries.append("AU")
        if de: selected_countries.append("DE")
        if fr: selected_countries.append("FR")
        if jp: selected_countries.append("JP")
        if hk: selected_countries.append("HK")
        if kr: selected_countries.append("KR")
        if in_market: selected_countries.append("IN")
        if cn: selected_countries.append("CN")
        
        if not selected_countries:
            st.warning("⚠️ Please select at least one market")

    else:
        selected_countries = None  # All countries on selected exchanges
    
    min_mcap = st.number_input(
        "Min Market Cap (USD)", 
        value=50_000_000,
        step=10_000_000,
        format="%d",
        help="Minimum market capitalization in USD"
    )
    
    scan_mode = st.radio(
        "Max Stocks to Scan",
        options=["Use Slider", "Enter Manually"],
        horizontal=True
    )
    
    if scan_mode == "Use Slider":
        limit = st.slider(
            "Number of stocks",
            min_value=10, 
            max_value=4500, 
            value=400,
            help="Limit processing (set to 4500 for all stocks)"
        )
    else:
        limit = st.number_input(
            "Number of stocks",
            min_value=10,
            max_value=10000,
            value=400,
            step=50,
            help="Enter any number (higher values may take longer)"
        )
    
    top_n = st.number_input(
        "Top N Results to Display", 
        value=30, 
        min_value=5, 
        max_value=100,
        help="Number of top-ranked stocks to show"
    )
    
    use_random = st.checkbox(
        "Randomize symbol selection",
        value=False,
        help="Shuffle symbols before limiting (for random sampling)"
    )
    use_annual = st.checkbox(
        "Use Annual Data (instead of TTM)",
        value=True,
        help="Use annual financial data instead of trailing twelve months"
    )
    
    st.divider()
    st.subheader("🩺 Health Checks (Optional)")
    
    check_debt_revenue = st.checkbox(
        "D/E decreasing + Revenue increasing",
        value=False,
        help="Require debt-to-equity ratio declining while revenue grows over 6 quarters"
    )
    
    check_cashflow = st.checkbox(
        "Cash flow exceeds net income",
        value=False,
        help="Require operating cash flow > net income for 8 consecutive quarters"
    )
 
# Main content area
if run_button:
    import time
    start_time = time.time()

    
    # Check for API key
    if not os.getenv("FMP_API_KEY"):
        st.error("❌ FMP_API_KEY environment variable not set!")
        st.info("Set it with: `export FMP_API_KEY='your_key_here'`")
        st.stop()
    
    # Step 1: Gather symbols
    with st.spinner("🔍 Gathering symbols from exchanges..."):
        exchanges_list = [x.strip() for x in exchanges.split(',') if x.strip()]
        
        all_symbols = []
        for ex in exchanges_list:
            try:
                rows = mf.list_symbols(ex, min_mcap,selected_countries)
                for r in rows:
                    sym = r.get("symbol")
                    if sym:
                        all_symbols.append(sym)
            except Exception as e:
                st.warning(f"⚠️ Error fetching symbols from {ex}: {str(e)}")
        
        # Dedup
        all_symbols = list(dict.fromkeys(all_symbols))
        
        # Randomize if requested
        if use_random:
            import random
            random.shuffle(all_symbols)
        
        # Limit
        if limit and len(all_symbols) > limit:
            all_symbols = all_symbols[:limit]
        
        st.success(f"✅ Found {len(all_symbols)} symbols to analyze")
    
    if not all_symbols:
        st.error("No symbols found. Check your exchange codes and market cap filter.")
        st.stop()
    
    # Step 2: Pull fundamentals with progress tracking
    st.subheader("📊 Analyzing Fundamentals")
    
    records = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    # Use ThreadPoolExecutor for parallel processing (like your CLI version)
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(mf.pull_company_cached, sym): sym for sym in all_symbols}
        
        completed = 0
        for future in as_completed(futures):
            sym = futures[future]
            completed += 1
            
            status_text.text(f"Analyzing {sym} ({completed}/{len(all_symbols)})")
            progress_bar.progress(completed / len(all_symbols))
            
            try:
                rec = future.result(timeout=10)
                if rec and rec.get("marketCap", 0) >= min_mcap:
                    records.append(rec)
            except Exception as e:
                # Silently skip errors (like your original code)
                pass
    
    progress_bar.empty()
    status_text.empty()
    
    # Step 3: Rank and display results
    if not records:
        st.error("❌ No qualifying stocks found. Try:")
        st.write("- Lowering the minimum market cap")
        st.write("- Increasing the scan limit")
        st.write("- Checking different exchanges")
        st.stop()
       
    # Rank using Magic Formula
    st.success(f"✅ Found {len(records)} qualifying stocks")
    
    # Rank using Magic Formula
    df = pd.DataFrame(records)
    ranked = mf.magic_formula_rank(df)
    
    # Apply health checks only to top candidates (much faster)
    if check_debt_revenue or check_cashflow:
        with st.spinner(f"🩺 Running health checks on top {top_n} candidates..."):
            top_candidates = ranked.head(top_n)
            healthy_tickers = []
            
            for ticker in top_candidates["ticker"]:
                health = mf.check_financial_health(
                    ticker,
                    check_debt_revenue=check_debt_revenue,
                    check_cashflow_quality=check_cashflow
                )
                if health["passes_all"]:
                    healthy_tickers.append(ticker)
            
            ranked = ranked[ranked["ticker"].isin(healthy_tickers)]
            st.info(f"🩺 Health checks: {len(healthy_tickers)}/{len(top_candidates)} passed")
    
    if len(ranked) == 0:
        st.error("❌ No stocks passed health checks. Try disabling some filters.")
        st.stop()
    
    # Select columns to display
    display_cols = [
        "ticker", "name", "exchange", "country", "sector", "industry",
        "marketCap", "EV", "EBIT", "EY", "ROC",
        "EY_rank", "ROC_rank", "MF_score"
    ]
    display_cols = [c for c in display_cols if c in ranked.columns]
    
    final_df = ranked[display_cols].head(top_n)
    
    # Display results
    st.subheader(f"🏆 Top {top_n} Stocks by Magic Formula")
    
    # Format numbers for better display
    formatted_df = final_df.copy()
    if "marketCap" in formatted_df.columns:
        formatted_df["marketCap"] = formatted_df["marketCap"].apply(lambda x: f"${x/1e9:.2f}B" if x >= 1e9 else f"${x/1e6:.1f}M")
    if "EV" in formatted_df.columns:
        formatted_df["EV"] = formatted_df["EV"].apply(lambda x: f"${x/1e9:.2f}B" if x >= 1e9 else f"${x/1e6:.1f}M")
    if "EBIT" in formatted_df.columns:
        formatted_df["EBIT"] = formatted_df["EBIT"].apply(lambda x: f"${x/1e9:.2f}B" if abs(x) >= 1e9 else f"${x/1e6:.1f}M")
    if "EY" in formatted_df.columns:
        formatted_df["EY"] = formatted_df["EY"].apply(lambda x: f"{x:.2%}")
    if "ROC" in formatted_df.columns:
        formatted_df["ROC"] = formatted_df["ROC"].apply(lambda x: f"{x:.2%}")
    
    st.dataframe(
        formatted_df,
        width='stretch',
        height=600
    )

    st.subheader("📊 Visual Analysis")
    
    # We use final_df (the raw numbers) rather than formatted_df (the strings) 
    # so Plotly can actually plot the numeric values correctly.
    fig = px.scatter(
        final_df, 
        x="EY", 
        y="ROC", 
        text="ticker", 
        size="marketCap", 
        color="sector",
        hover_name="name",
        labels={"EY": "Earnings Yield (Cheapness)", "ROC": "Return on Capital (Quality)"},
        title="Magic Formula Frontier: Quality vs. Value"
    )
    
    # Clean up the chart appearance
    fig.update_traces(textposition='top center')
    st.plotly_chart(fig, use_container_width=True)
    # -----------------------------------

    # Download button
    csv = final_df.to_csv(index=False).encode('utf-8')


    
    # Download button
    csv = final_df.to_csv(index=False).encode('utf-8')
    timestamp = pd.Timestamp.now().strftime('%Y%m%d_%H%M')
    st.download_button(
        label="💾 Download Results as CSV",
        data=csv,
        file_name=f"magic_formula_{timestamp}.csv",
        mime="text/csv",
        width='stretch'
    )
    
    # Summary statistics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Stocks Analyzed", len(records))
    with col2:
        avg_ey = final_df["EY"].mean() if "EY" in final_df.columns else 0
        st.metric("Avg Earnings Yield", f"{avg_ey:.2%}")
    with col3:
        avg_roc = final_df["ROC"].mean() if "ROC" in final_df.columns else 0
        st.metric("Avg ROC", f"{avg_roc:.2%}")
    with col4:
        top_score = final_df["MF_score"].iloc[0] if "MF_score" in final_df.columns and len(final_df) > 0 else 0
        st.metric("Top MF Score", f"{top_score:.0f}")

    # Display scan time
    elapsed = time.time() - start_time
    minutes, seconds = divmod(int(elapsed), 60)
    st.success(f"⏱️ Scan completed in {minutes}m {seconds}s")

else:
    # Welcome screen
    st.info("👈 Configure your scan settings in the sidebar and click **Run Magic Formula Scan** to start")
    
    st.markdown("""
    ### How it works:
    1. **Select exchanges** - Choose which stock exchanges to scan
    2. **Set minimum market cap** - Filter out micro-cap stocks
    3. **Limit scan size** - Control how many stocks to analyze (respects API limits)
    4. **Run the scan** - Let the screener analyze fundamentals and rank stocks
    
    ### Magic Formula Metrics:
    - **EY (Earnings Yield)** = EBIT / Enterprise Value
    - **ROC (Return on Capital)** = EBIT / (Net Working Capital + Net Fixed Assets)
    - **MF Score** = Combined rank (lower is better)
    
    Excludes financial services, utilities, and real estate sectors as per Greenblatt's methodology.
    """)
