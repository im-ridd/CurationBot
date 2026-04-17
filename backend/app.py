import logging
import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from backend.database import engine
from backend.models import Base
from backend.routers import voters, fanbase, trails, status, bot_control
from backend.routers.frontend import router as frontend_router
from backend.auth import AuthMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Create all tables on startup
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="CurationBot API",
    description="Multi-account Steem curation bot management",
    version="2.1.0",
)

# API routers
app.include_router(voters.router)
app.include_router(fanbase.router)
app.include_router(trails.router)
app.include_router(status.router)
app.include_router(bot_control.router)

# Frontend
app.include_router(frontend_router)

# Auth middleware (after routers so login route is registered)
app.add_middleware(AuthMiddleware)


@app.get("/", tags=["root"])
def root():
    return RedirectResponse("/ui")


if __name__ == "__main__":
    from backend.config import API_HOST, API_PORT
    uvicorn.run("backend.app:app", host=API_HOST, port=API_PORT, reload=True)
