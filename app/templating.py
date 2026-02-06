from fastapi.templating import Jinja2Templates

from .config import settings

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["DEV_MODE"] = bool(settings.dev_mode)
