# FinancialCalc MCP Server

A calculation-only MCP server for financial analysis, providing accurate DCF valuation tools.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

## Configuration

### Environment Variables

Create a `.env` file from `.env.example`:

```env
CACHE_DIR=./cache
CACHE_ENABLED=true
HOST=0.0.0.0
PORT=3000
LOG_LEVEL=INFO
```

### MCP Client Configuration

#### OpenCode Configuration

Add to `C:\Users\John\.config\opencode\opencode.json`:

```json
{
  "mcp": {
    "financialcalc": {
      "type": "local",
      "command": ["D:\\Programs\\AI\\!MCPServers!\\FinancialCalc\\run_mcp_server.bat"],
      "enabled": true
    }
  }
}
```

**Note:** OpenCode uses a batch script wrapper (`run_mcp_server.bat`) to set the working directory before running the server.

#### Cline Configuration

Add to `C:\Users\John\AppData\Roaming\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "financialcalc": {
      "type": "stdio",
      "command": "D:\\Programs\\AI\\!MCPServers!\\FinancialCalc\\.venv\\Scripts\\python.exe",
      "args": ["-m", "financialcalc.main"],
      "cwd": "D:\\Programs\\AI\\!MCPServers!\\FinancialCalc",
      "disabled": false,
      "timeout": 60
    }
  }
}
```

**Note:** Cline supports the `cwd` parameter to set the working directory, so it can run the Python module directly.

## Running the Server

```bash
python -m financialcalc.main
```

Or with uvicorn:

```bash
uvicorn financialcalc.main:app --reload --port 3000
```

## Available Tools

### 1. `get_financial_data`
Retrieve historical financial data from Yahoo Finance (cached).

**Inputs:**
- `symbol` (string, required): Stock ticker symbol (e.g., "AAPL")
- `years` (integer, optional): Number of years of data, 1-10 (default: 5)
- `force_refresh` (boolean, optional): Force refresh data (default: false)
- `clear_cache` (boolean, optional): Clear cache before fetching (default: false)

**Outputs:**
- Historical financial data including revenue, free cash flow, debt, cash, shares outstanding

---

### 2. `get_current_metrics`
Retrieve current market data (cached): price, market cap, shares.

**Inputs:**
- `symbol` (string, required): Stock ticker symbol
- `force_refresh` (boolean, optional): Force refresh data (default: false)
- `clear_cache` (boolean, optional): Clear cache before fetching (default: false)

**Outputs:**
- Current stock price
- Market capitalization
- Shares outstanding
- Net cash/debt position

---

### 3. `calculate_cagr`
Calculate Compound Annual Growth Rate (CAGR).

**Inputs:**
- `values` (array of numbers, required): Array of numerical values (e.g., [100, 110, 121])
  - Years is automatically calculated as (filtered_values_length - 1)
  - Null values are filtered out automatically

**Outputs:**
- `cagr` (number): Compound annual growth rate as percentage
- Example: [100, 110, 121] → 10% CAGR over 2 years

---

### 4. `project_cash_flows`
Project future cash flows with custom growth rates.

**Inputs:**
- `base_fcf` (number, required): Base year free cash flow
- `growth_rates` (array of numbers, required): Array of growth rates for each projection year (e.g., [5, 4, 3])
- `years` (integer, optional): Number of projection years, 1-20 (default: 10)

**Outputs:**
- Array of projected cash flows for each year

---

### 5. `calculate_present_value`
Calculate present value of cash flow series.

**Inputs:**
- `cash_flows` (array of numbers, required): Array of future cash flows
- `discount_rate` (number, required): Discount rate as percentage, 0-50

**Outputs:**
- `present_values` (array): Present values for each year
- `total_pv` (number): Sum of all present values
- `pv_factors` (array): Present value factors used

---

### 6. `calculate_terminal_value`
Calculate terminal value using multiple method.

**Inputs:**
- `final_year_fcf` (number, required): Final year projected free cash flow
- `terminal_multiple` (number, required): Terminal multiple, 1-50

**Outputs:**
- `terminal_value` (number): Calculated terminal value

---

### 7. `calculate_intrinsic_value`
Calculate per-share intrinsic value.

**Inputs:**
- `pv_cash_flows` (number, required): Present value of projected cash flows
- `pv_terminal_value` (number, required): Present value of terminal value
- `net_cash` (number, required): Net cash/debt position
- `shares_outstanding` (number, required): Number of shares outstanding (minimum: 1)

**Outputs:**
- `intrinsic_value` (number): Calculated per-share intrinsic value

---

### 8. `calculate_margin_of_safety`
Calculate buy price with margin of safety.

**Inputs:**
- `intrinsic_value` (number, required): Calculated intrinsic value (minimum: 0)
- `margin_percent` (number, required): Margin of safety percentage, 0-100

**Outputs:**
- `buy_price` (number): Recommended buy price with margin of safety

---

### 9. `calculate_sensitivity_analysis`
Perform sensitivity analysis on key variables.

**Inputs:**
- `base_value` (number, required): Base intrinsic value (minimum: 0)
- `variables` (array of strings, required): Array of variable names (max 3)
- `ranges` (array of arrays, required): Array of value ranges for each variable

**Outputs:**
- Sensitivity analysis results showing intrinsic value changes across variable ranges

---

### 10. `calculate_probability_weighted`
Calculate probability-weighted average of scenarios.

**Inputs:**
- `values` (array of numbers, required): Array of scenario values
- `probabilities` (array of numbers, required): Array of probabilities for each scenario

**Outputs:**
- `weighted_average` (number): Probability-weighted average value

---

### 11. `run_complete_dcf_analysis`
Execute complete DCF analysis with user assumptions.

**Inputs:**
- `financial_data` (object, required): Retrieved financial data from `get_financial_data`
- `assumptions` (object, required): User-defined assumptions including:
  - `growth_rates`: Array of growth rates for projection years
  - `discount_rate`: Discount rate as percentage
  - `terminal_multiple`: Terminal multiple for exit value
  - `margin_of_safety`: Margin of safety percentage

**Outputs:**
- `intrinsic_value`: Calculated per-share intrinsic value
- `buy_price`: Recommended buy price with margin of safety
- `projected_cash_flows`: Array of projected cash flows
- `present_values`: Present value of cash flows
- `terminal_value`: Calculated terminal value
- Analysis breakdown of all calculations

## Caching

The server uses SQLite caching to reduce Yahoo Finance API calls:

- **Current Price:** 15 minutes
- **Market Cap:** 1 hour
- **Historical Financials:** 1 week
- **Shares Outstanding:** 1 week

### Cache Control

- **Force Refresh:** Use `force_refresh=True` to bypass cache and fetch fresh data
- **Clear Cache:** Use `clear_cache=True` to clear cache for a symbol before fetching

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=. --cov-report=html

# Run single test file
pytest tests/unit/test_core_calculations.py

# Run single test function
pytest tests/unit/test_core_calculations.py::test_calculate_cagr

# Run with verbose output
pytest -v

# Run integration tests only
pytest tests/integration/

# Run unit tests only
pytest tests/unit/
```

## Development

```bash
# Format code
black tools/ utils/ tests/
isort tools/ utils/ tests/

# Lint
flake8 tools/ utils/ tests/

# Type check
mypy tools/ utils/
```

## Example Usage

### Complete DCF Analysis

```python
from tools.data_retrieval import get_financial_data
from tools.batch_operations import run_complete_dcf_analysis

# Get financial data
financial_data = get_financial_data("AAPL", years=5)

# Define assumptions
assumptions = {
    "growth_rates": [8, 7, 6, 5, 4, 4, 4, 4, 4, 4],
    "discount_rate": 9.0,
    "terminal_multiple": 15.0,
    "margin_of_safety": 25.0
}

# Run analysis
results = run_complete_dcf_analysis(financial_data, assumptions)
print(f"Intrinsic Value: ${results['intrinsic_value']}")
print(f"Buy Price (25% MOS): ${results['buy_price']}")
```

## API Endpoints

- `GET /` - Server info
- `GET /health` - Health check
- `GET /tools` - List available tools
- `POST /tools/call` - Execute a tool
- `GET /tools/{tool_name}/schema` - Get tool schema

## Performance

- Simple calculations: <1 second
- Complex calculations: <5 seconds
- Complete analysis: <10 seconds
- Data retrieval: <3 seconds
