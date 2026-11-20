import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()
from pathlib import Path
from utils.runtime_migrations import apply_runtime_migrations
apply_runtime_migrations(Path(__file__).resolve().parent)
import uvicorn
from config.settings import settings

def main():
    print("=" * 40)
    print("  Express System - starting...")
    print("=" * 40)
    from database import init_db
    init_db()
    url = f"http://localhost:{settings.app_port}"
    print(f"  Web:  {url}")
    if settings.debug:
        print(f"  API:  {url}/docs")
    uvicorn.run("api.main:app", host=settings.app_host, port=settings.app_port, reload=settings.debug)

if __name__ == "__main__":
    main()
