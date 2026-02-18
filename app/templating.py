from fastapi.templating import Jinja2Templates

from .config import settings
from .constants import field_limits
from .constants.help_text import HELP_TEXT

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["DEV_MODE"] = bool(settings.dev_mode)
templates.env.globals["limits"] = field_limits
templates.env.globals["help_text"] = HELP_TEXT
