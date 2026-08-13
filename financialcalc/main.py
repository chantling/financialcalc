"""Main MCP server application for FinancialCalc - stdio protocol"""

import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from financialcalc.tools.analysis_support import (
    calculate_margin_of_safety, calculate_probability_weighted,
    calculate_sensitivity_analysis, calculate_sotp_valuation,
    normalize_base_value)
from financialcalc.tools.batch_operations import run_complete_dcf_analysis
from financialcalc.tools.core_calculations import (calculate_cagr,
                                                   calculate_custom_fcf,
                                                   calculate_graham_formula,
                                                   calculate_intrinsic_value,
                                                   calculate_net_debt,
                                                   calculate_present_value,
                                                   calculate_terminal_value,
                                                   project_cash_flows)
from financialcalc.tools.data_retrieval import (get_balance_sheet,
                                                get_current_metrics,
                                                get_financial_data,
                                                get_raw_financial_statements)
from financialcalc.tools.eight_pillar import run_eight_pillar_analysis
from financialcalc.tools.report_generation import (generate_analysis_report,
                                                   generate_scenario_report)
from financialcalc.tools.session_dcf import (get_base_fcf_options,
                                             run_dcf_analysis)
from financialcalc.utils.error_handling import FinancialCalcError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Register all tools
TOOLS: list[Tool] = [
    # --- Data Retrieval Tools ---
    Tool(
        name="get_financial_data",
        description="Retrieve historical financial data from Yahoo Finance (cached). "
        "All monetary values are automatically converted to USD. "
        "Returns revenue, net_income, free_cash_flow, operating_cash_flow, "
        "capital_expenditures, stock_based_compensation, sbc_adjusted_fcf, "
        "shares_outstanding, basic_eps, original_currency, exchange_rate_used, "
        "and company_info (long_name, short_name, industry, sector, business_summary).",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker symbol"},
                "years": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                    "description": "Number of years of data",
                },
                "force_refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force refresh data",
                },
                "clear_cache": {
                    "type": "boolean",
                    "default": False,
                    "description": "Clear cache before fetching",
                },
                "include_company_info": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include company name, industry, and sector metadata",
                },
            },
            "required": ["symbol"],
        },
    ),
    Tool(
        name="get_current_metrics",
        description="Retrieve current market data (cached): price, market cap, shares, previous_close, "
        "trading_currency, and company_info (long_name, short_name, industry, sector, business_summary).",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker symbol"},
                "force_refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force refresh data",
                },
                "clear_cache": {
                    "type": "boolean",
                    "default": False,
                    "description": "Clear cache before fetching",
                },
                "include_company_info": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include company name, industry, and sector metadata",
                },
            },
            "required": ["symbol"],
        },
    ),
    Tool(
        name="get_balance_sheet",
        description="Retrieve balance sheet data from Yahoo Finance (cached). "
        "All monetary values are automatically converted to USD. "
        "Returns total_assets, total_liabilities, total_equity, total_debt, "
        "total_cash, short_term_debt, long_term_debt, cash_and_equivalents, "
        "short_term_investments, goodwill, intangible_assets, original_currency, "
        "exchange_rate_used, and optionally company_info (long_name, short_name, "
        "industry, sector, business_summary).",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker symbol"},
                "years": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                    "description": "Number of years of data",
                },
                "force_refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force refresh data",
                },
                "clear_cache": {
                    "type": "boolean",
                    "default": False,
                    "description": "Clear cache before fetching",
                },
                "include_company_info": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include company name, industry, and sector metadata",
                },
            },
            "required": ["symbol"],
        },
    ),
    Tool(
        name="get_raw_financial_statements",
        description="Retrieve raw financial statement data from Yahoo Finance. "
        "All monetary values are automatically converted to USD. "
        "Returns all line items as key-value pairs for verification and custom calculations, "
        "original_currency, exchange_rate_used, and optionally company_info "
        "(long_name, short_name, industry, sector, business_summary). "
        "statement_type: 'income', 'cashflow', or 'balance'.",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker symbol"},
                "statement_type": {
                    "type": "string",
                    "enum": ["income", "cashflow", "balance"],
                    "default": "income",
                    "description": "Type of financial statement",
                },
                "years": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                    "description": "Number of years of data",
                },
                "force_refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force refresh data",
                },
                "clear_cache": {
                    "type": "boolean",
                    "default": False,
                    "description": "Clear cache before fetching",
                },
                "include_company_info": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include company name, industry, and sector metadata",
                },
            },
            "required": ["symbol"],
        },
    ),
    # --- Core Calculation Tools ---
    Tool(
        name="calculate_cagr",
        description="Calculate Compound Annual Growth Rate (CAGR). "
        "Example: [100, 110, 121] -> 10% CAGR over 2 years. "
        "Years is automatically calculated as (filtered_values_length - 1). "
        "Null values are filtered out automatically.",
        inputSchema={
            "type": "object",
            "properties": {
                "values": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Array of numerical values. "
                    "Example: [100, 110, 121] calculates 10% CAGR over 2 years.",
                }
            },
            "required": ["values"],
        },
    ),
    Tool(
        name="project_cash_flows",
        description="Project future cash flows with custom growth rates",
        inputSchema={
            "type": "object",
            "properties": {
                "base_fcf": {
                    "type": "number",
                    "description": "Base year free cash flow (non-negative)",
                },
                "growth_rates": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Array of growth rates for each projection year",
                },
                "years": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 10,
                    "description": "Number of projection years",
                },
            },
            "required": ["base_fcf", "growth_rates"],
        },
    ),
    Tool(
        name="calculate_present_value",
        description="Calculate present value of cash flow series",
        inputSchema={
            "type": "object",
            "properties": {
                "cash_flows": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Array of future cash flows",
                },
                "discount_rate": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 50,
                    "description": "Discount rate as percentage",
                },
            },
            "required": ["cash_flows", "discount_rate"],
        },
    ),
    Tool(
        name="calculate_terminal_value",
        description="Calculate terminal value using multiple method",
        inputSchema={
            "type": "object",
            "properties": {
                "final_year_fcf": {
                    "type": "number",
                    "description": "Final year projected free cash flow",
                },
                "terminal_multiple": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Terminal multiple",
                },
            },
            "required": ["final_year_fcf", "terminal_multiple"],
        },
    ),
    Tool(
        name="calculate_intrinsic_value",
        description="Calculate per-share intrinsic value. "
        "net_cash is positive for net cash position, negative for net debt.",
        inputSchema={
            "type": "object",
            "properties": {
                "pv_cash_flows": {
                    "type": "number",
                    "description": "Present value of projected cash flows",
                },
                "pv_terminal_value": {
                    "type": "number",
                    "description": "Present value of terminal value",
                },
                "net_cash": {
                    "type": "number",
                    "description": "Net cash/debt position (positive=net cash, negative=net debt)",
                },
                "shares_outstanding": {
                    "type": "number",
                    "minimum": 1,
                    "description": "Number of shares outstanding",
                },
            },
            "required": [
                "pv_cash_flows",
                "pv_terminal_value",
                "net_cash",
                "shares_outstanding",
            ],
        },
    ),
    Tool(
        name="calculate_net_debt",
        description="Calculate net cash/debt position from balance sheet components. "
        "Can use total_cash+total_debt or detailed components.",
        inputSchema={
            "type": "object",
            "properties": {
                "total_cash": {
                    "type": "number",
                    "description": "Total cash (cash + short-term investments)",
                },
                "total_debt": {
                    "type": "number",
                    "description": "Total debt (short-term + long-term)",
                },
                "cash_and_equivalents": {
                    "type": "number",
                    "description": "Cash and cash equivalents",
                },
                "short_term_investments": {
                    "type": "number",
                    "description": "Short-term investments",
                },
                "short_term_debt": {
                    "type": "number",
                    "description": "Short-term debt",
                },
                "long_term_debt": {
                    "type": "number",
                    "description": "Long-term debt",
                },
            },
        },
    ),
    Tool(
        name="calculate_graham_formula",
        description="Calculate intrinsic value using Benjamin Graham's formula. "
        "IV = EPS x [8.5 + (2 x G)] x 4.4 / Y",
        inputSchema={
            "type": "object",
            "properties": {
                "eps": {
                    "type": "number",
                    "description": "Earnings per share",
                },
                "growth_rate": {
                    "type": "number",
                    "description": "Expected growth rate as percentage (e.g., 7 for 7%)",
                },
                "bond_yield": {
                    "type": "number",
                    "description": "Current AAA corporate bond yield as percentage (e.g., 5 for 5%)",
                },
            },
            "required": ["eps", "growth_rate", "bond_yield"],
        },
    ),
    Tool(
        name="calculate_custom_fcf",
        description="Calculate adjusted free cash flow with custom adjustments. "
        "Base FCF = OCF - CapEx, then apply adjustments. "
        "Each adjustment has name and value (negative to subtract SBC, etc.)",
        inputSchema={
            "type": "object",
            "properties": {
                "operating_cash_flow": {
                    "type": "number",
                    "description": "Operating cash flow",
                },
                "capital_expenditures": {
                    "type": "number",
                    "description": "Capital expenditures (positive number, will be subtracted)",
                },
                "adjustments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "value": {"type": "number"},
                        },
                    },
                    "description": "Optional adjustments. "
                    'Example: [{"name": "SBC", "value": -12000000000}]',
                },
            },
            "required": ["operating_cash_flow", "capital_expenditures"],
        },
    ),
    # --- Analysis Support Tools ---
    Tool(
        name="calculate_margin_of_safety",
        description="Calculate buy price with margin of safety",
        inputSchema={
            "type": "object",
            "properties": {
                "intrinsic_value": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Calculated intrinsic value",
                },
                "margin_percent": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Margin of safety percentage",
                },
            },
            "required": ["intrinsic_value", "margin_percent"],
        },
    ),
    Tool(
        name="calculate_sensitivity_analysis",
        description="Perform sensitivity analysis on key variables. "
        "Simple mode: applies percentage adjustments. "
        "DCF mode (provide dcf_params): recalculates full DCF for each scenario.",
        inputSchema={
            "type": "object",
            "properties": {
                "base_value": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Base intrinsic value",
                },
                "variables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                    "description": "Array of variable names "
                    "(e.g., growth_rate, discount_rate, terminal_multiple)",
                },
                "ranges": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                    "description": "Array of value ranges for each variable",
                },
                "dcf_params": {
                    "type": "object",
                    "description": "Optional DCF parameters for recalculated analysis",
                    "properties": {
                        "base_fcf": {"type": "number"},
                        "growth_rates": {
                            "type": "array",
                            "items": {"type": "number"},
                        },
                        "discount_rate": {"type": "number"},
                        "terminal_multiple": {"type": "number"},
                        "shares_outstanding": {"type": "number"},
                        "net_cash": {"type": "number"},
                    },
                },
            },
            "required": ["base_value", "variables", "ranges"],
        },
    ),
    Tool(
        name="calculate_probability_weighted",
        description="Calculate probability-weighted average of scenarios",
        inputSchema={
            "type": "object",
            "properties": {
                "values": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Array of scenario values",
                },
                "probabilities": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Array of probabilities (must sum to 100)",
                },
            },
            "required": ["values", "probabilities"],
        },
    ),
    Tool(
        name="calculate_sotp_valuation",
        description="Calculate Sum-of-the-Parts (SOTP) intrinsic value for conglomerates. "
        "Each component has name and value_per_share. "
        "Optional overlap_adjustment_percent for double-counting correction.",
        inputSchema={
            "type": "object",
            "properties": {
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "value_per_share": {"type": "number"},
                        },
                        "required": ["name", "value_per_share"],
                    },
                    "description": "Array of business segment / asset components",
                },
                "overlap_adjustment_percent": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 100,
                    "default": 0,
                    "description": "Percentage reduction for overlap/double-counting",
                },
            },
            "required": ["components"],
        },
    ),
    Tool(
        name="normalize_base_value",
        description="Normalize a series of values to select a representative base value. "
        "Methods: average, median, most_recent, exclude_outliers. "
        "Useful for selecting normalized base FCF when most recent year may be outlier.",
        inputSchema={
            "type": "object",
            "properties": {
                "values": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Array of numerical values (nulls filtered automatically)",
                },
                "method": {
                    "type": "string",
                    "enum": ["average", "median", "most_recent", "exclude_outliers"],
                    "default": "average",
                    "description": "Normalization method",
                },
            },
            "required": ["values"],
        },
    ),
    # --- Batch Operation Tools ---
    Tool(
        name="run_complete_dcf_analysis",
        description="Execute complete DCF analysis with user-supplied assumptions. "
        "LLM agent supplies base_fcf, growth_rates, discount_rate, terminal_multiple, "
        "margin_of_safety, and optionally net_cash. Server performs all calculations.",
        inputSchema={
            "type": "object",
            "properties": {
                "financial_data": {
                    "type": "object",
                    "description": "Retrieved financial data (from get_financial_data)",
                },
                "assumptions": {
                    "type": "object",
                    "description": "User-defined assumptions",
                    "properties": {
                        "base_fcf": {
                            "type": "number",
                            "description": "User-selected base year FCF",
                        },
                        "growth_rates": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "Array of growth rates per projection year",
                        },
                        "discount_rate": {
                            "type": "number",
                            "description": "Discount rate as percentage",
                        },
                        "terminal_multiple": {
                            "type": "number",
                            "description": "Terminal value multiple",
                        },
                        "margin_of_safety": {
                            "type": "number",
                            "description": "Margin of safety percentage",
                        },
                        "net_cash": {
                            "type": "number",
                            "description": "Net cash/debt (default: 0)",
                        },
                        "shares_outstanding": {
                            "type": "number",
                            "description": "Override shares from financial_data",
                        },
                    },
                    "required": [
                        "base_fcf",
                        "growth_rates",
                        "discount_rate",
                        "terminal_multiple",
                        "margin_of_safety",
                    ],
                },
            },
            "required": ["financial_data", "assumptions"],
        },
    ),
    # --- Session-Based Tools (Recommended) ---
    Tool(
        name="get_base_fcf_options",
        description="Get all available base FCF calculation options with guidance. "
        "RECOMMENDED: Use this before running DCF analysis to choose the best base FCF method. "
        "Returns options: most_recent, average, median, sbc_adjusted, normalized.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session identifier (typically ticker symbol). "
                    "Session is created automatically when get_financial_data is called.",
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="run_dcf_analysis",
        description="Run DCF analysis using data from session. RECOMMENDED over run_complete_dcf_analysis. "
        "Pulls all data automatically from session - agent only needs to provide assumptions. "
        "Includes validation warnings and confidence scoring. "
        "Session is created when get_financial_data is called.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session identifier (typically ticker symbol)",
                },
                "growth_rates": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Array of growth rates for each projection year (typically 10 years)",
                },
                "terminal_multiple": {
                    "type": "number",
                    "description": "Terminal value multiple (e.g., 15 for 15x)",
                },
                "discount_rate": {
                    "type": "number",
                    "default": 10.0,
                    "description": "Discount rate as percentage (default: 10%)",
                },
                "margin_of_safety": {
                    "type": "number",
                    "default": 30.0,
                    "description": "Margin of safety percentage (default: 30%)",
                },
                "base_fcf_method": {
                    "type": "string",
                    "enum": [
                        "most_recent",
                        "average",
                        "median",
                        "sbc_adjusted",
                        "normalized",
                    ],
                    "default": "most_recent",
                    "description": "Method for calculating base FCF (default: most_recent)",
                },
                "net_cash_override": {
                    "type": "number",
                    "description": "Override net cash/debt position (optional)",
                },
                "shares_override": {
                    "type": "number",
                    "description": "Override shares outstanding (optional)",
                },
            },
            "required": ["session_id", "growth_rates", "terminal_multiple"],
        },
    ),
    Tool(
        name="generate_analysis_report",
        description="Generate a complete analysis report from session data. "
        "Report includes executive summary with confidence score at the top for quick review. "
        "Must call run_dcf_analysis first.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session identifier (typically ticker symbol)",
                },
                "qualitative_assessment": {
                    "type": "object",
                    "description": "Optional qualitative factors",
                    "properties": {
                        "business_quality": {
                            "type": "string",
                            "enum": ["poor", "average", "good", "excellent"],
                        },
                        "moat_strength": {
                            "type": "string",
                            "enum": ["none", "narrow", "wide"],
                        },
                        "management_quality": {
                            "type": "string",
                            "enum": ["poor", "average", "good", "excellent"],
                        },
                        "financial_health": {
                            "type": "string",
                            "enum": ["poor", "average", "good", "excellent"],
                        },
                        "notes": {"type": "string"},
                    },
                },
                "include_sensitivity": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to include sensitivity analysis (default: true)",
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="run_eight_pillar_analysis",
        description="Run complete 8-pillar fundamental stock analysis. "
        "Fetches all data internally from Yahoo Finance (reusing cached session data). "
        "Evaluates: PE Ratio, ROIC, Revenue Growth, Net Income Growth, "
        "Shares Trend, Long-Term Liabilities, FCF Growth, and FCF Multiple. "
        "Returns pass/fail for each pillar with an overall score. "
        "Thresholds are configurable via .env. "
        "Call get_financial_data first to populate session cache.",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Stock ticker symbol (e.g., AAPL)",
                },
                "years": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                    "description": "Number of years of historical data",
                },
                "force_refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force fresh data fetch from Yahoo Finance",
                },
                "clear_cache": {
                    "type": "boolean",
                    "default": False,
                    "description": "Clear cache before fetching",
                },
            },
            "required": ["symbol"],
        },
    ),
]


# Tool function registry
TOOL_FUNCTIONS = {
    # Data retrieval
    "get_financial_data": get_financial_data,
    "get_current_metrics": get_current_metrics,
    "get_balance_sheet": get_balance_sheet,
    "get_raw_financial_statements": get_raw_financial_statements,
    # Core calculations
    "calculate_cagr": calculate_cagr,
    "project_cash_flows": project_cash_flows,
    "calculate_present_value": calculate_present_value,
    "calculate_terminal_value": calculate_terminal_value,
    "calculate_intrinsic_value": calculate_intrinsic_value,
    "calculate_net_debt": calculate_net_debt,
    "calculate_graham_formula": calculate_graham_formula,
    "calculate_custom_fcf": calculate_custom_fcf,
    # Analysis support
    "calculate_margin_of_safety": calculate_margin_of_safety,
    "calculate_sensitivity_analysis": calculate_sensitivity_analysis,
    "calculate_probability_weighted": calculate_probability_weighted,
    "calculate_sotp_valuation": calculate_sotp_valuation,
    "normalize_base_value": normalize_base_value,
    # Batch operations
    "run_complete_dcf_analysis": run_complete_dcf_analysis,
    # Session-based tools (recommended)
    "get_base_fcf_options": get_base_fcf_options,
    "run_dcf_analysis": run_dcf_analysis,
    "generate_analysis_report": generate_analysis_report,
    # 8-Pillar analysis
    "run_eight_pillar_analysis": run_eight_pillar_analysis,
}


def handle_tool_call(tool_name: str, params: dict) -> list[TextContent]:
    """Handle tool call and return result"""
    logger.info(f"Tool call: {tool_name} with params: {params}")

    if tool_name not in TOOL_FUNCTIONS:
        raise ValueError(f"Tool not found: {tool_name}")

    try:
        tool_func = TOOL_FUNCTIONS[tool_name]
        result = tool_func(**params)

        logger.info(f"Tool {tool_name} executed successfully")
        return [TextContent(type="text", text=str(result))]

    except FinancialCalcError as e:
        logger.error(f"Tool error in {tool_name}: {e}")
        raise

    except Exception as e:
        logger.error(f"Unexpected error in {tool_name}: {e}")
        raise


async def main():
    """Main server loop using stdio transport"""
    logger.info("Starting FinancialCalc MCP Server (stdio mode)")

    # Create MCP server instance
    server = Server("financialcalc")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available tools"""
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: Any) -> list[TextContent]:
        """Handle tool calls"""
        return handle_tool_call(name, arguments or {})

    # Run the server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
