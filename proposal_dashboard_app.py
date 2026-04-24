"""
proposal_dashboard_app.py

Streamlit 대시보드 진입점
실행: streamlit run proposal_dashboard_app.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proposal_agent.dashboard import main

if __name__ == "__main__":
    main()
