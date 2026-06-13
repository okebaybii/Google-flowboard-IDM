import multiprocessing
import uvicorn
import webbrowser
import threading
import time
import os
import sys
import logging

# Ensure absolute imports work when frozen
if hasattr(sys, '_MEIPASS'):
    sys.path.append(os.path.abspath(os.path.dirname(__file__)))
else:
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "agent")))

from flowboard.main import app

def open_browser():
    # Wait a few seconds for the server to start
    time.sleep(3)
    url = "http://127.0.0.1:8101"
    print(f"\n==========================================")
    print(f"Flowboard is running at: {url}")
    print(f"==========================================\n")
    webbrowser.open(url)

if __name__ == "__main__":
    multiprocessing.freeze_support()
    logging.basicConfig(level=logging.INFO)

    # Packaged personal build: default to no-login (straight into the UI).
    # A user who exports FLOWBOARD_NO_AUTH=0 before launching keeps login.
    os.environ.setdefault("FLOWBOARD_NO_AUTH", "1")
    
    # Start the browser thread
    threading.Thread(target=open_browser, daemon=True).start()
    
    # Run the uvicorn server
    uvicorn.run(app, host="127.0.0.1", port=8101, log_level="info")
