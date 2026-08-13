"""Entry point script for FinancialCalc MCP Server

This script ensures the Python path is set correctly
so imports work regardless of current working directory.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Now import and run the main module
if __name__ == "__main__":
    from financialcalc.main import app

    import uvicorn

    uvicorn.run(
        "financialcalc.main:app",
        host="0.0.0.0",
        port=3000,
        log_level="info",
    )
