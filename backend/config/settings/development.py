from .base import *
from .base import env_bool

DEBUG = True
ATLAS_ENABLE_DEVELOPMENT_ROLE_TEST = env_bool(
    'ATLAS_ENABLE_DEVELOPMENT_ROLE_TEST',
    True,
)
