#!/usr/bin/env python3
"""
Streamlit App Launcher for Email AI Company Tool
Run this script to start the Streamlit web interface locally
"""

import subprocess
import sys
import os
from pathlib import Path

def check_dependencies():
    """Check if required dependencies are installed."""
    try:
        import streamlit
        import pandas
        import requests
        print("✅ All dependencies are installed")
        return True
    except ImportError as e:
        print(f"❌ Missing dependency: {e}")
        print("💡 Run: pip install -r requirements.txt")
        return False

def main():
    """Launch the Streamlit application."""
    print("🚀 Starting Email AI Company Tool - Streamlit Interface")
    print("="*60)
    
    # Check if we're in the right directory
    current_dir = Path(__file__).parent
    streamlit_app = current_dir / "streamlit_app.py"
    
    if not streamlit_app.exists():
        print("❌ streamlit_app.py not found in current directory")
        print(f"Current directory: {current_dir}")
        return
    
    # Check dependencies
    if not check_dependencies():
        return
    
    # Launch Streamlit
    try:
        print("🌐 Launching Streamlit web interface...")
        print("📱 The app will open in your default web browser")
        print("🔗 URL: http://localhost:8501")
        print("⏹️  Press Ctrl+C to stop the server")
        print("="*60)
        
        # Run streamlit
        subprocess.run([
            sys.executable, "-m", "streamlit", "run", 
            str(streamlit_app),
            "--server.port=8501",
            "--server.address=localhost"
        ])
        
    except KeyboardInterrupt:
        print("\n👋 Streamlit server stopped")
    except Exception as e:
        print(f"❌ Error launching Streamlit: {e}")
        print("💡 Try running manually: streamlit run streamlit_app.py")

if __name__ == "__main__":
    main()